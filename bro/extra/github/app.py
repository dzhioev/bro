"""GitHub App authentication: minting installation access tokens.

An app authenticates in two hops: a short-lived JWT signed with the app's
private key proves the app identity, then that JWT mints an installation access
token — a `ghs_…` bearer the rest of the GitHub tooling uses like any other
token, acting as the app's bot identity with the installation's permissions.
Installation tokens expire after one hour. `Source` is the credential-source
front over the mint (the `github_app` registry type).
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from bro.base import credentials
from bro.extra.github import api

# JWT claim windows: GitHub caps `exp` at 10 minutes ahead, and recommends
# backdating `iat` to absorb clock drift between the minting host and GitHub.
_JWT_BACKDATE_SECONDS = 60
_JWT_LIFETIME_SECONDS = 600


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
  # credential registry that names the `github_app` source type
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
  """credential source minting installation tokens (registry type `github_app`).
  the minting config carries `app_id`, `installation_id`, and the app's PEM
  `private_key`; ids may be strings or numbers.

  GitHub degrades an installation token once a newer one is minted for the same
  installation: reads through the older one start answering 404 on endpoints it
  still covers. Every process resolving this credential therefore has to arrive
  at the same token, so the mint is held in a file beside the config rather than
  in the process that minted it, and re-minted only near expiry.
  """

  TYPE = 'github_app'

  def fetch(self) -> Optional[str]:
    config = self.config()
    if config is None:
      return None
    held = self._held()
    if held is not None:
      return held.value
    minted = self.mint(config)
    self._hold(minted)
    return minted.value

  def _held_path(self) -> Path:
    config_path = self._config_path()
    if config_path is None:
      raise ValueError(f'github_app config {self.file!r} disappeared while minting')
    return config_path.with_name(f'{config_path.name}.minted')

  def _held(self) -> Optional[credentials.Minted]:
    path = self._held_path()
    if not path.is_file():
      return None
    try:
      held = json.loads(path.read_text())
    except json.JSONDecodeError as e:
      raise ValueError(f'held github_app token {str(path)!r} is not valid json') from e
    expires_at = datetime.fromisoformat(held['expires_at'])
    if datetime.now(UTC) >= expires_at - self.EXPIRY_MARGIN:
      return None
    return credentials.Minted(held['token'], expires_at)

  def _hold(self, minted: credentials.Minted) -> None:
    path = self._held_path()
    # published by rename so a concurrent reader sees one whole token or none;
    # two processes racing to mint cost one extra mint, not a torn file
    staged = path.with_name(f'{path.name}.{os.getpid()}')
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as file:
      json.dump({'token': minted.value, 'expires_at': minted.expires_at.isoformat()}, file)
    os.replace(staged, path)

  def mint(self, config: dict) -> credentials.Minted:
    missing = sorted({'app_id', 'installation_id', 'private_key'} - set(config))
    if len(missing) > 0:
      raise ValueError(
        f'github_app config {self.file!r} is missing {", ".join(map(repr, missing))}'
      )
    for key in ('app_id', 'installation_id'):
      if not isinstance(config[key], (str, int)):
        raise ValueError(f'github_app config {self.file!r}: {key!r} must be a string or number')
    if not isinstance(config['private_key'], str):
      raise ValueError(f"github_app config {self.file!r}: 'private_key' must be a string")
    minted = mint_installation_token(
      app_id=str(config['app_id']),
      installation_id=str(config['installation_id']),
      private_key=config['private_key'],
    )
    return credentials.Minted(minted.token, minted.expires_at)
