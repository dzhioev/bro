"""GitHub App authentication: minting installation access tokens.

An app authenticates in two hops: a short-lived JWT signed with the app's
private key proves the app identity, then that JWT mints an installation access
token — a `ghs_…` bearer the rest of the GitHub tooling uses like any other
token, acting as the app's bot identity with the installation's permissions.
Installation tokens expire after one hour. `Source` is the credential-source
front over the mint (the `github_app` store-annotation type).
"""

import contextlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from bro.base import credentials, log
from bro.extra.github import api

# JWT claim windows: GitHub caps `exp` at 10 minutes ahead, and recommends
# backdating `iat` to absorb clock drift between the minting host and GitHub.
_JWT_BACKDATE_SECONDS = 60
_JWT_LIFETIME_SECONDS = 600

# how long one minted token serves before the next read re-mints it
_HELD_LIFETIME = timedelta(minutes=5)


@dataclass(frozen=True)
class InstallationToken:
  token: str
  expires_at: datetime


def mint_installation_token(
  *, app_id: str, installation_id: str, private_key: str
) -> InstallationToken:
  """mint an installation access token for one installation of the app;
  `private_key` is the app's PEM-encoded RSA key."""
  # pyjwt ships with the `github` extra, while this module loads for any
  # credential store annotation that names the `github_app` source type
  import jwt

  now = int(time.time())
  claims = {'iat': now - _JWT_BACKDATE_SECONDS, 'exp': now + _JWT_LIFETIME_SECONDS, 'iss': app_id}
  app_jwt = jwt.encode(claims, private_key, algorithm='RS256')
  response = api.post(
    f'https://api.github.com/app/installations/{installation_id}/access_tokens', app_jwt, {}
  )
  return InstallationToken(
    token=response['token'], expires_at=datetime.fromisoformat(response['expires_at'])
  )


class Source(credentials.MintingSource):
  """credential source minting installation tokens (store type `github_app`).
  the minting config carries `app_id`, `installation_id`, and the app's PEM
  `private_key`; ids may be strings or numbers.

  GitHub degrades an installation token once a newer one is minted for the same
  installation: reads through the older one start answering 404 on endpoints it
  still covers. Every process resolving this credential therefore has to arrive
  at the same token, so the mint is held in a file beside the config rather than
  in the process that minted it, and re-minted once `_HELD_LIFETIME` has passed.
  A store that cannot be written keeps its hold in the process instead, which
  bounds the minting it would otherwise do per read but shares nothing.
  """

  TYPE = 'github_app'

  def __init__(self):
    super().__init__()
    self._unpublished: Optional[dict] = None

  def fetch(self, material_path: Path) -> Optional[str]:
    config = self.config(material_path)
    if config is None:
      return None
    held = self._usable(self._published(material_path)) or self._usable(self._unpublished)
    if held is not None:
      return held.value
    minted = self.mint(config)
    hold = {
      'token': minted.value,
      'expires_at': minted.expires_at.isoformat(),
      'minted_at': datetime.now(UTC).isoformat(),
    }
    self._unpublished = hold
    self._publish(material_path, hold)
    return minted.value

  def _usable(self, hold: Optional[dict]) -> Optional[credentials.Minted]:
    if hold is None:
      return None
    expires_at = datetime.fromisoformat(hold['expires_at'])
    now = datetime.now(UTC)
    if now >= datetime.fromisoformat(hold['minted_at']) + _HELD_LIFETIME:
      return None
    if now >= expires_at - self.EXPIRY_MARGIN:
      return None
    return credentials.Minted(hold['token'], expires_at)

  def _held_path(self, material_path: Path) -> Path:
    return material_path.with_name(f'{material_path.name}.minted')

  def _published(self, material_path: Path) -> Optional[dict]:
    """the hold this store carries, or None when the next read must mint one.

    a hold this version cannot read is a miss rather than an error: the file is
    derived state whose shape travels with the code, so an older one left by a
    previous version must re-mint instead of failing every read on the host.
    """
    path = self._held_path(material_path)
    if not path.is_file():
      return None
    try:
      hold = json.loads(path.read_text())
      datetime.fromisoformat(hold['expires_at'])
      datetime.fromisoformat(hold['minted_at'])
      str(hold['token'])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
      log.warning(f'discarding an unreadable held github token: {path}')
      return None
    return hold

  def _publish(self, material_path: Path, hold: dict) -> None:
    path = self._held_path(material_path)
    # published by rename so a concurrent reader sees one whole token or none;
    # two processes racing to mint cost one extra mint, not a torn file
    staged = path.with_name(f'{path.name}.{os.getpid()}')
    try:
      descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
      with os.fdopen(descriptor, 'w') as file:
        json.dump(hold, file)
      os.replace(staged, path)
    except OSError as error:
      # a store nothing can write to — a read-only secret mount — leaves every
      # reader minting its own token, which is what they did before a hold existed
      log.warning(f'holding the github token failed: {error}')
      with contextlib.suppress(OSError):
        staged.unlink(missing_ok=True)

  def mint(self, config: dict) -> credentials.Minted:
    missing = sorted({'app_id', 'installation_id', 'private_key'} - set(config))
    if len(missing) > 0:
      raise ValueError(f'github_app config is missing {", ".join(map(repr, missing))}')
    for key in ('app_id', 'installation_id'):
      if not isinstance(config[key], (str, int)):
        raise ValueError(f'github_app config: {key!r} must be a string or number')
    if not isinstance(config['private_key'], str):
      raise ValueError("github_app config: 'private_key' must be a string")
    minted = mint_installation_token(
      app_id=str(config['app_id']),
      installation_id=str(config['installation_id']),
      private_key=config['private_key'],
    )
    return credentials.Minted(minted.token, minted.expires_at)
