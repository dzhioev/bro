from unittest.mock import patch

import pytest

import cw.cli


class TestSsValidation:
  def test_ss_grant_without_container_errors(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--grant', 'gmail_creds', 'wsname'])
    assert 'require -c' in capsys.readouterr().err

  def test_bro_with_resume_is_accepted(self):
    with (
      patch('cw.cli._load_anthropic_key', return_value={'api_key': 'k'}),
      patch('cw.cli.start_session', return_value=0) as fake_start,
    ):
      rc = cw.cli.main(['cw', 'ss', '-c', '--bro', 'ppp-dev', '--resume', 'w'])
    assert rc == 0
    assert fake_start.call_count == 1

  def test_ss_builds_spec_with_grant_revoke_normalized_to_lists(self):
    # the --grant/--revoke parser defaults are None; the SessionSpec the cli
    # builds must carry [] so to_command_argv can iterate them
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      cw.cli.main(['cw', 'ss', '-c', 'w'])
    spec = fake_start.call_args[0][0]
    assert spec.grant == []
    assert spec.revoke == []
