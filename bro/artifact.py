#!/usr/bin/env python
"""artifact — pass files between broker peers by content-addressed reference.

The peer side of artifact sharing: two request kinds on the session channel,
answered by the host-side store (`ride/ride/artifacts.py`). This module owns
the wire contract — the kinds, their args keys, and the ref grammar — for the
library client and the `artifact` CLI/session command.

- `artifact.mint` with args `{path}` — `path` names a file or directory
  relative to the requesting peer's workspace root. The host ingests a private
  copy into the session store and answers `ok{ref, size}`.
- `artifact.get` with args `{ref}` — the host makes the ref visible to the
  requesting peer and answers `ok{path}` with the path it appears at: the
  read-only view mount for a container peer, a copy under the session's
  workspace directory for a host-mode one. The path is not the peer's to
  write; a peer that wants an editable copy makes one itself.

A ref is `sha256:` plus 64 hex digits. For a file it is the plain content
digest, so `sha256sum` checks it. For a directory it is the digest of a
canonical manifest of typed entries — `digest_path` is the one implementation,
shared by the host's ingest and the local `artifact digest` verb: entries in
depth-first name order, each `{path, type}` plus per type the content digest
and executable bit of a file or the recorded (never followed) target of a
symlink, refused when that target escapes the directory; the manifest digested
as compact sorted-key JSON.

A minted ref is readable by the minting peer and its summoners up to the
session root; a summon request's `share` list hands refs down to the child it
spawns. Nothing else reaches a ref — knowing one is not access.

The CLI blocks for the host's answer: `artifact mint <path>` prints the ref,
`artifact get <ref>` prints the path, and `artifact digest <path>` computes a
ref locally, with no channel involved. Like `summon`, an unset
`BROKER_CHANNEL` is an error rather than inert, and broker imports are
deferred to call time so importers of the contract constants never pull the
broker package in.
"""

import hashlib
import json
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Optional

import bro.base.args as base_args
from bro.base import log

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.client import Client

__cli_name__ = 'artifact'

MINT = 'artifact.mint'  # the kind a mint request names; args {path}
GET = 'artifact.get'  # the kind a get request names; args {ref}
# client-side bound on the host's answer — ingest reads and copies the full
# content, so a multi-gigabyte bundle takes real time
DEFAULT_TIMEOUT = 600.0

_REF = re.compile(r'sha256:[0-9a-f]{64}')
_EXECUTABLE_BITS = 0o111


def is_ref(value: Any) -> bool:
  """whether `value` is a well-formed artifact ref."""
  return isinstance(value, str) and _REF.fullmatch(value) is not None


def _file_digest(path: Path) -> str:
  with path.open('rb') as content:
    return f'sha256:{hashlib.file_digest(content, "sha256").hexdigest()}'


def _symlink_escapes(link: PurePosixPath, target: str) -> bool:
  if PurePosixPath(target).is_absolute():
    return True
  combined = posixpath.normpath(str(link.parent / target))
  return combined == '..' or combined.startswith('../')


def _entries(directory: Path, prefix: PurePosixPath, manifest: list[dict[str, Any]]) -> None:
  for entry in sorted(os.scandir(directory), key=lambda scanned: scanned.name):
    path = prefix / entry.name
    if entry.is_symlink():
      target = os.readlink(entry.path)
      if _symlink_escapes(path, target):
        raise ValueError(f'symlink {path} escapes the directory (target {target!r})')
      manifest.append({'path': str(path), 'type': 'symlink', 'target': target})
    elif entry.is_dir(follow_symlinks=False):
      manifest.append({'path': str(path), 'type': 'dir'})
      _entries(Path(entry.path), path, manifest)
    elif entry.is_file(follow_symlinks=False):
      executable = entry.stat(follow_symlinks=False).st_mode & _EXECUTABLE_BITS != 0
      manifest.append(
        {
          'path': str(path),
          'type': 'file',
          'digest': _file_digest(Path(entry.path)),
          'executable': executable,
        }
      )
    else:
      raise ValueError(f'unsupported entry type at {path}')


def directory_manifest(path: Path) -> list[dict[str, Any]]:
  """the canonical manifest of the directory at `path` (see the module
  docstring); raises `ValueError` on an escaping symlink or an entry that is
  neither file, directory, nor symlink."""
  manifest: list[dict[str, Any]] = []
  _entries(path, PurePosixPath(), manifest)
  return manifest


def digest_path(path: Path) -> str:
  """the artifact ref of the file or directory at `path`."""
  if path.is_file():
    return _file_digest(path)
  if path.is_dir():
    manifest = json.dumps(
      directory_manifest(path), ensure_ascii=False, separators=(',', ':'), sort_keys=True
    )
    return f'sha256:{hashlib.sha256(manifest.encode()).hexdigest()}'
  raise ValueError(f'no file or directory at {path}')


