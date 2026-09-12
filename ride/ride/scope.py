"""per-surface launch scoping of a bro run: which credentials each launch
surface hydrates, which bros the session may summon, and which party actions it
may request, computed from static seeds under project, host, and launch layers.
"""

import contextlib
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from bro.base import credentials, host_config
from bro.base.scope import (
  DEFAULT_PERMITS,
  ScopeLayer,
  apply_idempotent,
  split_scope_overrides,
)
from ride.repository import Repository, attachment_identities, open_repository
from ride.workspace.store import (
  ScopedSecrets,
  credential_revoke_kind,
  finalize_scoped_secrets,
  grant_instances,
)

if TYPE_CHECKING:
  from bro.llm.llm import LLMSpec
  from bro.mcp import Harness

# the recording credential every surface hydrates best-effort, regardless of bro:
# it selects a backend (`bro.trails.store.resolve_config`) rather than enabling
# recording, so a launch that cannot resolve it still records.
_TRAILS_BASELINE = frozenset({'trails'})


class LaunchScopeError(Exception):
  """a launch failed its scope computation or preflight: a bro the installation
  does not declare, a malformed host config or a per-bro selection of a kind the
  launch does not read, a malformed or no-op grant/revoke override, an unknown
  summon target, or an unknown/unresolvable required secret."""


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


def configured_scope_layers(
  attachment: Optional[str],
  bro_name: str,
  binding: Optional[host_config.CredentialSelection] = None,
  attachment_repository: Optional[Repository] = None,
) -> tuple[ScopeLayer, ...]:
  """The project's layer followed by the matching host-config layers."""
  project_layers: tuple[ScopeLayer, ...] = ()
  if attachment is not None:
    repository = attachment_repository or open_repository(attachment)
    if repository.read_file('pyproject.toml') is not None:
      config = repository.project_config()
      project_layers = (ScopeLayer(config.grant, config.revoke),)
  selected = binding if binding is not None else bind_launch_credentials(attachment, bro_name)
  return (*project_layers, *selected.scope_layers)


def _namespace_values(layer: ScopeLayer, index: int) -> tuple[list[str], list[str]]:
  grant = split_scope_overrides(layer.grant)[index]
  revoke = split_scope_overrides(layer.revoke)[index]
  return grant, revoke


def effective_permits(
  layers: Sequence[ScopeLayer], *, grant: Sequence[str], revoke: Sequence[str], strict: bool
) -> set[str]:
  """Apply configured and request permit layers to the framework seed."""
  permits = set(DEFAULT_PERMITS)
  for layer in layers:
    layer_grant, layer_revoke = _namespace_values(layer, 2)
    permits = apply_idempotent(permits, grant=layer_grant, revoke=layer_revoke)
  grant_permits = split_scope_overrides(grant)[2]
  revoke_permits = split_scope_overrides(revoke)[2]
  if strict:
    return credentials.apply_grant_revoke(
      permits, grant=grant_permits, revoke=revoke_permits, subject='permit set'
    )
  return apply_idempotent(permits, grant=grant_permits, revoke=revoke_permits)


def selection_store(
  selection: Mapping[str, str], *, revoked: Collection[str] = ()
) -> credentials.Store:
  """the ambient store under an explicit kind-to-instance selection, the
  `revoked` kinds resolving as absent."""
  registry = credentials.default_registry()
  return credentials.Store(
    registry,
    credentials.STORE_DIR,
    {kind: instance for kind, instance in selection.items() if kind in registry},
    readable=None if len(revoked) == 0 else registry.keys() - set(revoked),
  )


