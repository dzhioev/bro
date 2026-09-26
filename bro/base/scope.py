"""The unified launch-scope grant and revoke grammar."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from bro.base import credentials

BRO_MARK = '@'
SECTION_MARK = ':'
_SEGMENT = re.compile(r'[a-z][a-z0-9-]*')
_RETIRED_LAUNCH_NAMES = {
  ':bro.party.start.boxed': ':launch.bro.party.boxed',
  ':bro.party.start.unboxed': ':launch.bro.party.unboxed',
  ':bro.party.join': ':launch.bro.party.join',
  ':webview.vnc': ':launch.webview.vnc',
}


def launch_choices() -> str:
  return ':launch.<type>[.<field>[.<value>]] or @<bro>'


def launch_name(value: str) -> str:
  """Validate one launch authority name and return its grant spelling."""
  if not isinstance(value, str):
    raise ValueError(f'unknown launch name {value!r}; expected {launch_choices()}')
  replacement = _RETIRED_LAUNCH_NAMES.get(value)
  if replacement is not None:
    raise ValueError(f'retired permit {value!r}; use {replacement}')
  if value.startswith(BRO_MARK):
    if value == BRO_MARK:
      raise ValueError(f'malformed grant/revoke {value!r}: expected {BRO_MARK}<bro-name>')
    return value
  if not value.startswith(SECTION_MARK):
    raise ValueError(f'unknown launch name {value!r}; expected {launch_choices()}')
  segments = value.removeprefix(SECTION_MARK).split('.')
  if any(_SEGMENT.fullmatch(segment) is None for segment in segments):
    raise ValueError(f'unknown launch name {value!r}; expected dot-separated lowercase segments')
  section = segments[0]
  if section == 'creds':
    raise ValueError(
      f'{value!r} names creds as a path; use creds / --cred to select an instance and '
      'grant <kind> to hold it'
    )
  if section != 'launch':
    raise ValueError(f'unknown permission section {section!r} in {value!r}; expected launch')
  if len(segments) == 1:
    raise ValueError("bare permission section ':launch' is malformed; name a mission type")
  if len(segments) >= 3 and segments[1:3] == ['bro', 'bros']:
    raise ValueError(f'{value!r} spells a bro target as a path; use @<bro>')
  return value


@dataclass(frozen=True)
class ScopeLayer:
  """One idempotent configuration layer in the unified scope grammar."""

  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  creds: tuple[str, ...] = ()
  source: str = field(default='', compare=False)


def split_scope_overrides(values: Iterable[str]) -> tuple[list[str], list[str]]:
  """Split unified values into credential kinds and launch names."""
  credential_names: list[str] = []
  launch_names: list[str] = []
  for value in values:
    if value.startswith((BRO_MARK, SECTION_MARK)):
      launch_names.append(launch_name(value))
    else:
      credential_names.append(value)
  return credential_names, launch_names


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
  if value.startswith((BRO_MARK, SECTION_MARK)):
    return launch_name(value)
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
  if value.startswith((BRO_MARK, SECTION_MARK)):
    return launch_name(value)
  return credential_revoke_name(value)


def validate_scope_layer(layer: ScopeLayer, *, context: str = 'host config') -> None:
  """Validate one layer without consulting installed registries."""
  grant_credentials, grant_launch = split_scope_overrides(layer.grant)
  revoke_credentials, revoke_launch = split_scope_overrides(layer.revoke)
  grant_kinds = {credential_grant_kind(value, context=context) for value in grant_credentials}
  revoke_kinds = {credential_revoke_name(value) for value in revoke_credentials}
  overlap = grant_kinds & revoke_kinds | (set(grant_launch) & set(revoke_launch))
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


def launch_names(
  launch: object, *, include_bros: bool = True, include_all_keys: bool = False
) -> tuple[str, ...]:
  """Render a launch section in the grant/revoke spelling."""
  from bro.worker_types import parse_launch

  parsed = parse_launch(launch)
  names: set[str] = set()
  for worker_type, payload in parsed.items():
    if include_all_keys or len(payload) == 0:
      names.add(f':launch.{worker_type}')
    for field_name, value in payload.items():
      if isinstance(value, bool):
        if value:
          names.add(f':launch.{worker_type}.{field_name}')
        continue
      if worker_type == 'bro' and field_name == 'bros':
        if include_bros:
          names.update(f'@{member}' for member in value)
        continue
      names.update(f':launch.{worker_type}.{field_name}.{member}' for member in value)
  return tuple(sorted(names))
