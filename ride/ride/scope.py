"""per-surface launch scoping of a bro run: which credentials each launch
surface hydrates, which bros the session may summon, and which party actions it
may request, computed from static seeds under project, host, and launch layers;
`bind_launch_llm` settles the launch's LLM recipe over the same host entries.
"""

import contextlib
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from bro.base import credentials, host_config
from bro.base.scope import (
  ScopeLayer,
  apply_idempotent,
  credential_grant_kind,
  credential_revoke_name,
  split_scope_overrides,
  validate_scope_layer,
)
from bro.launch.llm_flags import with_host_defaults
from bro.summon import PARTY_START_BOXED
from ride.repository import Repository, attachment_identities, open_repository
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from bro.llm.llm import LLMSpec
  from bro.mcp import Harness
  from ride.harness import Harness as Driver

_RECORDING_CREDENTIAL = 'trails'
DEFAULT_PERMITS = frozenset({PARTY_START_BOXED})


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


def bind_launch_llm(attachment: Optional[str], bro_name: str, llm: Optional[str]) -> Optional[str]:
  """the canonical `--llm` value a launch as `bro_name` runs under: `llm` — its
  own canonical selection, or None — with the host's per-bro defaults for the
  attachment beneath it (`with_host_defaults`); None when nothing names a slot.
  What the launch records and forwards, so the recipe is settled once, on the
  host, wherever the session runs."""
  from bro.llm.providers import LLMSelection, parse

  identities = None if attachment is None else attachment_identities(attachment)
  selection = LLMSelection() if llm is None else parse(llm)
  selection = with_host_defaults(selection, identities, bro_name)
  return None if selection.is_empty() else selection.format()


def launch_llm_spec(
  driver: 'Driver', attachment: Optional[str], bro_name: str, llm: Optional[str]
) -> 'LLMSpec':
  """the recipe a launch as `bro_name` under `driver` runs: `llm` settled over
  the host's per-bro default (`bind_launch_llm`) and resolved within the driver.
  Raises `LaunchScopeError` for a bro the installation does not declare."""
  try:
    return driver.resolve_llm(bind_launch_llm(attachment, bro_name, llm), bro_name)
  except KeyError as error:
    raise LaunchScopeError(f'unknown bro {bro_name!r}') from error


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
      project_layers = (ScopeLayer(config.grant, config.revoke, source='[tool.bro]'),)
  selected = binding if binding is not None else bind_launch_credentials(attachment, bro_name)
  return (*project_layers, *selected.scope_layers)


def _namespace_values(layer: ScopeLayer, index: int) -> tuple[list[str], list[str]]:
  grant = split_scope_overrides(layer.grant)[index]
  revoke = split_scope_overrides(layer.revoke)[index]
  return grant, revoke


def validate_permits(values: Collection[str]) -> None:
  """Check explicit permit names against only the worker types they name."""
  from bro.worker_types import installed_type

  by_type: dict[str, set[str]] = {}
  for value in values:
    type_name, leaf = value.split('.', 1)
    by_type.setdefault(type_name, set()).add(leaf)
  for type_name, leaves in sorted(by_type.items()):
    try:
      worker_type = installed_type(type_name)
    except KeyError as error:
      raise ValueError(error.args[0]) from error
    unknown = sorted(leaves - set(worker_type.permits))
    if unknown:
      rendered = ', '.join(f':{type_name}.{leaf}' for leaf in unknown)
      raise ValueError(f'worker type {type_name!r} does not declare permit(s): {rendered}')


def effective_permits(
  layers: Sequence[ScopeLayer], *, grant: Sequence[str], revoke: Sequence[str], strict: bool
) -> set[str]:
  """Apply configured and request permit layers to the framework seed."""
  configured: list[tuple[list[str], list[str]]] = []
  explicit: set[str] = set()
  for layer in layers:
    layer_grant, layer_revoke = _namespace_values(layer, 2)
    configured.append((layer_grant, layer_revoke))
    explicit.update(layer_grant)
    explicit.update(layer_revoke)
  grant_permits = split_scope_overrides(grant)[2]
  revoke_permits = split_scope_overrides(revoke)[2]
  explicit.update(grant_permits)
  explicit.update(revoke_permits)
  validate_permits(explicit)
  permits = set(DEFAULT_PERMITS)
  for layer_grant, layer_revoke in configured:
    permits = apply_idempotent(permits, grant=layer_grant, revoke=layer_revoke)
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


def _credential_picks(values: Sequence[str], *, subject: str) -> dict[str, str]:
  picks: dict[str, str] = {}
  for value in values:
    kind, instance = credentials.parse_name(value)
    if instance is None:
      raise ValueError(
        f'{subject} {value!r} names no instance; write {value!r}+<instance>, '
        f'or {value!r}+ for the empty instance'
      )
    if kind in picks:
      raise ValueError(f'{subject} selects credential kind {kind!r} more than once')
    picks[kind] = instance
  return picks


