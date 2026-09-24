"""The unified launch-scope grant and revoke grammar."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from bro.base import credentials

BRO_MARK = '@'
PERMIT_MARK = ':'
_PERMIT_SEGMENT = re.compile(r'[a-z][a-z0-9-]*')


def permit_choices() -> str:
  return ':<type>.<leaf>'


def permit_name(value: str) -> str:
  """Validate one unmarked worker permit and return it."""
  segments = value.split('.') if isinstance(value, str) else []
  if len(segments) < 2 or any(_PERMIT_SEGMENT.fullmatch(segment) is None for segment in segments):
    raise ValueError(
      f'unknown permit {PERMIT_MARK + str(value)!r}; expected {permit_choices()} with '
      'dot-separated lowercase segments'
    )
  return value


@dataclass(frozen=True)
class ScopeLayer:
  """One idempotent configuration layer in the unified scope grammar."""

  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  creds: tuple[str, ...] = ()
  source: str = field(default='', compare=False)


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
      permits.append(permit_name(value.removeprefix(PERMIT_MARK)))
    else:
      credential_names.append(value)
  return credential_names, bro_names, permits


def credential_grant_kind(value: str, *, context: str = 'host config') -> str:
  """Validate and return the kind named by a credential grant."""
  kind, instance = credentials.parse_name(value)
  if instance is None:
    return kind
  subject = f'credential grant {value!r}'
  if context == 'launch flags':
    subject = f'--grant {value}'
    remedy = f'use --cred {value} plus --grant {kind}'
  elif context == 'project':
    remedy = f'grant bare kind {kind!r}, and select {value!r} in host config "creds" or with --cred'
  elif context == 'host config':
    remedy = (
      f'replace it with "creds": ["{value}"] plus "grant": ["{kind}"] '
      f'where the bro does not already need {kind!r}'
    )
  else:
    raise ValueError(f'unknown credential grant context {context!r}')
  raise ValueError(f'{subject} names a credential instance; {remedy}')


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


def validate_scope_layer(layer: ScopeLayer, *, context: str = 'host config') -> None:
  """Validate one layer without consulting installed registries."""
  grant_credentials, grant_bros, grant_permits = split_scope_overrides(layer.grant)
  revoke_credentials, revoke_bros, revoke_permits = split_scope_overrides(layer.revoke)
  grant_kinds = {credential_grant_kind(value, context=context) for value in grant_credentials}
  revoke_kinds = {credential_revoke_name(value) for value in revoke_credentials}
  grant_keys = [*(f'@{name}' for name in grant_bros), *(f':{name}' for name in grant_permits)]
  revoke_keys = [*(f'@{name}' for name in revoke_bros), *(f':{name}' for name in revoke_permits)]
  if len(grant_keys) != len(set(grant_keys)):
    raise ValueError('a non-credential scope name is granted more than once in one layer')
  if len(revoke_keys) != len(set(revoke_keys)):
    raise ValueError('a non-credential scope name is revoked more than once in one layer')
  overlap = grant_kinds & revoke_kinds | (set(grant_keys) & set(revoke_keys))
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
