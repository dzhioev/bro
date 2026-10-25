import errno
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
from contextlib import contextmanager

import pytest

from bro.files.confinement import MAXIMUM_LANDLOCK_ABI, SandboxLimits
from bro.files.confinement.classification import CLASSIFICATION
from bro.files.confinement.tables import TABLES

HELPER = 'bro.files.confinement_probe_test_helper'
ARCH = os.uname().machine


def probe(action, **arguments):
  return subprocess.run(
    [sys.executable, '-m', HELPER, json.dumps({'action': action, **arguments})],
    input=b'pipe input',
    capture_output=True,
    timeout=15,
  )


def result(process):
  assert process.returncode == 0, (process.returncode, process.stderr)
  return json.loads(process.stdout)


@pytest.fixture(scope='module', params=range(1, MAXIMUM_LANDLOCK_ABI + 1))
def abi(request):
  report = result(probe('report'))
  if request.param > report['kernel_abi']:
    pytest.skip('kernel does not implement this Landlock ABI')
  return request.param


@pytest.fixture
def decoy(tmp_path):
  path = tmp_path / 'decoy'
  path.write_text('private decoy')
  return path


@contextmanager
def decoy_process():
  script = 'import signal; print("ready", flush=True); signal.pause()'
  with subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE) as process:
    try:
      assert process.stdout is not None
      assert process.stdout.readline() == b'ready\n'
      yield process
    finally:
      process.terminate()
      process.wait(timeout=5)


def test_implemented_protections_report_the_selected_abi(abi):
  report = result(probe('report', abi=abi))
  assert report['landlock_abi'] == abi
  assert report['landlock'] == [
    'fs',
    *(['tcp'] if abi >= 4 else []),
    *(['scope'] if abi >= 6 else []),
  ]


def test_stdin_stdout_and_anonymous_executable_memory_work(abi):
  assert result(probe('io', abi=abi)) == {'input': 'pipe input', 'memory': 'executable'}


@pytest.mark.parametrize('path', ['/etc/passwd', '/proc/self/environ'])
def test_host_files_are_unreadable(abi, path):
  assert result(probe('open', abi=abi, path=path)) == {'errno': errno.EACCES}


def test_decoy_files_are_unreadable_but_the_negative_control_can_open_them(abi, decoy):
  assert result(probe('open', unconfined=True, path=str(decoy))) == 'opened'
  assert result(probe('open', abi=abi, path=str(decoy))) == {'errno': errno.EACCES}


@pytest.mark.parametrize('action', ['create', 'truncate', 'rename', 'link'])
def test_files_cannot_be_created_or_changed(abi, decoy, action):
  target = str(decoy.parent / 'target')
  if action in ('create', 'truncate'):
    process = probe(
      'open',
      abi=abi,
      path=target if action == 'create' else str(decoy),
      flags=os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    )
  else:
    process = probe(action, abi=abi, path=str(decoy), target=target)
  assert result(process) == {'errno': errno.EACCES}
  assert decoy.read_text() == 'private decoy'
  assert not os.path.exists(target)


DENIED = sorted(
  name for name, decision in CLASSIFICATION.items() if decision == 'denied' and name in TABLES[ARCH]
)


@pytest.mark.parametrize('name', DENIED)
def test_every_denied_syscall_kills_the_process(abi, name):
  process = probe('syscall', abi=abi, name=name)
  # Some kernels exempt uretprobe from seccomp; direct callers then get SIGILL.
  expected = (-signal.SIGSYS, -signal.SIGILL) if name == 'uretprobe' else (-signal.SIGSYS,)
  assert process.returncode in expected, (name, process.stdout, process.stderr)


@pytest.mark.parametrize('family', list(socket.AddressFamily))
def test_every_socket_family_is_denied(abi, family):
  assert probe('socket', abi=abi, family=family).returncode == -signal.SIGSYS


