"""a `claude` standing in for print mode over stream-json, for tests of the runs
that drive one: it binds the first user message as `prompt` and answers with
`result(...)`, `tasks(...)` and `hook(...)` events from the script it is given;
`next_message()` reads the next user message, None once stdin closes."""

import os
from pathlib import Path

_PRELUDE = """#!/usr/bin/env python3
import json, os, signal, sys, time


def emit(**event):
  sys.stdout.write(json.dumps(event) + "\\n")
  sys.stdout.flush()


def tasks(*ids, ambient=()):
  emit(
    type="system",
    subtype="background_tasks_changed",
    tasks=[
      {"task_id": i, "task_type": "local_bash", "description": f"work {i}", **({"ambient": True} if i in ambient else {})}
      for i in (*ids, *ambient)
    ],
  )


def hook(event, exit_code, stderr="", stdout=""):
  emit(type="system", subtype="hook_response", hook_name=event, hook_event=event, exit_code=exit_code, stdout=stdout, stderr=stderr)


def result(text):
  emit(type="result", subtype="success", result=text)


def next_message():
  line = sys.stdin.readline()
  return None if line == "" else json.loads(line)["message"]["content"]


prompt = next_message()
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
