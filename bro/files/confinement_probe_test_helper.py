import ctypes
import errno
import fcntl
import json
import mmap
import os
import resource
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
from contextlib import ExitStack

from bro.files import confinement
from bro.files.confinement import linux
from bro.files.confinement.tables import TABLES


def raw_syscall(number: int, arguments: list[int | str]) -> int:
  converted = [
    ctypes.c_long(os.getpid())
    if value == '@self'
    else ctypes.c_char_p(value.encode())
    if isinstance(value, str)
    else ctypes.c_long(value)
    for value in arguments
  ]
  return linux.LIBC.syscall(number, *converted)


def attempt(request: dict) -> object:
  action = request['action']
  if action == 'syscall':
    table = TABLES[os.uname().machine]
    number = request['number'] if 'number' in request else table[request['name']]
    result = raw_syscall(number, request.get('arguments', [0] * 6))
    return {'result': result, 'errno': ctypes.get_errno()}
  if action == 'open':
    flags = request.get('flags', os.O_RDONLY)
    with os.fdopen(os.open(request['path'], flags, 0o600), 'wb' if flags & os.O_WRONLY else 'rb'):
      return 'opened'
  if action == 'rename':
    os.rename(request['path'], request['target'])
  elif action == 'link':
    os.link(request['path'], request['target'])
  elif action == 'chmod':
    os.chmod(request['path'], 0o777)
  elif action == 'utime':
    os.utime(request['path'], (0, 0))
  elif action == 'connect':
    request['_socket'].connect(request['address'])
    return 'connected'
  elif action in ('async_signal', 'descriptor_control'):
    read_descriptor, write_descriptor = os.pipe()
    with os.fdopen(read_descriptor, 'rb') as reader, os.fdopen(write_descriptor, 'wb') as writer:
      if action == 'descriptor_control':
        os.set_blocking(read_descriptor, False)
        os.set_inheritable(read_descriptor, False)
        fcntl.ioctl(read_descriptor, termios.FIONREAD, bytearray(4), True)
        return {
          'blocking': os.get_blocking(read_descriptor),
          'inheritable': os.get_inheritable(read_descriptor),
        }
      method = request['method']
      if method == 'owner':
        fcntl.fcntl(reader, fcntl.F_SETOWN, request['pid'])
      elif method == 'owner_ex':
        fcntl.fcntl(reader, 15, struct.pack('ii', 1, request['pid']))
      elif method == 'signal':
        fcntl.fcntl(reader, fcntl.F_SETSIG, signal.SIGUSR1)
      elif method == 'async_flags':
        fcntl.fcntl(reader, fcntl.F_SETFL, os.O_ASYNC)
      else:
        raise ValueError(f'unknown signal method: {method}')
      fcntl.fcntl(reader, fcntl.F_SETSIG, signal.SIGUSR1)
      fcntl.fcntl(reader, fcntl.F_SETFL, os.O_ASYNC)
      os.write(writer.fileno(), b'signal decoy')
  elif action in ('pipe_size', 'pipe_query'):
    read_descriptor, write_descriptor = os.pipe()
    with os.fdopen(read_descriptor, 'rb') as reader, os.fdopen(write_descriptor, 'wb'):
      before = fcntl.fcntl(reader, fcntl.F_GETPIPE_SZ)
      if action == 'pipe_query':
        return before
      after = fcntl.fcntl(reader, fcntl.F_SETPIPE_SZ, mmap.PAGESIZE)
      return {'before': before, 'after': after}
  elif action == 'backstops':
    death_signal = ctypes.c_int()
    if linux.LIBC.prctl(2, ctypes.byref(death_signal), 0, 0, 0) != 0:
      raise OSError(ctypes.get_errno(), 'cannot query parent-death signal')
    return {
      'death_signal': death_signal.value,
      'dumpable': linux.LIBC.prctl(3, 0, 0, 0, 0),
      'no_new_privs': linux.LIBC.prctl(39, 0, 0, 0, 0),
    }
  elif action == 'disable_filter':
    value = linux.LIBC.prctl(22, 0, 0, 0, 0)
    print(json.dumps({'result': value, 'errno': ctypes.get_errno()}), flush=True)
    with socket.socket():
      return 'filter disabled'
  elif action == 'stack_filter':
    number = TABLES[os.uname().machine]['getppid']
    instructions = (linux.Instruction * 4)(
      linux.Instruction(linux.LOAD, 0, 0, 0),
      linux.Instruction(linux.EQUAL, 0, 1, number),
      linux.Instruction(linux.RETURN, 0, 0, linux.SECCOMP_RET_ERRNO | errno.ENOSYS),
      linux.Instruction(linux.RETURN, 0, 0, linux.SECCOMP_RET_ALLOW),
    )
    program = linux.Program(len(instructions), instructions)
    installed = linux.LIBC.prctl(22, 2, ctypes.byref(program), 0, 0)
    value = linux.LIBC.syscall(number)
    return {'installed': installed, 'result': value, 'errno': ctypes.get_errno()}
  elif action == 'memory':
    with mmap.mmap(-1, 2 * confinement.SandboxLimits().address_space_bytes):
      return 'allocated'
  elif action == 'descriptors':
    while True:
      os.dup(0)
  elif action == 'limits':
    return {
      name: resource.getrlimit(kind)
      for name, kind in [
        ('as', resource.RLIMIT_AS),
        ('cpu', resource.RLIMIT_CPU),
        ('fsize', resource.RLIMIT_FSIZE),
        ('nofile', resource.RLIMIT_NOFILE),
        ('core', resource.RLIMIT_CORE),
      ]
    }
  elif action == 'file_size':
    os.write(request['_file'].fileno(), b'x' * 1024 * 1024)
    os.write(request['_file'].fileno(), b'x')
  elif action == 'thread':
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
  elif action == 'fork':
    if os.fork() == 0:
      os._exit(77)
  elif action == 'exec':
    os.execv('/bin/true', ['/bin/true'])
  elif action == 'setsid':
    os.setsid()
  elif action == 'socket':
    with socket.socket(request['family'], request.get('kind', socket.SOCK_STREAM)):
      return 'socket created'
  elif action == 'raise_limit':
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (soft + 1, hard + 1))
  elif action == 'cpu':
    while True:
      pass
  elif action == 'alarm':
    while True:
      signal.pause()
  elif action == 'int80':
    with mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC) as memory:
      memory.write(bytes.fromhex('b814000000cd80c3'))
      function = ctypes.CFUNCTYPE(ctypes.c_int)(ctypes.addressof(ctypes.c_char.from_buffer(memory)))
      function()
  elif action == 'io':
    with mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC) as memory:
      memory.write(b'executable anonymous memory')
      return {'input': sys.stdin.buffer.read().decode(), 'memory': memory[:10].decode()}
  elif action == 'abort':
    os.abort()
  elif action == 'hold':
    print('ready', flush=True)
    signal.pause()
  return 'returned'


