import contextlib
import ctypes
import errno
import os
import resource
import signal
from collections.abc import Iterator
from pathlib import Path

from . import MAXIMUM_LANDLOCK_ABI, SandboxLimits, SandboxReport
from .classification import CLASSIFICATION
from .tables import TABLES

LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long
LIBC.prctl.restype = ctypes.c_int

PR_SET_PDEATHSIG = 1
PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_FILTER_FLAG_TSYNC = 1
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
AUDIT_ARCHITECTURES = {'x86_64': 0xC000003E, 'aarch64': 0xC00000B7}

# linux/filter.h and linux/seccomp.h: classic BPF over struct seccomp_data.
LOAD = 0x20
EQUAL = 0x15
BITS_SET = 0x45
JUMP = 0x05
RETURN = 0x06

# Linux asm-generic/fcntl.h, sockios.h and ioctls.h, shared by both native ABIs.
FCNTL_DENIED_COMMANDS = {
  'F_SETOWN': 8,
  'F_SETSIG': 10,
  'F_SETOWN_EX': 15,
  'F_SETLEASE': 1024,
  'F_NOTIFY': 1026,
  'F_SETPIPE_SZ': 1031,
}
IOCTL_SIGNAL_COMMANDS = {
  'FIOSETOWN': 0x8901,
  'SIOCSPGRP': 0x8902,
  'TIOCSPGRP': 0x5410,
  'TIOCSTI': 0x5412,
  'TIOCSIG': 0x40045436,
  'FIOASYNC': 0x5452,
}
PRCTL_PROTECTED_SETTERS = {
  'PR_SET_PDEATHSIG': PR_SET_PDEATHSIG,
  'PR_SET_DUMPABLE': PR_SET_DUMPABLE,
}
PR_SCHED_CORE = 62
PR_SCHED_CORE_GET = 0
FCNTL_SETFL = 4
O_ASYNC = 0x2000


class Instruction(ctypes.Structure):
  _fields_ = [
    ('code', ctypes.c_ushort),
    ('jt', ctypes.c_ubyte),
    ('jf', ctypes.c_ubyte),
    ('k', ctypes.c_uint),
  ]


class Program(ctypes.Structure):
  _fields_ = [('length', ctypes.c_ushort), ('filter', ctypes.POINTER(Instruction))]


class Ruleset(ctypes.Structure):
  _fields_ = [
    ('handled_access_fs', ctypes.c_uint64),
    ('handled_access_net', ctypes.c_uint64),
    ('scoped', ctypes.c_uint64),
  ]


def _missing(protection: str) -> RuntimeError:
  return RuntimeError(
    f'file sandbox unavailable: missing {protection}: {os.strerror(ctypes.get_errno())}'
  )


def prctl(option: int, value: int, protection: str) -> None:
  if LIBC.prctl(option, ctypes.c_ulong(value), 0, 0, 0) != 0:
    raise _missing(protection)


def arm_parent_death(parent_pid: int, limits: SandboxLimits) -> None:
  if type(parent_pid) is not int or parent_pid <= 0:
    raise ValueError('parent_pid must be a positive integer')
  prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 'parent-death signal')
  if os.getppid() != parent_pid:
    os.kill(os.getpid(), signal.SIGKILL)
  prctl(PR_SET_DUMPABLE, 0, 'dumpable')
  signal.setitimer(signal.ITIMER_REAL, limits.wall_seconds + 2)


def kernel_abi(arch: str) -> int:
  result = LIBC.syscall(TABLES[arch]['landlock_create_ruleset'], 0, 0, 1)
  if result < 1:
    raise _missing('landlock')
  return result


def _limits(limits: SandboxLimits) -> list[str]:
  bounds = {
    'as': (resource.RLIMIT_AS, limits.address_space_bytes, limits.address_space_bytes),
    'cpu': (resource.RLIMIT_CPU, limits.cpu_seconds, limits.cpu_seconds + 2),
    'fsize': (resource.RLIMIT_FSIZE, limits.file_size_bytes, limits.file_size_bytes),
    'nofile': (resource.RLIMIT_NOFILE, limits.open_files, limits.open_files),
    'core': (resource.RLIMIT_CORE, 0, 0),
  }
  for name, (kind, soft, hard) in bounds.items():
    try:
      resource.setrlimit(kind, (soft, hard))
      if resource.getrlimit(kind) != (soft, hard):
        raise RuntimeError(f'file sandbox unavailable: missing rlimits.{name}')
    except (OSError, ValueError) as error:
      raise RuntimeError(f'file sandbox unavailable: missing rlimits.{name}: {error}') from error
  return list(bounds)


@contextlib.contextmanager
def _ruleset(arch: str, attributes: Ruleset, size: int) -> Iterator[int]:
  descriptor = LIBC.syscall(
    TABLES[arch]['landlock_create_ruleset'],
    ctypes.byref(attributes),
    size,
    0,
  )
  if descriptor < 0:
    raise _missing('landlock')
  try:
    yield descriptor
  finally:
    os.close(descriptor)


def landlock(arch: str, abi: int) -> list[str]:
  # linux/landlock.h: REFER (ABI 2), TRUNCATE (3), IOCTL_DEV (5).
  filesystem_bits = 13 + (abi >= 2) + (abi >= 3) + (abi >= 5)
  attributes = Ruleset((1 << filesystem_bits) - 1, 3 if abi >= 4 else 0, 3 if abi >= 6 else 0)
  size = 24 if abi >= 6 else 16 if abi >= 4 else 8
  with _ruleset(arch, attributes, size) as descriptor:
    if LIBC.syscall(TABLES[arch]['landlock_restrict_self'], descriptor, 0) != 0:
      raise _missing('landlock')
  return ['fs', *(['tcp'] if abi >= 4 else []), *(['scope'] if abi >= 6 else [])]