@pytest.mark.parametrize('action', ['exec', 'fork', 'thread', 'setsid'])
def test_process_and_thread_creation_and_session_escape_kill(abi, action):
  assert probe(action, abi=abi).returncode == -signal.SIGSYS


@pytest.mark.parametrize(
  'name', ['kill', 'tgkill', 'rt_sigqueueinfo', 'rt_tgsigqueueinfo', 'prlimit64']
)
def test_another_process_cannot_be_signalled_or_have_its_limits_changed(abi, name):
  with decoy_process() as process:
    if name == 'kill':
      assert (
        result(probe('syscall', unconfined=True, name=name, arguments=[process.pid, 0]))['result']
        == 0
      )
    arguments = (
      [process.pid, process.pid, 0, 0]
      if name in ('tgkill', 'rt_tgsigqueueinfo')
      else [process.pid, 0, 0, 0]
    )
    assert probe('syscall', abi=abi, name=name, arguments=arguments).returncode == -signal.SIGSYS
    assert process.poll() is None


@pytest.mark.parametrize('target', [0, -1, -100, (1 << 32) + 1])
def test_pid_aliases_and_process_groups_are_not_self(abi, target):
  assert probe('syscall', abi=abi, name='kill', arguments=[target, 0]).returncode == -signal.SIGSYS


def test_native_abort_can_signal_itself(abi):
  assert probe('abort', abi=abi).returncode == -signal.SIGABRT


def test_resource_limits_cannot_be_raised(abi):
  assert probe('raise_limit', abi=abi).returncode == -signal.SIGSYS


def test_cpu_overrun_is_sigxcpu_not_the_hard_limit_sigkill(abi):
  assert (
    probe('cpu', abi=abi, limits={'cpu_seconds': 1, 'wall_seconds': 10}).returncode
    == -signal.SIGXCPU
  )


@pytest.mark.parametrize(
  'number',
  [
    next(
      number for number in range(max(TABLES[ARCH].values())) if number not in TABLES[ARCH].values()
    ),
    max(TABLES[ARCH].values()) + 1,
    10000,
  ],
)
def test_unassigned_syscalls_return_enosys_including_holes(abi, number):
  assert result(probe('syscall', abi=abi, number=number)) == {'result': -1, 'errno': errno.ENOSYS}


def test_x32_flagged_numbers_kill(abi):
  assert (
    probe('syscall', abi=abi, number=0x40000000 | TABLES[ARCH]['getpid']).returncode
    == -signal.SIGSYS
  )


@pytest.mark.skipif(ARCH != 'x86_64', reason='x86 instruction')
def test_int80_foreign_audit_architecture_kills(abi):
  assert probe('int80', abi=abi).returncode == -signal.SIGSYS


@pytest.mark.parametrize(
  'name', ['landlock_create_ruleset', 'landlock_restrict_self', 'seccomp', 'prctl']
)
def test_a_stacked_filter_removing_a_required_syscall_fails_closed(name):
  assert (
    'file sandbox unavailable: missing' in result(probe('report', remove_syscall=name))['error']
  )


def test_an_existing_native_thread_fails_closed():
  assert result(probe('report', extra_thread=True)) == {
    'error': 'file sandbox unavailable: missing single thread'
  }


def test_a_parent_pid_mismatch_kills_the_worker():
  assert probe('report', parent_pid=1).returncode == -signal.SIGKILL


def test_the_wall_clock_backstop_is_armed():
  assert probe('alarm', limits={'wall_seconds': 0.01}).returncode == -signal.SIGALRM


@pytest.mark.parametrize('action', ['chmod', 'utime'])
def test_file_attributes_cannot_be_changed(abi, decoy, action):
  before = decoy.stat()
  assert probe(action, abi=abi, path=str(decoy)).returncode == -signal.SIGSYS
  after = decoy.stat()
  assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)


