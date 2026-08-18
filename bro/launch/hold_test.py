import pytest

from bro.launch.hold import HOLD_VARIABLE, interactive_session, session_hold


def test_no_hold_outside_a_managed_session(monkeypatch: pytest.MonkeyPatch):
  monkeypatch.delenv(HOLD_VARIABLE, raising=False)
  assert session_hold() is None
  assert not interactive_session()


@pytest.mark.parametrize('hold', ['detached', 'attended', 'guided'])
def test_every_level_but_unattended_is_interactive(hold: str, monkeypatch: pytest.MonkeyPatch):
  monkeypatch.setenv(HOLD_VARIABLE, hold)
  assert session_hold() == hold
  assert interactive_session()


def test_unattended_is_not_interactive(monkeypatch: pytest.MonkeyPatch):
  monkeypatch.setenv(HOLD_VARIABLE, 'unattended')
  assert not interactive_session()
