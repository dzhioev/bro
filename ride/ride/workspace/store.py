"""Scoped credential-store tiers and session materialization.

A container receives an in-memory tar in its own layer;
a host session points `BRO_STORE` at a materialized directory.
The launch surface owns which names enter each tier.
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
  """a session launch's credential scope.

  required is hydrated strictly (a missing secret fails launch); optional is the
  best-effort tier (skipped when unresolvable).

  unbound_kinds are the kinds this launch may not read at all: a project layer
  selects each kind, defaults does not, and the launch bound no project entry.
  A kind-addressed read would otherwise fall through to unowned bare material.
  The refusal is enforced after the launch's own overrides (`finalize_scoped_secrets`), since a
  granted instance names the project outright.
  """

  required: set[str]
  optional: set[str]
  unbound_kinds: frozenset[str] = frozenset()
  selection: dict[str, Optional[str]] = field(default_factory=dict)


def _replacement_revokes(scoped_names: set[str], grants: list[str]) -> list[str]:
  selected_by_kind: dict[str, str] = {}
  for selected in scoped_names:
    kind, _ = credentials.parse_name(selected)
    if kind in selected_by_kind:
      raise ValueError(f'credential kind {kind!r} has multiple selected names')
    selected_by_kind[kind] = selected

  granted_kinds: set[str] = set()
  replacements: list[str] = []
  for grant in grants:
    kind, _ = credentials.parse_name(grant)
    if kind in granted_kinds:
      raise ValueError(f'credential kind {kind!r} is granted more than once')
    granted_kinds.add(kind)
    selected = selected_by_kind.get(kind)
    if selected is not None and selected != grant:
      replacements.append(selected)
  return replacements


def finalize_scoped_secrets(
  scoped: ScopedSecrets, *, grant: list[str], revoke: list[str]
) -> ScopedSecrets:
  """layer strict per-session overrides across both credential tiers.

  a grant replaces a selected credential of the same kind, or joins the required
  tier when that kind is absent. a revoke removes the exact name from whichever
  tier contains it. all remaining no-op overrides are errors.
  """
  scoped_names = scoped.required | scoped.optional
  replacement_revokes = _replacement_revokes(scoped_names, grant)
  final_names = credentials.apply_grant_revoke(
    scoped_names,
    grant=grant,
    revoke=[*revoke, *replacement_revokes],
    subject='scoped credential set',
  )
  required = (scoped.required | set(grant)) & final_names
  optional = final_names - required
  _refuse_unbound_kinds(final_names, scoped.unbound_kinds)
  return ScopedSecrets(
    required=required,
    optional=optional,
    unbound_kinds=scoped.unbound_kinds,
    selection=dict(scoped.selection),
  )


def _refuse_unbound_kinds(names: set[str], unbound: frozenset[str]) -> None:
  """refuse the scope's kind-addressed reads of a kind the launch may not bind
  (`ScopedSecrets.unbound_kinds`). A `kind+instance` name passes: it names the
  instance itself, so nothing is being read on a project's behalf."""
  refused = sorted(name for name in names if name in unbound)
  if len(refused) > 0:
    raise ValueError(
      f'this host reads {", ".join(refused)} per project, and no project entry names this '
      f"launch's attachment; add it to ~/.bro.json, or name the instance for this launch "
      f'(--grant {refused[0]}+<instance>)'
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
