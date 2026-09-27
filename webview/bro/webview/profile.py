"""Versioned Playwright storage profiles accepted by webview."""

from __future__ import annotations

import json
from typing import Any, NoReturn

PROFILE_MAX_BYTES = 32 << 20
PLAYWRIGHT_MCP_PROFILE_VERSIONS = {1: '0.0.82'}
PROFILE_VERSION = max(PLAYWRIGHT_MCP_PROFILE_VERSIONS)


class ProfileError(ValueError):
  """A browser profile violates the webview profile contract."""


def _reject_json_constant(_constant: str) -> NoReturn:
  raise ProfileError('profile is not valid JSON')


def _storage_list(value: Any, field: str) -> list[Any]:
  if not isinstance(value, list):
    raise ProfileError(f"profile field '{field}' must be a list")
  return value


def decode_profile(raw: str) -> dict[str, Any]:
  """Validate a stored profile and return its Playwright storage state."""
  if len(raw.encode()) > PROFILE_MAX_BYTES:
    raise ProfileError(f'profile exceeds the {PROFILE_MAX_BYTES}-byte limit')
  try:
    value = json.loads(raw, parse_constant=_reject_json_constant)
  except json.JSONDecodeError as error:
    raise ProfileError('profile is not valid JSON') from error
  if not isinstance(value, dict):
    raise ProfileError('profile must be an object')
  expected = {'profile_version', 'cookies', 'origins'}
  unknown = sorted(set(value) - expected)
  missing = sorted(expected - set(value))
  if unknown:
    raise ProfileError(f'profile has unknown field(s): {", ".join(unknown)}')
  if missing:
    raise ProfileError(f'profile is missing field(s): {", ".join(missing)}')

  version = value['profile_version']
  if not isinstance(version, int) or isinstance(version, bool):
    raise ProfileError("profile field 'profile_version' must be an integer")
  if version > PROFILE_VERSION:
    raise ProfileError(
      f'profile version {version} is newer than supported profile version {PROFILE_VERSION}'
    )

  cookies = _storage_list(value['cookies'], 'cookies')
  origins = _storage_list(value['origins'], 'origins')
  for index, origin in enumerate(origins):
    if not isinstance(origin, dict):
      raise ProfileError(f'profile origin {index} must be an object')
    required = {'origin', 'localStorage'}
    allowed = required | {'indexedDB'}
    unknown_origin_fields = sorted(set(origin) - allowed)
    missing_origin_fields = sorted(required - set(origin))
    if unknown_origin_fields:
      raise ProfileError(
        f'profile origin {index} has unknown field(s): {", ".join(unknown_origin_fields)}'
      )
    if missing_origin_fields:
      raise ProfileError(
        f'profile origin {index} is missing field(s): {", ".join(missing_origin_fields)}'
      )
    if not isinstance(origin['origin'], str):
      raise ProfileError(f"profile origin {index} field 'origin' must be a string")
    _storage_list(origin['localStorage'], f'origins[{index}].localStorage')
    if 'indexedDB' in origin:
      _storage_list(origin['indexedDB'], f'origins[{index}].indexedDB')

  return {'cookies': cookies, 'origins': origins}
