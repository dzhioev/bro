import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import jwt
import pytest

from bro.base import credentials
from bro.extra.github import app

# throwaway RSA keypair, generated for this test file only
_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCrnypcBsOJfUEE
zdcaibJyQW5i47ULvZDjq9oGhb+LpoU8WHryLOtCkMYelFCqOffmbmN4M9kEykhd
1epPd3AYLTY4LRyLyHEn986b0dodO2XnZLJJj9Rj0nw14kyzLODrF5t4md2Dopb5
QtrhBt2FNYcW3NpEMydQEW81kVR16xgHq5hVtHjca4rNm2LWOq1ev6SQ3xSUPF+c
UhnJ0XjtlwHl9m9kKbjUOOTh6Pqai9XNjFM16LafEH7A4HcNmRtu1mGb4e2n0rAA
8liWtHJwizUgxKobF2PKYEgKjppkmY8aa4vdkeKgUEAyFyzhgIm6X3RzrM+3d9E/
z9GrJjt3AgMBAAECggEADxCgM8bQ4vXwlKmK+xng5QBTCRw1mBZnrGHj+BRFLaQ2
6DFYzX8IQbtv9gmNO9Ft2OI8xLWXFGwQm9K7ctv9piml1kWOJBkQEchCGyckedIX
zmQ7oD3KEN8jpx6PyV9O+Q6v8joIxKrWN4LZBXgdpDlVe+TJcMN6wnUH/uWcdTiX
vrvrKM1NKqwab+2gbmFYCAFcrZ+r+mPkE7UTI7Kx9vxkHVO5SfqNVxwfD9WND5iY
UE+qys3q75cH7gVh2HqHP5k1u1gJ+oHdiZuRJpp51UuXIuxq2ppjJ5lKwIf1wxjg
nNPnKZktjzUlv+ukf08lbJm2UdFv9P5sqA3RDyCCOQKBgQDhhwBIxsw7uJPbvfRY
PUVrvkyOtM781A449k+oto9DieFxPvwZYRK/wrQCKnXuINI0BaWg3hZRqaku9rBL
/4+zL5tP/YY5kUovepVK9T23ILow7HwORrO75J0SXoihUgWA79vjWnLrWIUJj7Ze
9ecrnz+Al11dEtfdowqK0fCVOQKBgQDCz5FGlOqZRUePFEcTjnVrdLQdGzb7KCUk
JvDksiNCVLRSbotip+T6X7yTr9mJVd29eo+zzEiJwI+/DUXHXdpPE6M6akQ82yuE
h/Is6faTNnorsJWDqG104vJTpoSmnonzs7V54j0uhGh+E2H+pHAAhtaEQPM3lmPg
qfGZ19eGLwKBgQDHAmEO11YLcRIQeyut3ctviwp1dzmbwugV/cxHXWlIONhWHTVK
k+1+h6pequdLzWyP+Vexf6iEQUmIpqjlN0uv29eam2YhUIL9KJerAIOIIHoMh/Hk
iyE5MUAloIPCjuVKZN5NXlhAMumaiVVtsGJgjPL1XxxE8EbKToAUBbPdiQKBgCmC
KKYtXL9Dr7egznQwSnyW5Tm+brydFSzaz0ErY6/idHmL7E8dDwD6HSgqs+M7VH/m
+W+J+3q+eOJwZYnRSY7H1GPB+MAuwtr+TG+delhrpyRf/7uJy6i4IoIIXQNTjHlM
tUI/HmIm/EzAvISRbPvvvw12+VvCw40/KKdrAhUpAoGBAKLtV+pQhD/9OGEDcazt
/eAZ2tUQdOa7cXDwPyoZn5KK1V/0reG4i4kIF+YGZgV4+jqShRHFZgxFq9u9piCa
KMKpTQFsHdsrSqDxOwqOTM27HdIkJKJPNGEvvYq7ycaU3hAfiEbeX1XP9poFDLMK
grxD7/ScU/lexizYZYP+esfZ
-----END PRIVATE KEY-----
"""

_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAq58qXAbDiX1BBM3XGomy
ckFuYuO1C72Q46vaBoW/i6aFPFh68izrQpDGHpRQqjn35m5jeDPZBMpIXdXqT3dw
GC02OC0ci8hxJ/fOm9HaHTtl52SySY/UY9J8NeJMsyzg6xebeJndg6KW+ULa4Qbd
hTWHFtzaRDMnUBFvNZFUdesYB6uYVbR43GuKzZti1jqtXr+kkN8UlDxfnFIZydF4
7ZcB5fZvZCm41Djk4ej6movVzYxTNei2nxB+wOB3DZkbbtZhm+Htp9KwAPJYlrRy
cIs1IMSqGxdjymBICo6aZJmPGmuL3ZHioFBAMhcs4YCJul90c6zPt3fRP8/RqyY7
dwIDAQAB
-----END PUBLIC KEY-----
"""


