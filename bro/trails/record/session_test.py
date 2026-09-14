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
      branch=None,
      base_sha=None,
    )
    assert session is not None
    assert session.git_record is None

  def test_an_attached_session_carries_its_git_state(self, monkeypatch):
    _publish(monkeypatch, RIDE_ISOLATION='unboxed', RIDE_BRANCH='workspace-ws', RIDE_BASE_SHA='abc')
    session = managed_session()
    assert session is not None
    assert (session.boxed, session.branch, session.base_sha) == (False, 'workspace-ws', 'abc')

  def test_the_trail_shapes_pin_the_stored_header_and_context_contract(self, monkeypatch):
    # the header `location` (`bro/trails/model.py`) and a launch-context record
    # (`rewind`'s session context preamble) are read back from stored trails as-is
    _publish(monkeypatch, RIDE_BRANCH='workspace-ws', RIDE_BASE_SHA='abc')
    session = managed_session()
    assert session is not None
    assert session.location == {
      'workspace': 'ws',
      'host': 'laptop',
      'dir': '/home/u/ws/tree',
      'is_container': True,
    }
    assert session.git_record == {
      'kind': 'git',
      'subtype': 'state',
      'title': 'git state at launch',
      'fields': {'branch': 'workspace-ws', 'base_sha': 'abc'},
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

  @pytest.mark.parametrize('present', ['RIDE_BRANCH', 'RIDE_BASE_SHA'])
  def test_a_branch_and_its_base_come_together(self, monkeypatch, present):
    _publish(monkeypatch, **{present: 'x'})
    with pytest.raises(ValueError, match='together'):
      managed_session()
