"""per-surface launch scoping of a bro run: which credentials each launch
surface hydrates and which bros the session may summon, computed from the bro's
own declarations (manifest, optional tier, `may_summon`, `needs_docker`).
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from bro.base import credentials, log
from bro.workspace.store import ScopedSecrets, finalize_scoped_secrets

if TYPE_CHECKING:
  from bro.llm.mcp import Harness

# secrets every claude code session resolves regardless of bro: the session
# recorder and any in-session bro run both record through trails.
_SESSION_BASELINE = frozenset({'trails'})


class Surface(enum.Enum):
  """the launch surface a credential scope is computed for."""

  CW_SESSION = 'cw-session'  # dive-in / plain `cw ss`, themed as its session bro
  RAW_SESSION = 'raw-session'  # `cw ss --raw`: claude --bare serving the bro's own MCP servers
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
  harness: 'Harness'
  auth_secret: Optional[str]
  llm_key: bool
  docker_sock: Optional[bool]
  unknown_bro_fallback: bool


# the per-surface recipes; reference/cw.md ("Scoped credential hydration" →
# per-surface sets) documents each set's rationale, bullet-per-row.
_RECIPES: dict[Surface, _Recipe] = {
  Surface.CW_SESSION: _Recipe(
    baseline=_SESSION_BASELINE,
    optional_baseline=frozenset(),
    harness='claude',
    auth_secret='claude_code',
    llm_key=False,
    docker_sock=True,
    unknown_bro_fallback=True,
  ),
  Surface.RAW_SESSION: _Recipe(
    baseline=_SESSION_BASELINE,
    optional_baseline=frozenset(),
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


def _validate_credential_instances(credential_instances: Mapping[str, str]) -> None:
  """reject mapped kinds absent from the host credential registry."""
  known_kinds = {credentials.parse_name(name)[0] for name in credentials.host_registry()}
  unknown = sorted(set(credential_instances) - known_kinds)
  if len(unknown) > 0:
    raise LaunchScopeError(
      f'[tool.bro] creds maps kind(s) not known to the credential registry: '
      f'{", ".join(map(repr, unknown))}'
    )


def _substitute_credential_instances(
  scoped: ScopedSecrets, credential_instances: Mapping[str, str]
) -> ScopedSecrets:
  """swap mapped bare kinds present in either tier for `kind+instance` variants."""

  def substitute(names: set[str]) -> set[str]:
    return {
      f'{name}+{credential_instances[name]}' if name in credential_instances else name
      for name in names
    }

  return ScopedSecrets(
    required=substitute(scoped.required),
    optional=substitute(scoped.optional),
    docker_sock=scoped.docker_sock,
  )


def scoped_secrets(
  bro_name: str, surface: Surface, *, credential_instances: Mapping[str, str]
) -> ScopedSecrets:
  """the credential scope of a launch running as `bro_name` on `surface` — one
  computation for every launch surface, so they cannot drift. the per-surface
  recipe is the `_RECIPES` row; required hydration is strict, so each surface
  requests only what it actually uses.

  `credential_instances` is the operated repo's kind → instance selection
  (`bro.workspace.project.ProjectConfig.creds`). mapped kinds are validated against
  the host credential registry, then matching names are substituted over both
  tiers of this persona's scope — so later `--grant`/`--revoke` overrides and
  hydration see the `kind+instance` names, while components keep declaring kinds.
  """
  from bro.registry import create_bro

  _validate_credential_instances(credential_instances)
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
    return _substitute_credential_instances(
      ScopedSecrets(required=required, optional=optional, docker_sock=docker_sock),
      credential_instances,
    )
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
  return _substitute_credential_instances(
    ScopedSecrets(required=required, optional=optional, docker_sock=docker_sock),
    credential_instances,
  )


def summoned_credential_scope(
  bro_name: str,
  *,
  credential_instances: Mapping[str, str],
  grant: list[str],
  revoke: list[str],
  swap_credentials: list[str],
) -> ScopedSecrets:
  """the credential scope a summoned bro runs with under its credential overrides."""
  scoped = scoped_secrets(bro_name, Surface.BRO_RUN, credential_instances=credential_instances)
  swap_grants, swap_revokes = credential_swap_overrides(scoped, swap_credentials)
  return finalize_scoped_secrets(
    scoped,
    grant=[*grant, *swap_grants],
    revoke=[*revoke, *swap_revokes],
  )


class LaunchScopeError(Exception):
  """a launch failed its scope computation or preflight: a malformed or no-op
  grant/revoke override, an unknown `[tool.bro] creds` kind, an unknown summon
  target, or an unknown/unresolvable required secret."""


# the unified --grant/--revoke value syntax: a leading `@` marks a bro summon
# target (`@reviewer`), any other value is a credential name.
_BRO_MARK = '@'


def split_scope_overrides(values: list[str]) -> tuple[list[str], list[str]]:
  """split unified grant/revoke values into (credential names, bro names)."""
  credential_names: list[str] = []
  bro_names: list[str] = []
  for value in values:
    if value.startswith(_BRO_MARK):
      name = value.removeprefix(_BRO_MARK)
      if name == '':
        raise ValueError(f'malformed grant/revoke {value!r}: expected {_BRO_MARK}<bro-name>')
      bro_names.append(name)
    else:
      credential_names.append(value)
  return credential_names, bro_names


def credential_swaps_by_kind(targets: list[str]) -> dict[str, str]:
  """validate credential swap targets and index them by kind."""
  swaps: dict[str, str] = {}
  for target in targets:
    kind, _ = credentials.parse_name(target)
    if kind in swaps:
      raise ValueError(f'credential kind {kind!r} is named by --swap-cred more than once')
    swaps[kind] = target
  return swaps


def credential_swap_overrides(
  scoped: ScopedSecrets, targets: list[str]
) -> tuple[list[str], list[str]]:
  """lower credential selections to strict same-kind grant/revoke overrides."""
  selected_by_kind: dict[str, str] = {}
  for selected in scoped.required | scoped.optional:
    kind, _ = credentials.parse_name(selected)
    if kind in selected_by_kind:
      raise ValueError(f'credential kind {kind!r} has multiple selected names')
    selected_by_kind[kind] = selected

  grants: list[str] = []
  revokes: list[str] = []
  for kind, target in credential_swaps_by_kind(targets).items():
    selected = selected_by_kind.get(kind)
    if selected is None:
      raise ValueError(f'cannot swap credential kind {kind!r}: not in the scoped credential set')
    if selected == target:
      raise ValueError(f'cannot swap credential {target!r}: already selected')
    grants.append(target)
    revokes.append(selected)
  return grants, revokes


def preflight_scoped_launch(
  scoped: ScopedSecrets,
  bro_name: str,
  *,
  grant: list[str],
  revoke: list[str],
  swap_credentials: list[str],
) -> tuple[ScopedSecrets, set[str], dict[str, bytes]]:
  """the scope preflight every launch surface runs before creating anything
  (worktree, container, workspace dir): split the unified grant/revoke overrides
  (`split_scope_overrides`), lower credential swaps, finalize the credential scope
  (`finalize_scoped_secrets`), compute the summon allow-list of a launch running
  as `bro_name` (`summon_control.summon_allow_list`), and hydrate the scoped store
  (`credentials.build_scoped_store`) — any failure raised as a single
  `LaunchScopeError` for the caller to render on its own error surface.

  returns (scope, allow-list, store). the container launch path rebuilds the
  store at create, so container callers drop it — the build is the preflight
  itself; a host session materializes the returned one.
  """
  # imported here, not at module level, parallel to every launch-surface import of
  # summon_control: the module sits on the pre-gate launch path
  from bro.launch.summon_control import summon_allow_list

  try:
    scoped, grant_bros, revoke_bros = _finalize_credential_scope(
      scoped, grant, revoke, swap_credentials
    )
    may_summon = summon_allow_list(bro_name, grant=grant_bros, revoke=revoke_bros)
    store = credentials.build_scoped_store(scoped.required, optional=scoped.optional)
  except (ValueError, credentials.SecretNotFound) as e:
    raise LaunchScopeError(str(e)) from e
  return scoped, may_summon, store


def _finalize_credential_scope(
  scoped: ScopedSecrets, grant: list[str], revoke: list[str], swap_credentials: list[str]
) -> tuple[ScopedSecrets, list[str], list[str]]:
  """finalize credential and summon-target overrides over a computed scope."""
  grant_credentials, grant_bros = split_scope_overrides(grant)
  revoke_credentials, revoke_bros = split_scope_overrides(revoke)
  swap_grants, swap_revokes = credential_swap_overrides(scoped, swap_credentials)
  finalized = finalize_scoped_secrets(
    scoped,
    grant=[*grant_credentials, *swap_grants],
    revoke=[*revoke_credentials, *swap_revokes],
  )
  return finalized, grant_bros, revoke_bros


def launch_view_store(
  scoped: ScopedSecrets, *, grant: list[str], revoke: list[str], swap_credentials: list[str]
) -> credentials.Store:
  """the lazy counterpart of `preflight_scoped_launch`'s hydrated store: the
  launch's credential binding as a kinds-only read-through store
  (`credentials.scoped_view_store`), for host-side code that reads a credential
  on the session's behalf before the launch exists. `grant`/`revoke` are the unified
  override values and `swap_credentials` carries same-kind replacements; the `@bro`
  halves shape only the summon side and are ignored here. raises `LaunchScopeError`
  like the preflight."""
  try:
    finalized, _, _ = _finalize_credential_scope(scoped, grant, revoke, swap_credentials)
    return credentials.scoped_view_store(finalized.required, optional=finalized.optional)
  except (ValueError, credentials.SecretNotFound) as e:
    raise LaunchScopeError(str(e)) from e
