import os
from unittest.mock import patch

import ride.ask
import ride.call


def test_ask_is_ride_solo_without_added_flags(monkeypatch):
  monkeypatch.delenv('BRO_SHELL_COMMAND', raising=False)
  with patch('ride.ask.alias_main', return_value=0) as alias:
    assert ride.ask.main(['ask', '--harness', 'bro', 'dev', 'go']) == 0
  alias.assert_called_once_with(['ask', '--harness', 'bro', 'dev', 'go'], solo=True)
  assert os.environ['BRO_SHELL_COMMAND'] == 'ask --harness bro dev go'


def test_call_is_ride_along_without_added_flags(monkeypatch):
  monkeypatch.delenv('BRO_SHELL_COMMAND', raising=False)
  with patch('ride.call.alias_main', return_value=0) as alias:
    assert ride.call.main(['call', '--harness', 'claude', 'dev']) == 0
  alias.assert_called_once_with(['call', '--harness', 'claude', 'dev'], solo=False)
  assert os.environ['BRO_SHELL_COMMAND'] == 'call --harness claude dev'