def test_mints_installation_token_via_signed_app_jwt(monkeypatch):
  post = MagicMock(return_value={'token': 'ghs_minted', 'expires_at': '2026-07-18T13:00:00Z'})
  monkeypatch.setattr(app.api, 'post', post)

  minted = app.mint_installation_token(
    app_id='12345', installation_id='67890', private_key=_PRIVATE_KEY
  )

  assert minted.token == 'ghs_minted'
  assert minted.expires_at == datetime(2026, 7, 18, 13, 0, tzinfo=UTC)
  (url, app_jwt, body) = post.call_args.args
  assert url == 'https://api.github.com/app/installations/67890/access_tokens'
  assert body == {}
  claims = jwt.decode(app_jwt, _PUBLIC_KEY, algorithms=['RS256'])
  assert claims['iss'] == '12345'
  assert claims['exp'] - claims['iat'] == 660


@pytest.fixture
def bro_dir(monkeypatch, tmp_path: Path) -> Path:
  """isolated credential search roots (as in base/credentials_test.py)"""
  configs = tmp_path / 'configs'
  bro = tmp_path / 'bro'
  configs.mkdir()
  bro.mkdir()
  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(configs))
  monkeypatch.setattr(credentials, 'BRO_DIR', str(bro))
  monkeypatch.setattr(credentials, '_default_store', None)
  return bro


