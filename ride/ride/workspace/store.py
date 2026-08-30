"""Scoped credential-store tiers and session materialization.

A container receives an in-memory tar in its own layer;
a host session points `BRO_STORE` at a materialized directory.
The launch surface owns which kinds enter each tier.
"""

import io
import os
import shutil
import tarfile
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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


def credential_revoke_kind(name: str) -> str:
  kind, instance = credentials.parse_name(name)
  if instance is not None:
    raise ValueError(
      f'cannot revoke credential instance {name!r}; revoke its kind instead (--revoke {kind})'
    )
  return kind


def finalize_scoped_secrets(
  scoped: ScopedSecrets, *, grant: list[str], revoke: list[str]
) -> ScopedSecrets:
  scoped_kinds = scoped.required | scoped.optional
  grants: dict[str, Optional[str]] = {}
  for name in grant:
    kind, instance = credentials.parse_name(name)
    if kind in grants:
      raise ValueError(f'credential kind {kind!r} is granted more than once')
    grants[kind] = instance

  revoke_kinds = [credential_revoke_kind(name) for name in revoke]

  both = sorted(grants.keys() & set(revoke_kinds))
  if len(both) > 0:
    raise ValueError(f'cannot grant and revoke the same credential kind: {", ".join(both)}')

  selection = dict(scoped.selection)
  required = set(scoped.required)
  optional = set(scoped.optional)
  for kind, instance in grants.items():
    if instance is None:
      if kind in scoped_kinds:
        raise ValueError(f'cannot grant {kind!r}: already in the scoped credential set')
    elif kind in required and selection.get(kind, '') == instance:
      name = credentials.storage_name(kind, instance)
      raise ValueError(f'cannot grant {name!r}: already selected in the scoped credential set')
    else:
      selection[kind] = instance
    required.add(kind)
    optional.discard(kind)

  for kind in revoke_kinds:
    if kind not in required and kind not in optional:
      raise ValueError(f'cannot revoke {kind!r}: not in the scoped credential set')
    required.discard(kind)
    optional.discard(kind)

  return ScopedSecrets(required=required, optional=optional, selection=selection)


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


def _bro_tarball(files: dict[str, bytes]) -> bytes:
  """pack a scoped credential store into a tar for `docker cp` into /home/ride.

  Entries are prefixed `.bro/` so extracting at /home/ride lands them at
  /home/ride/.bro/<file>. files are 0600, the dir 0700, all owned by the host
  uid/gid (the same uid the entrypoint remaps `ride` to on Linux); the entrypoint
  re-owns the tree to `ride` after its remap so the bytes are readable there and on
  Docker for Mac (where the remap is skipped). mtime defaults to 0 — deterministic,
  no clock needed.
  """
  uid, gid = os.getuid(), os.getgid()
  buffer = io.BytesIO()
  with tarfile.open(fileobj=buffer, mode='w') as tar:
    root = tarfile.TarInfo('.bro')
    root.type = tarfile.DIRTYPE
    root.mode = 0o700
    root.uid, root.gid = uid, gid
    tar.addfile(root)
    credentials_directory = tarfile.TarInfo('.bro/creds')
    credentials_directory.type = tarfile.DIRTYPE
    credentials_directory.mode = 0o700
    credentials_directory.uid, credentials_directory.gid = uid, gid
    tar.addfile(credentials_directory)
    for filename in sorted(files):
      data = files[filename]
      info = tarfile.TarInfo(f'.bro/{filename}')
      info.size = len(data)
      info.mode = 0o600
      info.uid, info.gid = uid, gid
      tar.addfile(info, io.BytesIO(data))
  return buffer.getvalue()
