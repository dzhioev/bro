import contextlib
import fcntl
import os
import subprocess
import sys
import threading
from collections.abc import Iterator

import pytest

import ride.workspace.docker as workspace_docker
import ride.workspace.image_locks as image_locks


@contextlib.contextmanager
def _lock_process(source: str, environment: dict[str, str]) -> Iterator[subprocess.Popen[str]]:
  process = subprocess.Popen(
    [sys.executable, '-c', source],
    env={**os.environ, **environment},
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
  )
  try:
    assert process.stdout is not None
    assert process.stdout.readline() == 'ready\n'
    yield process
  finally:
    if process.poll() is None:
      assert process.stdin is not None
      process.stdin.write('\n')
      process.stdin.flush()
    try:
      process.wait(timeout=5)
    except subprocess.TimeoutExpired:
      process.kill()
      process.wait()
      raise


def test_lock_paths_pin_the_cross_version_disk_contract(monkeypatch, tmp_path):
  monkeypatch.setattr(image_locks, 'runtime_base', lambda: tmp_path)

  tag_directory = tmp_path / 'image-locks' / 'YnJvL3JpZGUtcnVudGltZTphYmM'
  assert image_locks.image_lock_path('bro/ride-runtime:abc') == tag_directory / 'tag.lock'
  assert image_locks.build_lock_path('bro/ride-runtime:abc') == tag_directory / 'build.lock'
  assert image_locks.image_lock_path('bro/a:b') != image_locks.image_lock_path('bro-a:b')


def test_lock_paths_support_a_long_valid_project_image_tag(monkeypatch, tmp_path):
  monkeypatch.setattr(image_locks, 'runtime_base', lambda: tmp_path)
  repository = '/'.join(['a' * 60] * 3)
  tag = f'{repository}:{"b" * 12}'
  tag_directory = (
    tmp_path
    / 'image-locks'
    / 'YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhL2FhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFh'
    / 'YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYS9hYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE6YmJiYmJiYmJi'
    / 'YmJi'
  )

  assert image_locks.image_lock_path(tag) == tag_directory / 'tag.lock'
  assert image_locks.build_lock_path(tag) == tag_directory / 'build.lock'
  with image_locks.image_build_lock(tag):
    pass
  with image_locks.image_removal_lock(tag) as reserved:
    assert reserved

  assert image_locks.image_lock_path(tag).is_file()
  assert image_locks.build_lock_path(tag).is_file()


def test_tag_held_by_another_process_survives_pruning(monkeypatch):
  tag = 'bro/cross-process-held:old'
  source = """
import os
from ride.workspace.image_locks import reserve_image
reserve_image(os.environ['IMAGE_TAG'])
print('ready', flush=True)
input()
"""
  removals: list[str] = []

  def run(arguments, **_keywords):
    if arguments[:2] == ['docker', 'images']:
      return subprocess.CompletedProcess(arguments, 0, stdout=f'{tag}\n')
    removals.append(arguments[-1])
    return subprocess.CompletedProcess(arguments, 0, stdout='')

  with _lock_process(source, {'IMAGE_TAG': tag}):
    monkeypatch.setattr(workspace_docker.subprocess, 'run', run)
    workspace_docker.prune_superseded_images('bro/cross-process-held:current')

  assert removals == []


def test_build_lock_serializes_processes_for_one_tag():
  tag = 'bro/cross-process-build:one'
  source = """
import os
from ride.workspace.image_locks import image_build_lock
with image_build_lock(os.environ['IMAGE_TAG']):
  print('ready', flush=True)
  input()
"""
  with _lock_process(source, {'IMAGE_TAG': tag}) as process:
    path = image_locks.build_lock_path(tag)
    with path.open('a+b') as handle:
      with pytest.raises(BlockingIOError):
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert process.stdin is not None
    process.stdin.write('\n')
    process.stdin.flush()
    process.wait(timeout=5)

  with image_locks.image_build_lock(tag):
    pass


def test_ensure_waits_for_removal_then_rebuilds(monkeypatch, tmp_path):
  tag = 'bro/cross-process-removal:one'
  marker = tmp_path / 'image-present'
  marker.touch()
  source = """
import os
from pathlib import Path
from ride.workspace.image_locks import image_removal_lock
with image_removal_lock(os.environ['IMAGE_TAG']) as reserved:
  assert reserved
  print('ready', flush=True)
  input()
  Path(os.environ['IMAGE_MARKER']).unlink()
"""
  ensure_started = threading.Event()
  built = threading.Event()

  def build(_tag: str, _python_version: str) -> None:
    marker.touch()
    built.set()

  monkeypatch.setattr(workspace_docker, 'image_present', lambda _tag: marker.exists())
  monkeypatch.setattr(workspace_docker, 'build_runtime_image', build)
  monkeypatch.setattr(
    workspace_docker,
    'prune_superseded_images',
    lambda _tag: None,
  )

  def ensure() -> None:
    ensure_started.set()
    workspace_docker.ensure_runtime_image(tag, '3.12')

  with _lock_process(source, {'IMAGE_TAG': tag, 'IMAGE_MARKER': str(marker)}) as process:
    thread = threading.Thread(target=ensure)
    thread.start()
    assert ensure_started.wait(timeout=5)
    path = image_locks.image_lock_path(tag)
    with path.open('a+b') as handle:
      with pytest.raises(BlockingIOError):
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
    assert process.stdin is not None
    process.stdin.write('\n')
    process.stdin.flush()
    process.wait(timeout=5)
    assert built.wait(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()

  assert marker.is_file()
