"""scoped credential-store mechanics: the tiers a launch hydrates and how the
resolved store reaches the session (a tar into the container's own layer, an
on-disk dir a host session's CREDENTIALS_REGISTRY points at). Which names land
in the tiers is the launch surface's policy; this module only carries and
materializes it.
"""

import io
import os
import shutil
import tarfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from bro.base import credentials, log


@dataclass(frozen=True)
class ScopedSecrets:
  """a session launch's credential scope.

  required is hydrated strictly (a missing secret fails launch); optional is the
  best-effort tier (skipped when unresolvable); docker_sock decides the socket
  mount (container launches only — a host session has the host daemon anyway).

  unbound_kinds are the kinds this launch may not read at all: the host selects
  an instance of each per project and the launch bound no project entry, so the
  kind resolves to whichever project owns its registry entry. Enforced once the
  launch's own overrides have had their say (`finalize_scoped_secrets`), since a
  granted instance names the project outright.
  """

  required: set[str]
  optional: set[str]
  docker_sock: bool
  unbound_kinds: frozenset[str] = frozenset()


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
    docker_sock=scoped.docker_sock,
    unbound_kinds=scoped.unbound_kinds,
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
  """write a scoped credential store (`credentials.build_scoped_store`) to
  `directory` and return its registry file — the value a host session's
  CREDENTIALS_REGISTRY points at (the registry's directory joins the resolver's
  search path). the directory is recreated from scratch so a secret dropped from
  the scope (e.g. a lapsed `--grant`) does not linger from an earlier
  launch."""
  log.verbose('materializing the scoped credential store at %s', directory)
  if directory.exists():
    shutil.rmtree(directory)
  directory.mkdir(parents=True)
  directory.chmod(0o700)
  for filename, data in files.items():
    file = directory / filename
    file.write_bytes(data)
    file.chmod(0o600)
  return directory / 'credentials.json'


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
    for filename in sorted(files):
      data = files[filename]
      info = tarfile.TarInfo(f'.bro/{filename}')
      info.size = len(data)
      info.mode = 0o600
      info.uid, info.gid = uid, gid
      tar.addfile(info, io.BytesIO(data))
  return buffer.getvalue()
