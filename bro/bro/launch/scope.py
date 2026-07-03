import io
import os
import tarfile
from dataclasses import dataclass
from typing import Optional

from base import credentials, log

# secrets every containerized claude code session resolves regardless of bro: the
# sync-session-log hooks run in-container, and an in-session bro run records to trails.
_CW_SESSION_BASELINE = ('session_log', 'trails')

# the bro a no-`--bro` container session themes as (dive-in already sets CW_BRO to
# this); bounds a manual `cw ss -c` session's scoped credentials.
_DEFAULT_CW_BRO = 'ppp-dev'


@dataclass(frozen=True)
class ScopedSecrets:
  """the credential scope `_container_secrets` computes for a container session.

  required is hydrated strictly (a missing secret fails launch); optional is the
  best-effort tier (skipped when unresolvable); docker_sock decides the socket mount.
  """

  required: set[str]
  optional: set[str]
  docker_sock: bool


def _load_anthropic_key() -> Optional[str]:
  """return the api_key from the `anthropic` secret, or None if missing/invalid."""
  try:
    config = credentials.get_json('anthropic')
  except credentials.SecretNotFound:
    return None
  key = config.get('api_key')
  if not isinstance(key, str) or len(key) == 0:
    return None
  return key


def _claude_code_token_env() -> dict[str, str]:
  """CLAUDE_CODE_OAUTH_TOKEN overlay for a host-mode claude session, or empty.

  resolves the long-lived `claude setup-token` credential (`claude_code`)
  best-effort: present → export it so claude prefers this stable subscription
  bearer over the rotating OAuth in ~/.claude/.credentials.json (whose
  cross-session refresh-token rotation forces the periodic re-login); absent →
  empty, and claude falls back to that file. containers get the same var from
  the secret's registry install hook, not here.
  """
  token = credentials.try_get('claude_code')
  if token is None:
    return {}
  return {'CLAUDE_CODE_OAUTH_TOKEN': token}


def _finalize_secrets(secrets: set[str], *, grant: list[str], revoke: list[str]) -> set[str]:
  """layer the per-session `--grant` / `--revoke` overrides onto a computed scoped
  set. grant/revoke apply strictly — a grant/revoke that doesn't change the set
  raises `ValueError` (`credentials.apply_grant_revoke`)."""
  return credentials.apply_grant_revoke(secrets, grant=grant, revoke=revoke)


def _container_secrets(bro_name: str, *, mcp: Optional[str], bro_mode: bool) -> ScopedSecrets:
  """scoped credential sets (required, optional) + docker-socket decision for a
  container session themed as `bro_name`. the two surfaces request different sets
  (required hydration is strict, so each requests only what it actually uses):

  - `--bro` (`claude --bare` serving the bro's own in-process MCP servers): the
    bro's full `needed_secrets()` + `anthropic` for the apiKeyHelper, plus the
    bro's `optional_secrets()` hydrated best-effort (e.g. the LLM key behind a
    data source's query-focused fetch summary). docker socket only if
    `bro.needs_docker`.
  - a native claude code session themed as the bro (dive-in / plain `cw ss`): it
    drives the bro's *skills* (bash → `extra_secrets`) and its flow via `--mcp`,
    not the bro's in-process MCP / data-source toolset — so `extra_secrets`
    + the flow MCP secrets (`--mcp http` → `flow_mcp` for the deployed server;
    `--mcp local` → whatever `flow.mcp.spec()` declares, since the session-local
    server runs inside the container) + `claude_code` (required: the long-lived
    OAuth token it exports as CLAUDE_CODE_OAUTH_TOKEN is a native session's only
    auth). always keeps the socket (it has a Bash tool).

  both add the session baseline (sync-log + trails) to the required set.
  """
  from bro.registry import create_bro

  secrets: set[str] = set(_CW_SESSION_BASELINE)
  optional: set[str] = set()
  docker_sock = True
  try:
    bro = create_bro(bro_name)
  except KeyError as e:
    # unknown bro (registry KeyError) only — other failures propagate rather than
    # collapse into a silently under-scoped session. a native session still gets
    # the socket; a --bro fallback does not (moot anyway — the argv builder
    # re-raises the same KeyError downstream).
    log.warning('could not resolve bro %r for credential scoping: %s', bro_name, e)
    return ScopedSecrets(required=secrets, optional=optional, docker_sock=not bro_mode)
  if bro_mode:
    secrets.update(bro.needed_secrets())
    secrets.add('anthropic')
    optional.update(bro.optional_secrets())
    docker_sock = bro.needs_docker
  else:
    secrets.update(bro._extra_secrets)
    if mcp == 'http':
      secrets.add('flow_mcp')
    if mcp == 'local':
      import flow.mcp

      secrets.update(flow.mcp.spec().needed_secrets)
    # required, not best-effort: this secret's CLAUDE_CODE_OAUTH_TOKEN (registry
    # install hook) is a native session's sole credential, so a missing token
    # must fail loudly on the host before the container starts rather than as a
    # turn-1 401 inside it. `--bro` (claude --bare) omits it — bare ignores the
    # var and authenticates with the anthropic key.
    secrets.add('claude_code')
  return ScopedSecrets(required=secrets, optional=optional, docker_sock=docker_sock)


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
