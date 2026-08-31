"""per-surface launch scoping of a bro run: which credentials each launch
surface hydrates and which bros the session may summon, computed from the bro's
own declarations (manifest, optional tier, `may_summon`) against the host's
credential selection for the operated project.
"""

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from bro.base import credentials, host_config
from ride.repository import attachment_identities
from ride.workspace.store import ScopedSecrets, finalize_scoped_secrets

if TYPE_CHECKING:
  from bro.llm.llm import LLMSpec
  from bro.mcp import Harness

# the recording credential every surface hydrates best-effort, regardless of bro:
# it selects a backend (`bro.trails.store.resolve_config`) rather than enabling
# recording, so a launch that cannot resolve it still records.
_TRAILS_BASELINE = frozenset({'trails'})


class LaunchScopeError(Exception):
  """a launch failed its scope computation or preflight: a bro the installation
  does not declare, a malformed or no-op grant/revoke override, an unknown summon
  target, or an unknown/unresolvable required secret."""


@contextlib.contextmanager
def launch_scope_errors() -> Iterator[None]:
  """raise whatever a launch's credential scope fails with as a single
  `LaunchScopeError`, for the surface to render on its own error path. a caller
  deferring a read through `launch_view_store` wraps it too, so a failure at that
  read names the launch rather than the credential it stopped on."""
  try:
    yield
  except (ValueError, credentials.SecretNotFound) as e:
    raise LaunchScopeError(str(e)) from e


@dataclass(frozen=True)
class ScopeRecipe:
  """the component and credential policy a harness mode scopes a bro through."""

  name: str
  harness: 'Harness'
  auth_secret: Optional[str]
  llm_key: bool
  optional_baseline: frozenset[str] = _TRAILS_BASELINE


BRO_RUN_RECIPE = ScopeRecipe(
  name='bro-run',
  harness='bro',
  auth_secret=None,
  llm_key=True,
)


def bind_launch_credentials(
  attachment: Optional[str], bro_name: str
) -> host_config.CredentialSelection:
  identities = None if attachment is None else attachment_identities(attachment)
  return host_config.launch_selection(identities, bro_name)


def scoped_secrets(
  bro_name: str,
  recipe: ScopeRecipe,
  *,
  attachment: Optional[str] = None,
  llm_spec: Optional['LLMSpec'] = None,
) -> ScopedSecrets:
  """the credential scope of a launch running as `bro_name` under `recipe`.

  Harness implementations own their recipes; launch surfaces and summon lowering
  share this one computation over them. Required hydration is strict, so each
  recipe requests only what it actually uses.

  `llm_spec` is the recipe the launch settled on (`--provider` / `--model` /
  `--llm`), whose key the scope hydrates in place of the bro's own — a run
  against another provider needs that provider's key, not the declared one.

  The scope carries host defaults plus the operated project's instance selection
  to each explicit store the launch constructs.
  """
  from bro.registry import create_bro

  binding = bind_launch_credentials(attachment, bro_name)
  required: set[str] = set()
  optional = set(recipe.optional_baseline)
  try:
    bro = create_bro(bro_name)
  except KeyError as e:
    raise LaunchScopeError(f'unknown bro {bro_name!r}') from e
  required.update(bro.needed_secrets(harness=recipe.harness))
  if recipe.auth_secret is not None:
    required.add(recipe.auth_secret)
  if recipe.llm_key:
    required.update((llm_spec if llm_spec is not None else bro.llm_spec).needed_secrets())
  optional.update(bro.optional_secrets(harness=recipe.harness))
  return ScopedSecrets(
    required=required,
    optional=optional,
    selection=dict(binding.instances),
  )


def summoned_credential_scope(
  bro_name: str,
  recipe: ScopeRecipe,
  *,
  attachment: Optional[str] = None,
  grant: list[str],
  revoke: list[str],
  llm_spec: Optional['LLMSpec'] = None,
) -> ScopedSecrets:
  """the credential scope a summoned bro runs with: its own scope under `recipe`
  — the child harness's — plus the request's overrides. `grant`/`revoke` are the
  credential halves of the request's unified values (`split_scope_overrides`) —
  the `@bro` halves shape the summon allow-list instead. Raises `ValueError` on
  a no-op override."""
  return finalize_scoped_secrets(
    scoped_secrets(bro_name, recipe, attachment=attachment, llm_spec=llm_spec),
    grant=grant,
    revoke=revoke,
  )


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


def credential_store(scoped: ScopedSecrets) -> credentials.Store:
  """The ambient store under a launch's explicit selection."""
  registry = credentials.default_registry()
  selection = {kind: instance for kind, instance in scoped.selection.items() if kind in registry}
  return credentials.Store(registry, credentials.STORE_DIR, selection)


class HydratedStore(dict[str, bytes]):
  """Scoped-store files carrying the declared kinds that resolved."""

  def __init__(self, files: dict[str, bytes], kinds: frozenset[str]):
    super().__init__(files)
    self.kinds = kinds


def preflight_scoped_launch(
  scoped: ScopedSecrets,
  bro_name: str,
  *,
  grant: list[str],
  revoke: list[str],
) -> tuple[ScopedSecrets, set[str], HydratedStore]:
  """the scope preflight every launch surface runs before creating anything
  (worktree, container, workspace dir): split the unified grant/revoke overrides
  (`split_scope_overrides`), finalize the credential scope
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
  from ride.summon_control import summon_allow_list

  with launch_scope_errors():
    scoped, grant_bros, revoke_bros = _finalize_credential_scope(scoped, grant, revoke)
    may_summon = summon_allow_list(bro_name, grant=grant_bros, revoke=revoke_bros)
    files, hydrated_kinds = credentials.build_scoped_store(
      credential_store(scoped), scoped.required, optional=scoped.optional
    )
  return scoped, may_summon, HydratedStore(files, hydrated_kinds)


def _finalize_credential_scope(
  scoped: ScopedSecrets, grant: list[str], revoke: list[str]
) -> tuple[ScopedSecrets, list[str], list[str]]:
  """split the unified overrides (`split_scope_overrides`) and finalize the
  credential tiers; returns the finalized scope plus the `@bro` halves
  (grant, revoke) for the summon side."""
  grant_credentials, grant_bros = split_scope_overrides(grant)
  revoke_credentials, revoke_bros = split_scope_overrides(revoke)
  finalized = finalize_scoped_secrets(scoped, grant=grant_credentials, revoke=revoke_credentials)
  return finalized, grant_bros, revoke_bros


def launch_view_store(
  scoped: ScopedSecrets, *, grant: list[str], revoke: list[str]
) -> credentials.Store:
  """the lazy counterpart of `preflight_scoped_launch`'s hydrated store: the
  launch's credential binding as a kinds-only read-through store
  (`credentials.scoped_view_store`), for host-side code that reads a credential
  on the session's behalf before the launch exists. `grant`/`revoke` are the
  unified override values; the `@bro` halves shape only the summon side and are
  ignored here. raises `LaunchScopeError` like the preflight; a read through the
  returned store is the caller's to wrap in `launch_scope_errors`, the store
  itself being a plain one."""
  with launch_scope_errors():
    finalized, _, _ = _finalize_credential_scope(scoped, grant, revoke)
    return credentials.scoped_view_store(
      credential_store(finalized), finalized.required, optional=finalized.optional
    )
