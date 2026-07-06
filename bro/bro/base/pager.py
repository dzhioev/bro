"""pipe long CLI output through the user's pager."""

import os
import shutil
import subprocess
import sys
from contextlib import suppress


def page(text: str) -> None:
  """write `text` through $PAGER (default `less -FRX` when less is available),
  falling back to plain stdout when no pager can run."""
  pager_command = os.environ.get('PAGER')
  if pager_command is None or len(pager_command.strip()) == 0:
    pager_command = 'less -FRX' if shutil.which('less') is not None else None
  if pager_command is None:
    sys.stdout.write(text)
    return
  try:
    process = subprocess.Popen(pager_command, shell=True, stdin=subprocess.PIPE)
  except FileNotFoundError:
    sys.stdout.write(text)
    return
  assert process.stdin is not None
  # the pager may quit before consuming everything (`q` in less); the resulting
  # broken pipe on write or close is a normal end, not an error
  with suppress(BrokenPipeError), process:
    process.stdin.write(text.encode('utf-8'))
