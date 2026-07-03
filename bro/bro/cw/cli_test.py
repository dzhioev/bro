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


class TestInPlace:
  def test_dispatches_to_the_runner(self):
    with (
      patch('cw.cli.run_in_place', return_value=0) as fake_run,
      patch('cw.cli.start_session') as fake_start,
    ):
      rc = cw.cli.main(['cw', 'ss', '--in-place', 'w'])
    assert rc == 0
    assert fake_run.call_count == 1
    assert fake_start.call_count == 0

  def test_rejects_machinery_flags(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--in-place', '-c', '--drop', 'w'])
    error = capsys.readouterr().err
    assert '--in-place cannot be combined with -c, --drop' in error

  def test_skips_the_auto_container_gate(self):
    # the inner argv carries --auto but never -c; the outer validated the pairing
    with patch('cw.cli.run_in_place', return_value=0) as fake_run:
      rc = cw.cli.main(['cw', 'ss', '--in-place', '--auto', 'w'])
    assert rc == 0
    spec = fake_run.call_args[0][0]
    assert spec.auto
    assert not spec.container

  def test_skips_the_bro_gates(self):
    # no -c requirement and no anthropic-key probe (deliberately unpatched here)
    with patch('cw.cli.run_in_place', return_value=0) as fake_run:
      rc = cw.cli.main(['cw', 'ss', '--in-place', '--bro', 'pm', 'w'])
    assert rc == 0
    assert fake_run.call_args[0][0].bro == 'pm'

  def test_bro_mcp_exclusivity_still_enforced(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--in-place', '--bro', 'pm', '--mcp', 'local', 'w'])
    assert 'cannot be combined with --mcp' in capsys.readouterr().err

  def test_hidden_from_help(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--help'])
    assert '--in-place' not in capsys.readouterr().out
