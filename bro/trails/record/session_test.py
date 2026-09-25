from typing import Optional

import pytest

from bro.trails.record.session import ManagedSession, managed_session


def _publish(monkeypatch, **overrides: Optional[str]) -> None:
  """a launcher's session env, with `overrides` replacing (None: removing) facts."""
  facts: dict[str, Optional[str]] = {
    'RIDE_SESSION_DIR': '/var/ride/session',
    'RIDE_WORKSPACE': 'ws',
    'RIDE_HOST': 'laptop',
    'RIDE_HOST_WORKSPACE': '/home/u/ws/tree',
    'RIDE_ISOLATION': 'boxed',
    'RIDE_COMMAND': 'ride along ws',
    **overrides,
  }
  for name, value in facts.items():
    if value is None:
      monkeypatch.delenv(name, raising=False)
    else:
      monkeypatch.setenv(name, value)


class TestManagedSession:
  def test_outside_a_managed_session_there_is_none(self, monkeypatch):
    _publish(monkeypatch, RIDE_SESSION_DIR=None)
    assert managed_session() is None

  def test_a_detached_session_carries_its_location_and_launch_line(self, monkeypatch):
    _publish(monkeypatch)
    session = managed_session()
    assert session == ManagedSession(
      workspace='ws',
      host='laptop',
      host_workspace='/home/u/ws/tree',
      boxed=True,
      ride_command='ride along ws',
      repo=None,
      repo_url=None,
      branch=None,
      base_sha=None,
    )
    assert session is not None
    assert session.git is None

  def test_an_attached_session_carries_its_git_state(self, monkeypatch):
    _publish(
      monkeypatch,
      RIDE_ISOLATION='unboxed',
      RIDE_REPO='/source/bro',
      RIDE_BRANCH='workspace-ws',
      RIDE_BASE_SHA='abc',
    )
    session = managed_session()
    assert session is not None
    assert (session.boxed, session.branch, session.base_sha) == (False, 'workspace-ws', 'abc')

  def test_the_trail_shapes_pin_the_stored_header_contract(self, monkeypatch):
    _publish(
      monkeypatch,
      RIDE_REPO='https://user:token@GitHub.COM/dzhioev/bro.git?token=x#ref',
      RIDE_REPO_URL='git@GitHub.COM:dzhioev/bro.git',
      RIDE_BRANCH='workspace-ws',
      RIDE_BASE_SHA='abc',
    )
    session = managed_session()
    assert session is not None
    assert session.location == {
      'workspace': 'ws',
      'host': 'laptop',
      'dir': '/home/u/ws/tree',
      'is_container': True,
    }
    assert session.git == {
      'repo': 'https://github.com/dzhioev/bro.git',
      'url': 'git@github.com:dzhioev/bro.git',
      'branch': 'workspace-ws',
      'base_sha': 'abc',
    }

  @pytest.mark.parametrize(
    'name', ['RIDE_WORKSPACE', 'RIDE_HOST', 'RIDE_HOST_WORKSPACE', 'RIDE_ISOLATION', 'RIDE_COMMAND']
  )
  def test_a_managed_session_missing_a_fact_fails(self, monkeypatch, name):
    _publish(monkeypatch, **{name: None})
    with pytest.raises(RuntimeError, match=name):
      managed_session()

  def test_an_unknown_isolation_fails(self, monkeypatch):
    _publish(monkeypatch, RIDE_ISOLATION='somewhere')
    with pytest.raises(ValueError, match='RIDE_ISOLATION'):
      managed_session()

  @pytest.mark.parametrize('present', ['RIDE_REPO', 'RIDE_BRANCH', 'RIDE_BASE_SHA'])
  def test_an_attachment_identity_branch_and_base_come_together(self, monkeypatch, present):
    _publish(monkeypatch, **{present: 'x'})
    with pytest.raises(ValueError, match='together'):
      managed_session()

  @pytest.mark.parametrize('repo_url', ['/local/origin', 'file:///local/origin'])
  def test_a_local_origin_is_not_recorded_as_a_url(self, monkeypatch, repo_url):
    _publish(
      monkeypatch,
      RIDE_REPO='/source/bro',
      RIDE_REPO_URL=repo_url,
      RIDE_BRANCH='workspace-ws',
      RIDE_BASE_SHA='abc',
    )
    session = managed_session()
    assert session is not None
    assert session.git == {
      'repo': '/source/bro',
      'branch': 'workspace-ws',
      'base_sha': 'abc',
    }

  def test_an_origin_without_an_attachment_is_refused(self, monkeypatch):
    _publish(monkeypatch, RIDE_REPO_URL='https://example.test/repository.git')
    with pytest.raises(ValueError, match='only with RIDE_REPO'):
      managed_session()
