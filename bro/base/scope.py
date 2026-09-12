"""The unified launch-scope grant and revoke grammar."""

from collections.abc import Iterable
from dataclasses import dataclass

from bro.base import credentials

BRO_MARK = '@'
PERMIT_MARK = ':'
PARTY_START_BOXED = 'party.start.boxed'
PARTY_START_UNBOXED = 'party.start.unboxed'
PARTY_JOIN = 'party.join'
PARTY_PERMIT_NAMES = (PARTY_START_BOXED, PARTY_START_UNBOXED, PARTY_JOIN)
PARTY_PERMITS = frozenset(PARTY_PERMIT_NAMES)
DEFAULT_PERMITS = frozenset({PARTY_START_BOXED})


def permit_choices() -> str:
  return ', '.join(f'{PERMIT_MARK}{name}' for name in PARTY_PERMIT_NAMES)


@dataclass(frozen=True)
class ScopeLayer:
  """One idempotent configuration layer in the unified scope grammar."""

  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()


def split_scope_overrides(values: Iterable[str]) -> tuple[list[str], list[str], list[str]]:
  """Split unified values into credential names, bro names, and permit names."""
  credential_names: list[str] = []
  bro_names: list[str] = []
  permits: list[str] = []
  for value in values:
    if value.startswith(BRO_MARK):
      name = value.removeprefix(BRO_MARK)
      if name == '':
        raise ValueError(f'malformed grant/revoke {value!r}: expected {BRO_MARK}<bro-name>')
      bro_names.append(name)
    elif value.startswith(PERMIT_MARK):
      permit = value.removeprefix(PERMIT_MARK)
      if permit not in PARTY_PERMITS:
        raise ValueError(f'unknown permit {value!r}; expected one of {permit_choices()}')
      permits.append(permit)
    else:
      credential_names.append(value)
  return credential_names, bro_names, permits


def scope_override_key(value: str) -> str:
  """The namespace-qualified identity changed by one grant value."""
  if value.startswith((BRO_MARK, PERMIT_MARK)):
    split_scope_overrides((value,))
    return value
  kind, _ = credentials.parse_name(value)
  return kind


def credential_revoke_name(value: str) -> str:
  """Validate and return the kind named by a credential revoke."""
  kind, instance = credentials.parse_name(value)
  if instance is not None:
    raise ValueError(
      f'cannot revoke credential instance {value!r}; revoke its kind instead (--revoke {kind})'
    )
  return kind


def scope_revoke_key(value: str) -> str:
  """The namespace-qualified identity changed by one revoke value."""
  if value.startswith((BRO_MARK, PERMIT_MARK)):
    split_scope_overrides((value,))
    return value
  return credential_revoke_name(value)


def validate_scope_layer(layer: ScopeLayer, *, allow_credential_instances: bool) -> None:
  """Validate one configuration layer without consulting installed registries."""
  grant_credentials, _, _ = split_scope_overrides(layer.grant)
  revoke_credentials, _, _ = split_scope_overrides(layer.revoke)
  for value in grant_credentials:
    _, instance = credentials.parse_name(value)
    if instance is not None and not allow_credential_instances:
      raise ValueError(
        f'credential instance {value!r} is host-specific; grant its bare kind in the project'
      )
  for value in revoke_credentials:
    credential_revoke_name(value)
  grant_keys = [scope_override_key(value) for value in layer.grant]
  revoke_keys = [scope_revoke_key(value) for value in layer.revoke]
  if len(grant_keys) != len(set(grant_keys)):
    raise ValueError('a scope name is granted more than once in one layer')
  if len(revoke_keys) != len(set(revoke_keys)):
    raise ValueError('a scope name is revoked more than once in one layer')
  overlap = set(grant_keys) & set(revoke_keys)
  if overlap:
    raise ValueError(f'cannot grant and revoke the same scope name: {", ".join(sorted(overlap))}')


def apply_idempotent(
  seed: Iterable[str], *, grant: Iterable[str], revoke: Iterable[str]
) -> set[str]:
  """Apply a configuration layer where restating either state is accepted."""
  result = set(seed)
  result.update(grant)
  result.difference_update(revoke)
  return result
