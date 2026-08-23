"""Bearer tokens and the permissions each one carries."""

import dataclasses
import enum
import hmac
from collections.abc import Callable
from typing import Any, Optional

from bro.base import credentials
from bro.trails.model import LOOPBACK_HOSTS

TOKENS_SECRET = 'trails_tokens'
_DECLARED_PERMISSION = '_trails_permission'


class Permission(enum.Enum):
  """What a bearer token may do. The three are independent: a token that records
  need not read."""

  READ = 'read'
  WRITE = 'write'
  ADMIN = 'admin'


@dataclasses.dataclass(frozen=True)
class Token:
  name: str
  secret: str
  permissions: frozenset[Permission]


@dataclasses.dataclass(frozen=True)
class TokenTable:
  """The tokens a server accepts, each under a name and its own permissions."""

  tokens: tuple[Token, ...]

  @classmethod
  def from_config(cls, config: Any) -> 'TokenTable':
    """Read the `trails_tokens` credential:
    `{"tokens": {"<name>": {"token": "...", "permissions": [...]}}}`."""
    if not isinstance(config, dict) or set(config) != {'tokens'}:
      raise ValueError(f'the {TOKENS_SECRET} credential must be an object with only `tokens`')
    declared = config['tokens']
    if not isinstance(declared, dict) or len(declared) == 0:
      raise ValueError(f'{TOKENS_SECRET}.tokens must name at least one token')
    tokens = tuple(_token(name, entry) for name, entry in sorted(declared.items()))
    secrets = {token.secret for token in tokens}
    if len(secrets) != len(tokens):
      raise ValueError(f'{TOKENS_SECRET} declares one token under several names')
    return cls(tokens)

  def match(self, authorization: str) -> Optional[Token]:
    """The token an ``Authorization`` header presents, or None for no match."""
    for token in self.tokens:
      if hmac.compare_digest(authorization, f'Bearer {token.secret}'):
        return token
    return None

  def names(self) -> tuple[str, ...]:
    return tuple(token.name for token in self.tokens)


def _token(name: str, entry: Any) -> Token:
  if not isinstance(name, str) or len(name) == 0:
    raise ValueError(f'{TOKENS_SECRET} token names must be non-empty strings')
  if not isinstance(entry, dict) or set(entry) != {'token', 'permissions'}:
    raise ValueError(f'{TOKENS_SECRET} token {name!r} must carry only `token` and `permissions`')
  secret = entry['token']
  if not isinstance(secret, str) or len(secret) == 0:
    raise ValueError(f'{TOKENS_SECRET} token {name!r} must carry a non-empty token')
  declared = entry['permissions']
  if not isinstance(declared, list) or len(declared) == 0:
    raise ValueError(f'{TOKENS_SECRET} token {name!r} must carry at least one permission')
  permissions = set()
  for permission in declared:
    try:
      permissions.add(Permission(permission))
    except ValueError as exception:
      known = sorted(item.value for item in Permission)
      raise ValueError(
        f'{TOKENS_SECRET} token {name!r} names permission {permission!r}; known: {known}'
      ) from exception
  return Token(name=name, secret=secret, permissions=frozenset(permissions))


def requires[Handler: Callable[..., Any]](permission: Permission) -> Callable[[Handler], Handler]:
  """Declare the permission a route demands. A route that reaches the router
  without one is refused rather than served open."""

  def declare(handler: Handler) -> Handler:
    setattr(handler, _DECLARED_PERMISSION, permission)
    return handler

  return declare


def declared_permission(handler: Any) -> Optional[Permission]:
  return getattr(handler, _DECLARED_PERMISSION, None)


def presented(
  tokens: Optional[TokenTable], authorization: str
) -> tuple[Optional[str], frozenset[Permission]]:
  """The token name and permissions an ``Authorization`` header carries. An
  unauthenticated server grants every permission to everyone."""
  if tokens is None:
    return None, frozenset(Permission)
  token = tokens.match(authorization)
  if token is None:
    return None, frozenset()
  return token.name, token.permissions


def resolve_auth(
  store: credentials.Store, *, allow_no_auth: bool, host: str
) -> Optional[TokenTable]:
  """The tokens a server starts with, or None for an unauthenticated run. The
  credential is the server's alone: a client is issued one token, never the
  table naming every token there is."""
  if store.available(TOKENS_SECRET):
    return TokenTable.from_config(store.get_json(TOKENS_SECRET))
  if not allow_no_auth:
    raise RuntimeError(
      f'the {TOKENS_SECRET} credential is required; set TRAILS_ALLOW_NO_AUTH=1 to disable auth'
    )
  if host not in LOOPBACK_HOSTS:
    raise RuntimeError(f'TRAILS_ALLOW_NO_AUTH=1 requires HOST in {sorted(LOOPBACK_HOSTS)}')
  return None