@pytest.mark.parametrize(
  'family,kind',
  [
    (socket.AF_INET, socket.SOCK_STREAM),
    (socket.AF_INET, socket.SOCK_DGRAM),
    (socket.AF_INET6, socket.SOCK_STREAM),
    (socket.AF_UNIX, socket.SOCK_STREAM),
  ],
)
def test_connect_on_an_existing_socket_cannot_reach_a_listener(abi, tmp_path, family, kind):
  with socket.socket(family, kind) as listener:
    listener.bind(
      str(tmp_path / 'listener')
      if family == socket.AF_UNIX
      else ('::1', 0)
      if family == socket.AF_INET6
      else ('127.0.0.1', 0)
    )
    if kind == socket.SOCK_STREAM:
      listener.listen(8)
    arguments = {'family': family, 'kind': kind, 'address': listener.getsockname()}
    assert result(probe('connect', unconfined=True, **arguments)) == 'connected'
    assert probe('connect', abi=abi, **arguments).returncode == -signal.SIGSYS


def test_ptrace_cannot_attach_to_a_decoy(abi):
  with decoy_process() as process:
    assert (
      probe('syscall', abi=abi, name='ptrace', arguments=[16, process.pid, 0, 0]).returncode
      == -signal.SIGSYS
    )
    assert process.poll() is None


@pytest.mark.parametrize(
  'name,arguments',
  [
    ('kill', ['@self', 0]),
    ('tgkill', ['@self', '@self', 0]),
    ('rt_sigqueueinfo', ['@self', 0, 0]),
    ('rt_tgsigqueueinfo', ['@self', '@self', 0, 0]),
    ('prlimit64', [0, 0, 0, 0]),
    ('prlimit64', ['@self', 0, 0, 0]),
  ],
)
def test_self_signals_and_self_limit_queries_remain_available(abi, name, arguments):
  value = result(probe('syscall', abi=abi, name=name, arguments=arguments))
  assert value['result'] == 0 or value['errno'] == errno.EFAULT


def test_the_reported_limits_reach_the_kernel(abi):
  limits = SandboxLimits()
  value = result(probe('limits', abi=abi))
  assert value == {
    'as': [limits.address_space_bytes] * 2,
    'cpu': [limits.cpu_seconds, limits.cpu_seconds + 2],
    'fsize': [limits.file_size_bytes] * 2,
    'nofile': [limits.open_files] * 2,
    'core': [0, 0],
  }


def test_address_space_is_bounded(abi):
  assert result(probe('memory', abi=abi)) == {'errno': errno.ENOMEM}


def test_open_descriptors_are_bounded(abi):
  assert result(probe('descriptors', abi=abi)) == {'errno': errno.EMFILE}


def test_writes_on_an_inherited_file_are_bounded(abi, decoy):
  assert (
    probe('file_size', abi=abi, path=str(decoy), limits={'file_size_bytes': 4096}).returncode
    == -signal.SIGXFSZ
  )
  assert decoy.stat().st_size <= 4096


def test_a_worker_dies_when_its_spawning_parent_exits():
  assert result(probe('parent_death')) == {'returncode': -signal.SIGKILL}


@pytest.mark.parametrize('resource', range(16))
def test_limit_mutations_are_blocked_before_the_kernel_can_use_capabilities(abi, resource):
  assert (
    probe('syscall', abi=abi, name='prlimit64', arguments=[0, resource, 1, 0]).returncode
    == -signal.SIGSYS
  )


@pytest.mark.parametrize('method', ['owner', 'owner_ex', 'signal', 'async_flags'])
def test_async_io_cannot_redirect_a_signal_to_a_decoy(abi, method):
  with decoy_process() as process:
    assert (
      probe('async_signal', abi=abi, method=method, pid=process.pid).returncode == -signal.SIGSYS
    )
    assert process.poll() is None


def test_an_unconfined_async_io_negative_control_signals_only_a_decoy():
  with decoy_process() as process:
    assert (
      result(probe('async_signal', unconfined=True, method='owner', pid=process.pid)) == 'returned'
    )
    assert process.wait(timeout=5) == -signal.SIGUSR1