def scoped_secrets(
  bro_name: str,
  recipe: ScopeRecipe,
  *,
  attachment: Optional[str] = None,
  attachment_repository: Optional[Repository] = None,
  llm_spec: Optional['LLMSpec'] = None,
  grant: Sequence[str] = (),
  revoke: Sequence[str] = (),
  check_selection: bool = True,
) -> ScopedSecrets:
  """the credential scope of a launch running as `bro_name` under `recipe`.

  Harness implementations own their recipes; launch surfaces and summon lowering
  share this one computation over them. Required hydration is strict, so each
  recipe requests only what it actually uses.

  `llm_spec` is the recipe the launch settled on (`--provider` / `--model` /
  `--llm`), whose key the scope hydrates in place of the bro's own — a run
  against another provider needs that provider's key, not the declared one.

  `grant` / `revoke` are the launch's unified override values
  (`split_scope_overrides`): credential values shape the scope here, while
  `@bro` and `:permit` values shape the other authority sets. The bro's declarations
  are evaluated under the selection the launch ends with — the host's defaults,
  the operated project's and this bro's layers, and the request's instance
  grants. The project's per-bro `grant` kinds join the required tier under that
  selection, and a per-bro `creds` selection of a kind the launch does not read
  fails it — unless `check_selection` is off, for a scope computed to reason
  about rather than to hydrate. Raises `ValueError` on a no-op override.
  """
  from bro.registry import create_bro

  grant_credentials, _, _ = split_scope_overrides(grant)
  revoke_credentials, _, _ = split_scope_overrides(revoke)
  with launch_scope_errors():
    binding = bind_launch_credentials(attachment, bro_name)
    configured_layers = configured_scope_layers(
      attachment,
      bro_name,
      binding,
      attachment_repository,
    )
  selection = dict(binding.instances)
  for kind, instance in grant_instances(grant_credentials).items():
    if instance is not None:
      selection[kind] = instance
  revoked: set[str] = set()
  for layer in configured_layers:
    layer_grant, layer_revoke = _namespace_values(layer, 0)
    revoked.difference_update(credentials.parse_name(name)[0] for name in layer_grant)
    revoked.update(credential_revoke_kind(name) for name in layer_revoke)
  revoked.difference_update(credentials.parse_name(name)[0] for name in grant_credentials)
  revoked.update(credential_revoke_kind(name) for name in revoke_credentials)
  required: set[str] = set()
  optional = set(recipe.optional_baseline)
  with credentials.as_default_store(selection_store(selection, revoked=revoked)):
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
  configured_scope = ScopedSecrets(
    required=required,
    optional=optional,
    selection=dict(binding.instances),
  )
  for layer in configured_layers:
    layer_grant, layer_revoke = _namespace_values(layer, 0)
    configured_scope = finalize_scoped_secrets(
      configured_scope,
      grant=[credentials.parse_name(name)[0] for name in layer_grant],
      revoke=layer_revoke,
      strict=False,
    )
  if check_selection:
    # the recording kind counts as read under `--no-trails`, which empties the recipe's baseline
    _require_bro_layer_selections_read(
      binding,
      bro_name,
      configured_scope.required | configured_scope.optional | _TRAILS_BASELINE,
    )
  return finalize_scoped_secrets(
    configured_scope,
    grant=grant_credentials,
    revoke=revoke_credentials,
  )


def _require_bro_layer_selections_read(
  binding: host_config.CredentialSelection, bro_name: str, readable: set[str]
) -> None:
  unread = [
    f'{credentials.storage_name(kind, binding.instances[kind])} ({layer})'
    for kind, layer in sorted(binding.layers.items())
    if layer in host_config.BRO_LAYERS and kind not in readable
  ]
  if len(unread) > 0:
    raise LaunchScopeError(
      f'{host_config.HOST_CONFIG_FILE}: bros.{bro_name} selects {", ".join(unread)}, which '
      'this launch does not read; move the entry from "creds" to "grant" to add the kind to '
      'its scope'
    )


def credential_store(scoped: ScopedSecrets) -> credentials.Store:
  """The ambient store under a launch's explicit selection."""
  return selection_store(scoped.selection)


class HydratedStore(dict[str, bytes]):
  """Scoped-store files carrying the declared kinds that resolved."""

  def __init__(self, files: dict[str, bytes], kinds: frozenset[str]):
    super().__init__(files)
    self.kinds = kinds


def preflight_scoped_launch(
  scoped: ScopedSecrets,
  bro_name: str,
  *,
  attachment: Optional[str] = None,
  attachment_repository: Optional[Repository] = None,
  grant: list[str],
  revoke: list[str],
) -> tuple[set[str], set[str], HydratedStore]:
  """the scope preflight every launch surface runs before creating anything
  (worktree, container, workspace dir): compute the summon allow-list of a launch
  running as `bro_name` (`summon_control.summon_allow_list`) from the `@bro`
  halves of the unified overrides (`split_scope_overrides`; the credential halves
  already shaped `scoped`), and hydrate the scoped store
  (`credentials.build_scoped_store`) — any failure raised as a single
  `LaunchScopeError` for the caller to render on its own error surface.

  returns (allow-list, permits, store). the container launch path rebuilds the store at
  create, so container callers drop it — the build is the preflight itself; a
  unboxed session materializes the returned one.
  """
  # imported here, not at module level, parallel to every launch-surface import of
  # summon_control: the module sits on the pre-gate launch path
  from ride.summon_control import summon_allow_list

  with launch_scope_errors():
    configured_layers = configured_scope_layers(
      attachment,
      bro_name,
      attachment_repository=attachment_repository,
    )
    may_summon = summon_allow_list(
      bro_name,
      layers=configured_layers,
      grant=split_scope_overrides(grant)[1],
      revoke=split_scope_overrides(revoke)[1],
    )
    permits = effective_permits(
      configured_layers,
      grant=grant,
      revoke=revoke,
      strict=True,
    )
    files, hydrated_kinds = credentials.build_scoped_store(
      credential_store(scoped), scoped.required, optional=scoped.optional
    )
  return may_summon, permits, HydratedStore(files, hydrated_kinds)


def launch_view_store(scoped: ScopedSecrets) -> credentials.Store:
  """the lazy counterpart of `preflight_scoped_launch`'s hydrated store: the
  launch's credential binding as a kinds-only read-through store
  (`credentials.scoped_view_store`), for host-side code that reads a credential
  on the session's behalf before the launch exists. raises `LaunchScopeError`
  like the preflight; a read through the returned store is the caller's to wrap
  in `launch_scope_errors`, the store itself being a plain one."""
  with launch_scope_errors():
    return credentials.scoped_view_store(
      credential_store(scoped), scoped.required, optional=scoped.optional
    )
