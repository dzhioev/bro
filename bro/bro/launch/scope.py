import enum
import io
import os
import shutil
import tarfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from base import credentials, log

if TYPE_CHECKING:
  import llm.mcp

# secrets every claude code session resolves regardless of bro: the
# sync-session-log hooks run in-session, and an in-session bro run records to trails.
_SESSION_BASELINE = frozenset({'session_log', 'trails'})


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


class Surface(enum.Enum):
  """the launch surface a credential scope is computed for."""

  CW_SESSION = 'cw-session'  # dive-in / plain `cw ss`, themed as its persona bro
  BRO_SESSION = 'bro-session'  # `cw ss --bro`: claude --bare serving the bro's own MCP servers
  BRO_RUN = 'bro-run'  # the bro as an LLM process: the `bro run` / `bro chat` hop, summon children


@dataclass(frozen=True)
class _Recipe:
  """a surface's row in the per-surface scope table (`_RECIPES`).

  `baseline`/`optional_baseline` are the bro-independent tiers; `harness` selects
  the component set the bro's manifest counts; `auth_secret` is the surface's
  fixed session-auth secret; `llm_key` adds the bro's own LLM-provider key
  (`llm_spec.needed_secrets()`, which the manifest omits); `docker_sock` pins the
  socket decision (None → the bro's `needs_docker`); `unknown_bro_fallback`
  degrades an unknown bro to the baseline scope with a warning instead of raising.
  """

  baseline: frozenset[str]
  optional_baseline: frozenset[str]
  harness: 'llm.mcp.Harness'
  auth_secret: Optional[str]
  llm_key: bool
  docker_sock: Optional[bool]
  unknown_bro_fallback: bool


# the per-surface recipes; reference/cw.md ("Scoped credential hydration" →
# per-surface sets) documents each set's rationale, bullet-per-row.
_RECIPES: dict[Surface, _Recipe] = {
  Surface.CW_SESSION: _Recipe(
    baseline=_SESSION_BASELINE,
    optional_baseline=frozenset({'openai'}),
    harness='claude',
    auth_secret='claude_code',
    llm_key=False,
    docker_sock=True,
    unknown_bro_fallback=True,
  ),
  Surface.BRO_SESSION: _Recipe(
    baseline=_SESSION_BASELINE,
    optional_baseline=frozenset({'openai'}),
    harness='bro',
    auth_secret='anthropic',
    llm_key=False,
    docker_sock=None,
    unknown_bro_fallback=True,
  ),
  Surface.BRO_RUN: _Recipe(
    baseline=frozenset({'trails'}),
    optional_baseline=frozenset(),
    harness='bro',
    auth_secret=None,
    llm_key=True,
    docker_sock=None,
    unknown_bro_fallback=False,
  ),
}


def scoped_secrets(bro_name: str, surface: Surface) -> ScopedSecrets:
  """the credential scope of a launch running as `bro_name` on `surface` — one
  computation for every launch surface, so they cannot drift. the per-surface
  recipe is the `_RECIPES` row; required hydration is strict, so each surface
  requests only what it actually uses.
  """
  from bro.registry import create_bro

  recipe = _RECIPES[surface]
  required = set(recipe.baseline)
  optional = set(recipe.optional_baseline)
  try:
    bro = create_bro(bro_name)
  except KeyError as e:
    # unknown bro (registry KeyError) only — other failures propagate rather
    # than collapse into a silently under-scoped session
    if not recipe.unknown_bro_fallback:
      raise
    log.warning('could not resolve bro %r for credential scoping: %s', bro_name, e)
    # no bro to consult, so the per-bro socket rule degrades to no socket (moot
    # anyway — the argv builder re-raises the same KeyError downstream)
    docker_sock = recipe.docker_sock if recipe.docker_sock is not None else False
    return ScopedSecrets(required=required, optional=optional, docker_sock=docker_sock)
  required.update(bro.needed_secrets(harness=recipe.harness))
  if recipe.auth_secret is not None:
    required.add(recipe.auth_secret)
  if recipe.llm_key:
    required.update(bro.llm_spec.needed_secrets())
  optional.update(bro.optional_secrets(harness=recipe.harness))
  if recipe.docker_sock is not None:
    docker_sock = recipe.docker_sock
  else:
    docker_sock = bro.needs_docker
  return ScopedSecrets(required=required, optional=optional, docker_sock=docker_sock)