@pytest.mark.parametrize('command', [0x8901, 0x8902, 0x5410, 0x5412, 0x40045436, 0x5452])
def test_native_ioctl_signal_commands_are_denied(abi, command):
  assert (
    probe('syscall', abi=abi, name='ioctl', arguments=[0, command, 0]).returncode == -signal.SIGSYS
  )


def test_ordinary_descriptor_flags_and_ioctl_queries_still_work(abi):
  assert result(probe('descriptor_control', abi=abi)) == {'blocking': False, 'inheritable': False}


@pytest.mark.parametrize(
  'command',
  [fcntl.F_SETOWN, fcntl.F_SETSIG, 15, fcntl.F_SETLEASE, fcntl.F_NOTIFY, fcntl.F_SETPIPE_SZ],
)
@pytest.mark.parametrize('upper_word', [0, 1 << 32])
def test_native_fcntl_restricted_commands_and_integer_aliases_are_denied(abi, command, upper_word):
  assert (
    probe('syscall', abi=abi, name='fcntl', arguments=[0, command | upper_word, 0]).returncode
    == -signal.SIGSYS
  )


@pytest.mark.parametrize('option,value', [(1, 0), (4, 1)])
@pytest.mark.parametrize('upper_word', [0, 1 << 32])
def test_setup_prctl_protections_cannot_be_reset_or_aliased(abi, option, value, upper_word):
  assert (
    probe(
      'syscall', abi=abi, name='prctl', arguments=[option | upper_word, value, 0, 0, 0]
    ).returncode
    == -signal.SIGSYS
  )


def test_prctl_queries_observe_the_worker_backstops(abi):
  assert result(probe('backstops', abi=abi)) == {
    'death_signal': signal.SIGKILL,
    'dumpable': 0,
    'no_new_privs': 1,
  }


@pytest.mark.parametrize('command', [1, 2, 3])
def test_core_scheduling_cannot_mutate_a_decoy(abi, command):
  with decoy_process() as process:
    assert (
      probe('syscall', abi=abi, name='prctl', arguments=[62, command, process.pid, 0, 0]).returncode
      == -signal.SIGSYS
    )
    assert process.poll() is None


@pytest.mark.parametrize(
  'arguments',
  [
    [15, 'parser-worker', 0, 0, 0],
    [36, 1, 0, 0, 0],
    [35, 0, 0, 0, 0],
    [0x59616D61, 0, 0, 0, 0],
    [62, 0, 0, 0, 0],
  ],
)
def test_worker_local_prctl_operations_and_read_only_core_queries_remain_available(abi, arguments):
  value = result(probe('syscall', abi=abi, name='prctl', arguments=arguments))
  assert value['result'] == 0 or value['errno'] in (
    errno.EINVAL,
    errno.EPERM,
    errno.EFAULT,
    errno.ENODEV,
  )


def test_prctl_cannot_disable_the_installed_seccomp_filter(abi):
  process = probe('disable_filter', abi=abi)
  assert process.returncode == -signal.SIGSYS
  assert json.loads(process.stdout) == {'result': -1, 'errno': errno.EINVAL}


def test_prctl_can_only_stack_a_stronger_seccomp_filter(abi):
  assert result(probe('stack_filter', abi=abi)) == {
    'installed': 0,
    'result': -1,
    'errno': errno.ENOSYS,
  }


def test_pipe_resizing_is_denied_but_queries_remain_available(abi):
  assert probe('pipe_size', abi=abi).returncode == -signal.SIGSYS
  assert result(probe('pipe_query', abi=abi)) > 0


def test_an_unconfined_pipe_resize_negative_control_is_bounded_to_one_page():
  value = result(probe('pipe_size', unconfined=True))
  assert value['after'] == os.sysconf('SC_PAGESIZE')
  assert value['before'] >= value['after']
