"""Scoped credential-store tiers and session materialization.

A container receives an in-memory tar in its own layer;
an unboxed session points `BRO_STORE` at a materialized directory.
The launch surface owns which kinds enter each tier.
"""

import io
import os
import shutil
import tarfile
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from bro.base import credentials, log


@dataclass(frozen=True)
class ScopedSecrets:
  """a session launch's kinds-only credential scope and instance selection.

  required is hydrated strictly (a missing secret fails launch); optional is the
  best-effort tier (skipped when unresolvable).
  """

  required: set[str]
  optional: set[str]
  selection: dict[str, str] = field(default_factory=dict)

  def __post_init__(self) -> None:
    for name in self.required | self.optional:
      kind, instance = credentials.parse_name(name)
      if instance is not None:
        raise ValueError(
          f'credential scope entry {name!r} names an instance; use kind {kind!r} '
          'and put its instance in the selection'
        )


def log_scoped_secrets(subject: str, required: Collection[str], optional: Collection[str]) -> None:
  """log a launch's credential scope at every scoped-store launch path."""
  names = sorted(set(required))
  log.info('scoped secrets for %s: %s', subject, ', '.join(names) if len(names) > 0 else '(none)')
  optional_names = sorted(set(optional) - set(required))
  if len(optional_names) > 0:
    log.info('optional (best-effort) secrets for %s: %s', subject, ', '.join(optional_names))


def materialize_scoped_store(files: dict[str, bytes], directory: Path) -> Path:
  """Write a scoped credential store and return its exclusive directory.

  The directory is recreated so a credential dropped from the scope cannot
  linger from an earlier launch.
  """
  log.verbose('materializing the scoped credential store at %s', directory)
  if directory.exists():
    shutil.rmtree(directory)
  directory.mkdir(parents=True)
  directory.chmod(0o700)
  for filename, data in files.items():
    file = directory / filename
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)
    file.chmod(0o600)
  return directory


def store_tarball(files: dict[str, bytes], root: PurePosixPath) -> bytes:
  """pack a scoped credential store into a tar for `docker cp` into /home/ride.

  Entries are prefixed with `root` (a relative path) so extracting at /home/ride
  lands them at /home/ride/<root>/<file>. files are 0600, the dirs 0700, all owned
  by the host uid/gid (the same uid the entrypoint remaps `ride` to on Linux); the
  receiving side re-owns the tree to `ride` after that remap so the bytes are
  readable there and on Docker for Mac (where the remap is skipped). mtime
  defaults to 0 — deterministic, no clock needed.
  """
  if root.is_absolute():
    raise ValueError(f'store tar root must be relative, not {root}')
  uid, gid = os.getuid(), os.getgid()
  buffer = io.BytesIO()

  def add_directory(tar: tarfile.TarFile, path: PurePosixPath) -> None:
    info = tarfile.TarInfo(str(path))
    info.type = tarfile.DIRTYPE
    info.mode = 0o700
    info.uid, info.gid = uid, gid
    tar.addfile(info)

  with tarfile.open(fileobj=buffer, mode='w') as tar:
    for directory in (*reversed(root.parents[:-1]), root, root / 'creds'):
      add_directory(tar, directory)
    for filename in sorted(files):
      data = files[filename]
      info = tarfile.TarInfo(str(root / filename))
      info.size = len(data)
      info.mode = 0o600
      info.uid, info.gid = uid, gid
      tar.addfile(info, io.BytesIO(data))
  return buffer.getvalue()
