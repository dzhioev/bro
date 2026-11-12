import contextlib
import os
import select
import signal
import subprocess
import sys

import pytest

# Supervises a command that leaves one descendant in a session of its own, named
# in bytes that are neither UTF-8 nor one line, which every sweep misses (`missed`)
# or may not kill (`refused`), and asks for a kill.
# Prints the descendant's pid, `waiting` whenever the supervision blocks, then
# what the supervision wrote to the command's output, then `returned`.
_SUPERVISION = r"""
import errno, os, select, sys, types
from bro import job_supervisor

# 15 is PR_SET_NAME
NAMED_DESCENDANT = (
  "import ctypes, os, time; ctypes.CDLL(None).prctl(15, b'\\xff\\nescaped', 0, 0, 0); "
  'print(os.getpid(), flush=True); time.sleep(30)'
)
escaped = set()
real_select = select.select
real_kill = os.kill
real_descendants = job_supervisor._descendants


def announced_select(*arguments):
  print('waiting', flush=True)
  return real_select(*arguments)


def refusing_kill(process_id, signal_number):
  if process_id in escaped:
    raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
  real_kill(process_id, signal_number)


job_supervisor.select = types.SimpleNamespace(select=announced_select)
if sys.argv[1] == 'missed':
  job_supervisor._descendants = lambda: [
    descendant for descendant in real_descendants() if descendant.process_id not in escaped
  ]
else:
  os.kill = refusing_kill
assert job_supervisor._become_subreaper()
read_fd, write_fd = os.pipe()
with os.fdopen(read_fd, 'rb') as source:
  with os.fdopen(write_fd, 'wb', buffering=0) as output:
    supervision = job_supervisor._Supervision(
      ['sh', '-c', 'setsid "$0" -c "$1" &', sys.executable, NAMED_DESCENDANT], output, True
    )
    escaped.add(int(source.readline()))
    print(*escaped, flush=True)
    os.kill(os.getpid(), job_supervisor.KILL_SIGNAL)
    supervision.wait()
  sys.stdout.write(source.read().decode())
print('returned', flush=True)
"""


def _kill(process_fd: int) -> None:
  with contextlib.suppress(ProcessLookupError):
    signal.pidfd_send_signal(process_fd, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != 'linux', reason='sweeps list descendants through /proc')
@pytest.mark.parametrize('escape', ['missed', 'refused'])
def test_a_kill_waits_for_a_descendant_its_sweeps_leave_running(escape):
  with contextlib.ExitStack() as cleanup:
    supervisor = cleanup.enter_context(
      subprocess.Popen(
        [sys.executable, '-c', _SUPERVISION, escape], stdout=subprocess.PIPE, text=True
      )
    )
    cleanup.callback(supervisor.kill)
    assert supervisor.stdout is not None
    process_id = int(supervisor.stdout.readline())
    descendant = os.pidfd_open(process_id)
    cleanup.callback(os.close, descendant)
    cleanup.callback(_kill, descendant)
    assert supervisor.stdout.readline() == 'waiting\n'
    assert select.select([descendant], [], [], 0)[0] == []
    signal.pidfd_send_signal(descendant, signal.SIGKILL)
    rest, _ = supervisor.communicate(timeout=10)
    assert supervisor.returncode == 0
  lines = rest.splitlines()
  assert lines[-1] == 'returned'
  naming = [line for line in lines if f'pid {process_id} (' in line]
  assert len(naming) == (1 if escape == 'refused' else 0)
  assert all('escaped)' in line for line in naming)
