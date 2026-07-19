import json
from pathlib import Path

import pytest

from base import credentials
from bro.launch.identity import bro_git_identity_env

_BOT_EMAIL = '123+app[bot]@users.noreply.github.com'
_LEGACY_EMAIL = 'dzhioev+bro@gmail.com'


class _MintingSource(credentials.MintingSource):
  TYPE = 'test_mint'

  def mint(self, config: dict) -> credentials.Minted:
    raise AssertionError('identity derivation must not mint')


@pytest.fixture
def ppp_dir(monkeypatch, tmp_path: Path) -> Path:
  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path / 'configs'))
  monkeypatch.setattr(credentials, 'PPP_DIR', str(tmp_path))
  return tmp_path


def _github_store(*sources: credentials.Source) -> credentials.Store:
  return credentials.Store({'github': credentials.Secret('github', list(sources))})


def test_defaults_to_legacy_identity_without_github():
  env = bro_git_identity_env(credentials.Store({}))
  assert env == {
    'GIT_AUTHOR_NAME': 'bro',
    'GIT_AUTHOR_EMAIL': _LEGACY_EMAIL,
    'GIT_COMMITTER_NAME': 'bro',
    'GIT_COMMITTER_EMAIL': _LEGACY_EMAIL,
  }


def test_token_backed_github_keeps_the_legacy_identity(ppp_dir: Path):
  (ppp_dir / 'token').write_text('ghp_abc')
  env = bro_git_identity_env(_github_store(credentials.LocalSource('token')))
  assert env['GIT_AUTHOR_EMAIL'] == _LEGACY_EMAIL


def test_minted_github_stamps_the_bot_email(ppp_dir: Path):
  (ppp_dir / 'app.json').write_text(json.dumps({'git_email': _BOT_EMAIL}))
  env = bro_git_identity_env(_github_store(_MintingSource('app.json')))
  assert env == {
    'GIT_AUTHOR_NAME': 'bro',
    'GIT_AUTHOR_EMAIL': _BOT_EMAIL,
    'GIT_COMMITTER_NAME': 'bro',
    'GIT_COMMITTER_EMAIL': _BOT_EMAIL,
  }


def test_minted_github_without_git_email_raises(ppp_dir: Path):
  (ppp_dir / 'app.json').write_text(json.dumps({'app_id': 1}))
  with pytest.raises(ValueError, match='git_email'):
    bro_git_identity_env(_github_store(_MintingSource('app.json')))


def test_absent_minting_config_falls_back_to_the_legacy_identity(ppp_dir: Path):
  env = bro_git_identity_env(_github_store(_MintingSource('missing.json')))
  assert env['GIT_AUTHOR_EMAIL'] == _LEGACY_EMAIL
