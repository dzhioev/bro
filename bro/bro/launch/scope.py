import io
import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base import credentials, log

# secrets every claude code session resolves regardless of bro: the
# sync-session-log hooks run in-session, and an in-session bro run records to trails.
_CW_SESSION_BASELINE = ('session_log', 'trails')

# the bro a no-`--bro` container session themes as (dive-in already sets CW_BRO to
# this); bounds a manual `cw ss` session's scoped credentials.
_DEFAULT_CW_BRO = 'ppp-dev'


def _session_bro_name(bro: Optional[str]) -> str:
  """the bro a `cw ss` session runs as — its identity for credential scoping and
  the summon allow-list. `--bro` names it directly; a native session themes as the
  ambient CW_BRO (dive-in sets ppp-dev; a plain `cw ss` defaults to it too)."""
  if bro is not None:
    return bro
  return os.environ.get('CW_BRO', _DEFAULT_CW_BRO)


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


def bro_run_secrets(bro_name: str) -> ScopedSecrets:
  """the credential scope of a bro run as an LLM process in its own container —
  one computation for every surface that spawns one (the `ask`/`do-task`/`call`
  hop, broker-spawned bro children), so they cannot drift.

  required: the bro's manifest plus its LLM key (`needed_secrets()` omits it) and
  `trails` (recording is mandatory for bro runs). optional: the bro's best-effort
  tier — a no-op for a bro whose optional secret is already its required LLM key,
  but correct in general: a component that degrades without a secret still gets it
  when the host can resolve it. docker socket only when the bro does docker work.
  """
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  required = set(bro.needed_secrets()) | set(bro.llm_spec.needed_secrets()) | {'trails'}
  return ScopedSecrets(
    required=required, optional=set(bro.optional_secrets()), docker_sock=bro.needs_docker
  )


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


# auth env vars that outrank CLAUDE_CODE_OAUTH_TOKEN in claude's credential
# precedence: a value inherited from the launching shell would silently hijack
# the session's auth (an invalid one surfaces as a login/API-key error at the
# first call), so the launch scrubs them.
_OUTRANKING_AUTH_VARS = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')


def _apply_claude_auth(env: dict[str, str], *, warn_when_missing: bool = False) -> None:
  """align a claude session env with the session auth model (reference/cw.md).

  scrubs the inherited vars that outrank the session's designated auth, then
  overlays the long-lived `claude setup-token` credential (`claude_code`). the
  session's private claude state (cw/claude_config.py) carries no OAuth
  credentials file, so the token is a native session's whole auth — both
  launch surfaces gate on it before anything is created, and `warn_when_missing`
  surfaces the remaining unauthenticated path (a runner spawned by an outer cw
  that predates the gate). a `--bro` session authenticates via apiKeyHelper and
  resolves no token by design. containers get the same var from the secret's
  registry install hook as well; re-applying it here is idempotent.
  """
  for var in _OUTRANKING_AUTH_VARS:
    if env.pop(var, None) is not None:
      log.info('scrubbed inherited %s from the claude session env', var)
  token = credentials.try_get('claude_code')
  if token is None:
    if warn_when_missing:
      log.warning(
        'claude_code secret not resolvable; the session starts unauthenticated — mint a '
        'token with `claude setup-token` and store it in ~/.ppp/claude_code_oauth_token'
      )
    return
  env['CLAUDE_CODE_OAUTH_TOKEN'] = token


def _finalize_secrets(secrets: set[str], *, grant: list[str], revoke: list[str]) -> set[str]:
  """layer the per-session `--grant-cred` / `--revoke-cred` overrides onto a
  computed scoped set. grant/revoke apply strictly — a grant/revoke that doesn't
  change the set raises `ValueError` (`credentials.apply_grant_revoke`)."""
  return credentials.apply_grant_revoke(
    secrets, grant=grant, revoke=revoke, subject='scoped credential set'
  )


def _session_secrets(bro_name: str, *, mcp: Optional[str], bro_mode: bool) -> ScopedSecrets:
  """scoped credential sets (required, optional) + docker-socket decision for a
  `cw ss` session themed as `bro_name` — one computation for both launch modes.
  the two surfaces request different sets (required hydration is strict, so each
  requests only what it actually uses):

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
    server runs in-session) + `claude_code` (required: the long-lived OAuth
    token it exports as CLAUDE_CODE_OAUTH_TOKEN is a native session's only
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
    # required, not best-effort: this secret's CLAUDE_CODE_OAUTH_TOKEN is a
    # native session's sole credential, so a missing token must fail loudly
    # before the session starts rather than as a turn-1 401 inside it. `--bro`
    # (claude --bare) omits it — bare ignores the var and authenticates with
    # the anthropic key.
    secrets.add('claude_code')
  return ScopedSecrets(required=secrets, optional=optional, docker_sock=docker_sock)


def _materialize_scoped_store(files: dict[str, bytes], directory: Path) -> Path:
  """write a scoped credential store (`credentials.build_scoped_store`) to
  `directory` and return its registry file — the value a host session's
  CREDENTIALS_REGISTRY points at (the registry's directory joins the resolver's
  search path). the directory is recreated from scratch so a secret dropped from
  the scope (e.g. a lapsed `--grant-cred`) does not linger from an earlier
  launch."""
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
