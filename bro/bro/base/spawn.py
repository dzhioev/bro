"""subprocess wrappers that spawn children unable to block on interactive input.

Every child runs in a fresh session (`start_new_session=True`), detaching it from
any controlling terminal so a `/dev/tty` open fails with ENXIO instead of blocking.
stdin defaults to /dev/null.
"""

import subprocess


def run(cmd, **kwargs) -> subprocess.CompletedProcess:
  # `input` and `stdin` are mutually exclusive in subprocess.run; only default
  # stdin to /dev/null when the caller hasn't supplied input to feed in.
  if 'input' not in kwargs:
    kwargs.setdefault('stdin', subprocess.DEVNULL)
  kwargs['start_new_session'] = True
  return subprocess.run(cmd, **kwargs)


def popen(cmd, **kwargs) -> subprocess.Popen:
  kwargs.setdefault('stdin', subprocess.DEVNULL)
  kwargs['start_new_session'] = True
  return subprocess.Popen(cmd, **kwargs)