class ArtifactError(Exception):
  """an artifact request that produced no usable answer: denied, failed, or its
  result never arrived. The message is the operator-facing reason."""


@dataclass(frozen=True)
class Minted:
  ref: str
  size: int


def _open_client() -> 'Client':
  from bro.broker.client import CHANNEL_ENV, Client

  client = Client.from_env()
  if client is None:
    raise ArtifactError(f'no broker channel ({CHANNEL_ENV} unset); artifacts need a session channel')  # fmt: skip
  return client


def _interpret_result(message: 'Message') -> dict[str, Any]:
  """turn an artifact result into its value, or raise `ArtifactError` with the
  failure reason."""
  payload = message.payload
  outcome = payload.get('outcome')
  if outcome == 'ok':
    value = payload.get('value')
    if not isinstance(value, dict):
      raise ArtifactError(f'malformed artifact result value: {value!r}')
    return value
  if outcome == 'denied':
    raise ArtifactError(str(payload.get('error', payload)))
  detail = payload.get('detail')
  detail = detail if isinstance(detail, dict) else {}
  parts = [f'artifact request failed ({detail.get("reason")})']
  diagnostic = payload.get('error')
  if diagnostic is not None and len(str(diagnostic).strip()) > 0:
    parts.append(str(diagnostic).strip())
  raise ArtifactError('; '.join(parts))


def _call(kind: str, args: dict[str, Any], timeout: Optional[float]) -> dict[str, Any]:
  with _open_client() as client:
    try:
      result = client.call(kind, args, timeout if timeout is not None else DEFAULT_TIMEOUT)
    except TimeoutError:
      raise ArtifactError(f'no {kind} result within the timeout') from None
    except ConnectionError as e:
      raise ArtifactError(f'broker channel closed awaiting the {kind} result: {e}') from None
  return _interpret_result(result)


def mint_artifact(path: str, *, timeout: Optional[float] = None) -> Minted:
  """mint the file or directory at workspace-relative `path` and return its ref
  and size. Raises `ArtifactError` on any failure."""
  value = _call(MINT, {'path': path}, timeout)
  ref = value.get('ref')
  size = value.get('size')
  if not is_ref(ref) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
    raise ArtifactError(f'malformed mint result value: {value!r}')
  return Minted(ref=str(ref), size=size)


def get_artifact(ref: str, *, timeout: Optional[float] = None) -> str:
  """make `ref` visible to this peer and return the path it appears at. Raises
  `ArtifactError` on any failure."""
  value = _call(GET, {'ref': ref}, timeout)
  path = value.get('path')
  if not isinstance(path, str) or len(path) == 0:
    raise ArtifactError(f'malformed get result value: {value!r}')
  return path


# --- CLI ------------------------------------------------------------------------


def _mint(path: str, timeout: Optional[float]) -> int:
  try:
    minted = mint_artifact(path, timeout=timeout)
  except ArtifactError as e:
    log.error('%s', e)
    return 1
  log.info('minted %d bytes', minted.size)
  print(minted.ref)
  return 0


def _get(ref: str, timeout: Optional[float]) -> int:
  try:
    path = get_artifact(ref, timeout=timeout)
  except ArtifactError as e:
    log.error('%s', e)
    return 1
  print(path)
  return 0


def _digest(path: str) -> int:
  try:
    ref = digest_path(Path(path))
  except (OSError, ValueError) as e:
    log.error('%s', e)
    return 1
  print(ref)
  return 0


_TIMEOUT_HELP = f"seconds to wait for the host's answer (default: {DEFAULT_TIMEOUT:.0f})"


def main(argv: list[str]) -> Optional[int]:
  if len(argv) > 1 and argv[1] == 'mint':
    parser = base_args.Parser(
      prog='artifact mint',
      description='mint a workspace file or directory into the session artifact '
      'store and print its content-addressed ref; the ref is readable by this '
      'peer and its summoners, and a summon request can share it down',
    )
    parser.add_argument('path', help='file or directory, relative to the workspace root')
    parser.add_argument('--timeout', type=float, metavar='SECONDS', help=_TIMEOUT_HELP)
    return _mint(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'get':
    parser = base_args.Parser(
      prog='artifact get',
      description='make an artifact ref visible to this session and print the '
      'read-only path it appears at; copy from there for an editable version',
    )
    parser.add_argument('ref', help='artifact ref (sha256:<64 hex digits>)')
    parser.add_argument('--timeout', type=float, metavar='SECONDS', help=_TIMEOUT_HELP)
    return _get(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'digest':
    parser = base_args.Parser(
      prog='artifact digest',
      description='compute the artifact ref of a local file or directory '
      'without touching the session channel — what checks a directory ref, the '
      'way sha256sum checks a file one',
    )
    parser.add_argument('path', help='file or directory to digest')
    return _digest(**parser.parse(argv[1:]))
  log.error('usage: artifact mint|get|digest …; each verb takes --help')
  return 2