def log_scoped_secrets(subject: str, required: Collection[str], optional: Collection[str]) -> None:
  """log a launch's credential scope at every scoped-store launch path."""
  names = sorted(set(required))
  log.info('scoped secrets for %s: %s', subject, ', '.join(names) if len(names) > 0 else '(none)')
  optional_names = sorted(set(optional) - set(required))
  if len(optional_names) > 0:
    log.info('optional (best-effort) secrets for %s: %s', subject, ', '.join(optional_names))


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
  credentials file, so the token is a cw-session's whole auth — both
  launch surfaces gate on it before anything is created, and `warn_when_missing`
  surfaces the remaining unauthenticated path (a runner spawned by an outer cw
  that predates the gate). a `--bro` session authenticates via apiKeyHelper and
  resolves no token by design. containers get the same var from the secret's
  registry install hook as well; re-applying it here is idempotent.
  """
  for var in _OUTRANKING_AUTH_VARS:
    if env.pop(var, None) is not None:
      log.verbose('scrubbed inherited %s from the claude session env', var)
  token = credentials.try_get('claude_code')
  if token is None:
    if warn_when_missing:
      log.warning(
        'claude_code secret not resolvable; the session starts unauthenticated — mint a '
        'token with `claude setup-token` and store it in ~/.ppp/claude_code_oauth_token'
      )
    return
  env['CLAUDE_CODE_OAUTH_TOKEN'] = token


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


class LaunchScopeError(Exception):
  """a launch failed its scope preflight: a no-op grant/revoke override, an
  unknown summon target, or an unknown/unresolvable required secret."""


def preflight_scoped_launch(
  scoped: ScopedSecrets,
  bro_name: str,
  *,
  grant_cred: list[str],
  revoke_cred: list[str],
  grant_summon: list[str],
  revoke_summon: list[str],
) -> tuple[ScopedSecrets, set[str], dict[str, bytes]]:
  """the scope preflight every launch surface runs before creating anything
  (worktree, container, workspace dir): finalize the credential scope
  (`finalize_scoped_secrets`), compute the summon allow-list of a launch running
  as `bro_name` (`cw.summon.summon_allow_list`), and hydrate the scoped store
  (`credentials.build_scoped_store`) — any failure raised as a single
  `LaunchScopeError` for the caller to render on its own error surface.

  returns (scope, allow-list, store). the container launch path rebuilds the
  store at create, so container callers drop it — the build is the preflight
  itself; a host session materializes the returned one.
  """
  # imported here, not at module level: cw.summon reaches back into this module
  # through cw.workspace → cw.docker
  from cw.summon import summon_allow_list

  try:
    scoped = finalize_scoped_secrets(scoped, grant=grant_cred, revoke=revoke_cred)
    may_summon = summon_allow_list(bro_name, grant=grant_summon, revoke=revoke_summon)
    store = credentials.build_scoped_store(scoped.required, optional=scoped.optional)
  except (ValueError, credentials.SecretNotFound) as e:
    raise LaunchScopeError(str(e)) from e
  return scoped, may_summon, store


def _materialize_scoped_store(files: dict[str, bytes], directory: Path) -> Path:
  """write a scoped credential store (`credentials.build_scoped_store`) to
  `directory` and return its registry file — the value a host session's
  CREDENTIALS_REGISTRY points at (the registry's directory joins the resolver's
  search path). the directory is recreated from scratch so a secret dropped from
  the scope (e.g. a lapsed `--grant-cred`) does not linger from an earlier
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
