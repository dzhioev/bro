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

from base import credentials, log


@dataclass(frozen=True)
class ScopedSecrets:
  """a session launch's credential scope.

  required is hydrated strictly (a missing secret fails launch); optional is the
  best-effort tier (skipped when unresolvable); docker_sock decides the socket
  mount (container launches only — a host session has the host daemon anyway).
  """

  required: set[str]
  optional: set[str]
  docker_sock: bool


def finalize_scoped_secrets(
  scoped: ScopedSecrets, *, grant: list[str], revoke: list[str]
) -> ScopedSecrets:
  """layer strict per-session overrides across both credential tiers.

  grants join the required tier. a revoke removes the name from whichever tier
  contains it; a name in neither tier remains an error, as do all other no-op
  overrides enforced by `credentials.apply_grant_revoke`.
  """
  final_names = credentials.apply_grant_revoke(
    scoped.required | scoped.optional,
    grant=grant,
    revoke=revoke,
    subject='scoped credential set',
  )
  required = (scoped.required | set(grant)) & final_names
  optional = final_names - required
  return ScopedSecrets(required=required, optional=optional, docker_sock=scoped.docker_sock)


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


def _ppp_tarball(files: dict[str, bytes]) -> bytes:
  """pack a scoped credential store into a tar for `docker cp` into /home/cw.

  entries are prefixed `.ppp/` so extracting at /home/cw lands them at
  /home/cw/.ppp/<file>. files are 0600, the dir 0700, all owned by the host
  uid/gid (the same uid the entrypoint remaps `cw` to on Linux); the entrypoint
  re-owns the tree to `cw` after its remap so the bytes are readable there and on
  Docker for Mac (where the remap is skipped). mtime defaults to 0 — deterministic,
  no clock needed.
  """
  uid, gid = os.getuid(), os.getgid()
  buffer = io.BytesIO()
  with tarfile.open(fileobj=buffer, mode='w') as tar:
    root = tarfile.TarInfo('.ppp')
    root.type = tarfile.DIRTYPE
    root.mode = 0o700
    root.uid, root.gid = uid, gid
    tar.addfile(root)
    for filename in sorted(files):
      data = files[filename]
      info = tarfile.TarInfo(f'.ppp/{filename}')
      info.size = len(data)
      info.mode = 0o600
      info.uid, info.gid = uid, gid
      tar.addfile(info, io.BytesIO(data))
  return buffer.getvalue()