def _credential_changes(layer: ScopeLayer) -> tuple[set[str], set[str]]:
  grant_values, _, _ = split_scope_overrides(layer.grant)
  revoke_values, _, _ = split_scope_overrides(layer.revoke)
  context = (
    'launch flags'
    if layer.source == 'launch flags'
    else 'project'
    if layer.source == '[tool.bro]'
    else 'host config'
  )
  grants = {credential_grant_kind(value, context=context) for value in grant_values}
  revokes = {credential_revoke_name(value) for value in revoke_values}
  if grants & revokes:
    overlap = ', '.join(sorted(grants & revokes))
    raise ValueError(f'cannot grant and revoke the same credential kind: {overlap}')
  return grants, revokes


def _validate_registered_credentials(layers: Sequence[ScopeLayer], registered: set[str]) -> None:
  for layer in layers:
    grants, revokes = _credential_changes(layer)
    picks = _credential_picks(
      layer.creds, subject='--cred' if layer.source == 'launch flags' else 'creds'
    )
    unknown = sorted((grants | revokes | set(picks)) - registered)
    if len(unknown) == 0:
      continue
    replacement = (
      '; move host-wide picks or scope changes into the project entries that use them'
      if layer.source == host_config.DEFAULTS_LAYER
      else ''
    )
    raise ValueError(
      f'{layer.source or "scope layer"} names unregistered credential kind(s): '
      f'{", ".join(unknown)}{replacement}'
    )


def _fold_credential_kinds(kinds: Collection[str], layers: Sequence[ScopeLayer]) -> set[str]:
  result = set(kinds)
  for layer in layers:
    grants, revokes = _credential_changes(layer)
    result = apply_idempotent(result, grant=grants, revoke=revokes)
  return result


def scoped_secrets(
  bro_name: str,
  recipe: ScopeRecipe,
  *,
  attachment: Optional[str] = None,
  attachment_repository: Optional[Repository] = None,
  llm_spec: Optional['LLMSpec'] = None,
  cred: Sequence[str] = (),
  grant: Sequence[str] = (),
  revoke: Sequence[str] = (),
  recording: bool = True,
  check_selection: bool = True,
) -> ScopedSecrets:
  """Compute one launch's credential tiers and instance selection."""
  from bro.registry import create_bro

  with launch_scope_errors():
    binding = bind_launch_credentials(attachment, bro_name)
    configured_layers = configured_scope_layers(
      attachment,
      bro_name,
      binding,
      attachment_repository,
    )
    launch_layer = ScopeLayer(tuple(grant), tuple(revoke), tuple(cred), 'launch flags')
    validate_scope_layer(launch_layer, context='launch flags')
    registered = set(credentials.default_registry())
    _validate_registered_credentials((*configured_layers, launch_layer), registered)
    launch_picks = _credential_picks(cred, subject='--cred')
  selection = {**binding.instances, **launch_picks}
  revoked: set[str] = set()
  for layer in (*configured_layers, launch_layer):
    grants, revokes = _credential_changes(layer)
    revoked.difference_update(grants)
    revoked.update(revokes)
  required_needs: set[str] = set()
  optional_needs: set[str] = set()
  with (
    launch_scope_errors(),
    credentials.as_default_store(selection_store(selection, revoked=revoked)),
  ):
    try:
      bro = create_bro(bro_name)
    except KeyError as error:
      raise LaunchScopeError(f'unknown bro {bro_name!r}') from error
    required_needs.update(bro.needed_secrets(harness=recipe.harness))
    optional_needs.update(bro.optional_secrets(harness=recipe.harness))
    if recipe.auth_secret is not None:
      required_needs.add(recipe.auth_secret)
    if recipe.llm_key:
      required_needs.update((llm_spec if llm_spec is not None else bro.llm_spec).needed_secrets())
  if recording:
    optional_needs.add(_RECORDING_CREDENTIAL)
  optional_needs.difference_update(required_needs)
  needs = required_needs | optional_needs
  configured_kinds = _fold_credential_kinds(needs, configured_layers)
  if check_selection:
    configured_kinds_with_recording = _fold_credential_kinds(
      needs | {_RECORDING_CREDENTIAL}, configured_layers
    )
    _require_bro_layer_selections_read(
      binding,
      bro_name,
      configured_kinds_with_recording,
    )
  held_kinds = _fold_credential_kinds(configured_kinds, (launch_layer,))
  unheld_picks = sorted(set(launch_picks) - held_kinds)
  if unheld_picks:
    rendered = ', '.join(
      credentials.storage_name(kind, launch_picks[kind]) for kind in unheld_picks
    )
    grants = ', '.join(f'--grant {kind}' for kind in unheld_picks)
    raise LaunchScopeError(
      f'--cred selects {rendered}, but the launch does not hold its kind; add {grants}'
    )
  optional = held_kinds & optional_needs
  return ScopedSecrets(
    required=held_kinds - optional,
    optional=optional,
    selection=selection,
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
  running as `bro_name` (`bro_worker.summon_allow_list`) from the `@bro`
  halves of the unified overrides (`split_scope_overrides`; the credential halves
  already shaped `scoped`), and hydrate the scoped store
  (`credentials.build_scoped_store`) — any failure raised as a single
  `LaunchScopeError` for the caller to render on its own error surface.

  returns (allow-list, permits, store). the container launch path rebuilds the store at
  create, so container callers drop it — the build is the preflight itself; a
  unboxed session materializes the returned one.
  """
  from ride.bro_worker import summon_allow_list

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