class TestSource:
  def test_registry_parses_github_app_source(self):
    secret = credentials.Secret.from_dict(
      'github', {'sources': [{'type': 'github_app', 'file': 'github_app_bot.json'}]}
    )
    source = secret.sources[0]
    assert isinstance(source, app.Source)
    assert source.file == 'github_app_bot.json'

  def test_mint_normalizes_numeric_ids(self, monkeypatch):
    expires_at = datetime(2026, 7, 18, 13, tzinfo=UTC)
    mint = MagicMock(return_value=app.InstallationToken('ghs_x', expires_at))
    monkeypatch.setattr(app, 'mint_installation_token', mint)
    minted = app.Source('bot.json').mint(
      {'app_id': 1234, 'installation_id': 567, 'private_key': 'PEM'}
    )
    assert minted == credentials.Minted('ghs_x', expires_at)
    mint.assert_called_once_with(app_id='1234', installation_id='567', private_key='PEM')

  def test_mint_missing_keys_raises(self):
    with pytest.raises(ValueError, match="missing 'installation_id', 'private_key'"):
      app.Source('bot.json').mint({'app_id': 1})

  def test_mint_non_scalar_id_raises(self):
    with pytest.raises(ValueError, match="'app_id' must be a string or number"):
      app.Source('bot.json').mint({'app_id': ['x'], 'installation_id': 1, 'private_key': 'P'})

  def test_mint_non_string_private_key_raises(self):
    with pytest.raises(ValueError, match="'private_key' must be a string"):
      app.Source('bot.json').mint({'app_id': 1, 'installation_id': 2, 'private_key': 5})

  def _configured(self, bro_dir: Path, monkeypatch, expires_in: timedelta) -> MagicMock:
    (bro_dir / 'github_app_bot.json').write_text(
      json.dumps({'app_id': 1, 'installation_id': 2, 'private_key': 'PEM'})
    )
    mint = MagicMock(
      side_effect=lambda **_: app.InstallationToken(
        f'ghs_{mint.call_count}', datetime.now(UTC) + expires_in
      )
    )
    monkeypatch.setattr(app, 'mint_installation_token', mint)
    return mint

  def test_a_second_source_reads_the_held_token(self, bro_dir: Path, monkeypatch):
    mint = self._configured(bro_dir, monkeypatch, timedelta(hours=1))
    first = app.Source('github_app_bot.json').fetch()
    second = app.Source('github_app_bot.json').fetch()
    assert first == second == 'ghs_1'
    assert mint.call_count == 1

  def test_a_token_near_expiry_is_reminted(self, bro_dir: Path, monkeypatch):
    mint = self._configured(bro_dir, monkeypatch, credentials.MintingSource.EXPIRY_MARGIN / 2)
    assert app.Source('github_app_bot.json').fetch() == 'ghs_1'
    assert app.Source('github_app_bot.json').fetch() == 'ghs_2'
    assert mint.call_count == 2

  def test_a_held_token_is_reminted_once_its_lifetime_passes(self, bro_dir: Path, monkeypatch):
    mint = self._configured(bro_dir, monkeypatch, timedelta(hours=1))
    assert app.Source('github_app_bot.json').fetch() == 'ghs_1'
    held_path = bro_dir / 'github_app_bot.json.minted'
    held = json.loads(held_path.read_text())
    aged = datetime.now(UTC) - app._HELD_LIFETIME - timedelta(seconds=1)
    held_path.write_text(json.dumps({**held, 'minted_at': aged.isoformat()}))
    assert app.Source('github_app_bot.json').fetch() == 'ghs_2'
    assert mint.call_count == 2

  def test_the_held_token_is_owner_only(self, bro_dir: Path, monkeypatch):
    self._configured(bro_dir, monkeypatch, timedelta(hours=1))
    app.Source('github_app_bot.json').fetch()
    held = bro_dir / 'github_app_bot.json.minted'
    assert held.stat().st_mode & 0o777 == 0o600
    assert sorted(f.name for f in bro_dir.iterdir()) == [
      'github_app_bot.json',
      'github_app_bot.json.minted',
    ]

  def test_an_unreadable_held_token_raises(self, bro_dir: Path, monkeypatch):
    self._configured(bro_dir, monkeypatch, timedelta(hours=1))
    (bro_dir / 'github_app_bot.json.minted').write_text('{tru')
    with pytest.raises(ValueError, match='is not valid json'):
      app.Source('github_app_bot.json').fetch()

  def test_scoped_store_round_trip(self, bro_dir: Path, monkeypatch):
    # a github_app-backed variant hydrates as its minting config under the kind
    # name, and the scoped entry rehydrates back into this Source in-session
    config = {'app_id': 1, 'installation_id': 2, 'private_key': 'PEM'}
    (bro_dir / 'github_app_bot.json').write_text(json.dumps(config))
    (bro_dir / credentials.HOST_REGISTRY_FILE).write_text(
      json.dumps(
        {'github+bot': {'sources': [{'type': 'github_app', 'file': 'github_app_bot.json'}]}}
      )
    )
    mint = MagicMock(
      return_value=app.InstallationToken('ghs_x', datetime.now(UTC) + timedelta(hours=1))
    )
    monkeypatch.setattr(app, 'mint_installation_token', mint)
    store = credentials.build_scoped_store(['github+bot'])
    assert mint.call_count == 1
    assert json.loads(store['github.cred']) == config
    scoped = json.loads(store[credentials.REGISTRY_FILE])
    assert scoped['github']['sources'] == [{'type': 'github_app', 'file': 'github.cred'}]
    assert 'credentials get github' in scoped['github']['install']
    rebuilt = credentials._registry_from_dict(scoped)
    source = rebuilt['github'].sources[0]
    assert isinstance(source, app.Source)
    assert source.file == 'github.cred'