def run_probe() -> None:
  request = json.loads(sys.argv[1])
  if request['action'] == 'orphan_launcher':
    with subprocess.Popen(
      [sys.executable, '-m', __spec__.name, json.dumps({'action': 'hold'})],
      stdout=subprocess.PIPE,
    ) as child:
      assert child.stdout is not None
      assert child.stdout.readline() == b'ready\n'
      print(child.pid, flush=True)
      # Process death is the subject: bypass normal child teardown.
      os._exit(0)
  if request['action'] == 'parent_death':
    linux.prctl(36, 1, 'test subreaper')
    parent = subprocess.run(
      [sys.executable, '-m', __spec__.name, json.dumps({'action': 'orphan_launcher'})],
      capture_output=True,
      check=True,
      timeout=10,
    )
    child_pid = int(parent.stdout)
    _, status = os.waitpid(child_pid, 0)
    print(json.dumps({'returncode': os.waitstatus_to_exitcode(status)}), flush=True)
    return
  limits = confinement.SandboxLimits(**request.get('limits', {}))
  arch = os.uname().machine
  with ExitStack() as stack:
    if request['action'] == 'connect':
      request['_socket'] = stack.enter_context(socket.socket(request['family'], request['kind']))
      if request['family'] in (socket.AF_INET, socket.AF_INET6):
        request['address'] = tuple(request['address'])
    if request['action'] == 'file_size':
      signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
      request['_file'] = stack.enter_context(open(request['path'], 'wb'))
    if request.get('extra_thread'):
      finished = threading.Event()
      started = threading.Event()
      thread = threading.Thread(target=lambda: (started.set(), finished.wait()))
      thread.start()
      stack.callback(thread.join)
      stack.callback(finished.set)
      started.wait()
    if request.get('remove_syscall'):
      linux.prctl(linux.PR_SET_NO_NEW_PRIVS, 1, 'no_new_privs')
      linux.install_filter(
        arch,
        [
          linux.Instruction(linux.LOAD, 0, 0, 0),
          linux.Instruction(linux.EQUAL, 0, 1, TABLES[arch][request['remove_syscall']]),
          linux.Instruction(linux.RETURN, 0, 0, linux.SECCOMP_RET_ERRNO | errno.ENOSYS),
          linux.Instruction(linux.RETURN, 0, 0, linux.SECCOMP_RET_ALLOW),
        ],
      )
    try:
      if not request.get('unconfined'):
        confinement.arm_parent_death(request.get('parent_pid', os.getppid()), limits)
        if 'abi' in request:
          report = linux.apply(limits, arch, _abi_cap=request['abi'])
        else:
          report = confinement.apply(limits)
        if request['action'] == 'report':
          print(json.dumps(report), flush=True)
          os._exit(0)
      result = attempt(request)
    except OSError as error:
      result = {'errno': error.errno}
    except (RuntimeError, ValueError) as error:
      print(json.dumps({'error': str(error)}), flush=True)
      return
    print(json.dumps(result), flush=True)
  os._exit(0)


if __name__ == '__main__':
  run_probe()