def seccomp_filter(arch: str, pid: int) -> list[Instruction]:
  instructions = [
    Instruction(LOAD, 0, 0, 4),
    Instruction(EQUAL, 1, 0, AUDIT_ARCHITECTURES[arch]),
    Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
    Instruction(LOAD, 0, 0, 0),
    Instruction(BITS_SET, 0, 1, 0x40000000),
    Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
  ]
  table = TABLES[arch]
  jumps = []
  for number in sorted(table.values()):
    instructions.append(Instruction(EQUAL, 0, 1, number))
    jumps.append(len(instructions))
    instructions.append(Instruction(JUMP, 0, 0, 0))
  instructions.append(Instruction(RETURN, 0, 0, SECCOMP_RET_ERRNO | errno.ENOSYS))
  for index in jumps:
    instructions[index].k = len(instructions) - index - 1

  for name in ('kill', 'tgkill', 'rt_sigqueueinfo', 'rt_tgsigqueueinfo', 'prlimit64'):
    body = []
    arguments = (0, 1) if name in ('tgkill', 'rt_tgsigqueueinfo') else (0,)
    for argument in arguments:
      # Arguments are 64-bit; pid_t aliases with nonzero upper words are refused too.
      body.extend(
        [
          Instruction(LOAD, 0, 0, 20 + 8 * argument),
          Instruction(EQUAL, 1, 0, 0),
          Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
          Instruction(LOAD, 0, 0, 16 + 8 * argument),
        ]
      )
      if name == 'prlimit64':
        body.append(Instruction(EQUAL, 2, 0, 0))
      body.extend(
        [
          Instruction(EQUAL, 1, 0, pid),
          Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
        ]
      )
    if name == 'prlimit64':
      for offset in (32, 36):
        body.extend(
          [
            Instruction(LOAD, 0, 0, offset),
            Instruction(EQUAL, 1, 0, 0),
            Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
          ]
        )
    body.append(Instruction(RETURN, 0, 0, SECCOMP_RET_ALLOW))
    instructions.append(Instruction(EQUAL, 0, len(body), table[name]))
    instructions.extend(body)
  for name, commands, offset in (
    ('fcntl', FCNTL_DENIED_COMMANDS, 24),
    ('ioctl', IOCTL_SIGNAL_COMMANDS, 24),
    ('prctl', PRCTL_PROTECTED_SETTERS, 16),
  ):
    body = [Instruction(LOAD, 0, 0, offset)]
    for command in commands.values():
      body.extend(
        [
          Instruction(EQUAL, 0, 1, command),
          Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
        ]
      )
    if name == 'fcntl':
      body.extend(
        [
          Instruction(EQUAL, 0, 3, FCNTL_SETFL),
          Instruction(LOAD, 0, 0, 32),
          Instruction(BITS_SET, 0, 1, O_ASYNC),
          Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
        ]
      )
    if name == 'prctl':
      body.extend(
        [
          Instruction(EQUAL, 0, 3, PR_SCHED_CORE),
          Instruction(LOAD, 0, 0, 24),
          Instruction(EQUAL, 1, 0, PR_SCHED_CORE_GET),
          Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
        ]
      )
    body.append(Instruction(RETURN, 0, 0, SECCOMP_RET_ALLOW))
    instructions.append(Instruction(EQUAL, 0, len(body), table[name]))
    instructions.extend(body)
  for name, number in table.items():
    if CLASSIFICATION[name] == 'denied':
      instructions.extend(
        [
          Instruction(EQUAL, 0, 1, number),
          Instruction(RETURN, 0, 0, SECCOMP_RET_KILL_PROCESS),
        ]
      )
  instructions.append(Instruction(RETURN, 0, 0, SECCOMP_RET_ALLOW))
  return instructions


def install_filter(arch: str, instructions: list[Instruction]) -> None:
  array = (Instruction * len(instructions))(*instructions)
  program = Program(len(array), array)
  if (
    LIBC.syscall(TABLES[arch]['seccomp'], 1, SECCOMP_FILTER_FLAG_TSYNC, ctypes.byref(program)) != 0
  ):
    raise _missing('seccomp')


def apply(limits: SandboxLimits, arch: str, *, _abi_cap: int | None = None) -> SandboxReport:
  try:
    status = Path('/proc/self/status').read_text()
  except OSError as error:
    raise RuntimeError(f'file sandbox unavailable: missing single thread: {error}') from error
  threads = [line.split()[1] for line in status.splitlines() if line.startswith('Threads:')]
  if threads != ['1']:
    raise RuntimeError('file sandbox unavailable: missing single thread')
  if ctypes.sizeof(ctypes.c_void_p) != 8:
    raise RuntimeError(f'no confinement backend for linux/{arch}/32-bit')
  abi = kernel_abi(arch)
  used_abi = min(abi, MAXIMUM_LANDLOCK_ABI)
  if _abi_cap is not None:
    if not 1 <= _abi_cap <= used_abi:
      raise ValueError('invalid test Landlock ABI cap')
    used_abi = _abi_cap
  rlimits = _limits(limits)
  prctl(PR_SET_NO_NEW_PRIVS, 1, 'no_new_privs')
  protections = landlock(arch, used_abi)
  install_filter(arch, seccomp_filter(arch, os.getpid()))
  return SandboxReport(
    backend='linux',
    arch=arch,
    kernel_abi=abi,
    landlock_abi=used_abi,
    landlock=protections,
    seccomp=True,
    rlimits=rlimits,
  )
