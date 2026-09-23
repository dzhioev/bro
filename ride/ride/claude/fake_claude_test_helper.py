"""a `claude` standing in for print mode over stream-json, for tests of the runs
that drive one: it binds the first user message as `prompt` and answers with
`result(...)` and `tasks(...)` events from the script it is given."""

import os
from pathlib import Path

_PRELUDE = """#!/usr/bin/env python3
import json, os, signal, sys, time


def emit(**event):
  sys.stdout.write(json.dumps(event) + "\\n")
  sys.stdout.flush()


def tasks(*ids):
  emit(type="system", subtype="background_tasks_changed", tasks=[{"task_id": i} for i in ids])


def result(text):
  emit(type="result", subtype="success", result=text)


prompt = json.loads(sys.stdin.readline())["message"]["content"]
"""


def write_fake_claude(path: Path, script: str) -> Path:
  path.write_text(_PRELUDE + script)
  path.chmod(0o755)
  return path


def fake_claude_argv(tmp_path: Path, script: str) -> list[str]:
  """the fake as an argv to run directly."""
  return [str(write_fake_claude(tmp_path / 'claude', script))]


def fake_claude_env(tmp_path: Path, script: str) -> dict[str, str]:
  """an environment whose PATH resolves `claude` to the fake."""
  bin_dir = tmp_path / 'bin'
  bin_dir.mkdir(exist_ok=True)
  write_fake_claude(bin_dir / 'claude', script)
  return {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
