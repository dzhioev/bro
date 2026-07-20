from unittest.mock import patch

import pytest

import cw.cli


class TestSsValidation:
  def test_ss_grant_with_host_is_accepted(self):
    # host sessions hydrate the same scoped store as containers (a convenience
    # scope, not a boundary), so the grant/revoke pair applies to both modes
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', '--host', '--grant', 'gmail_creds', 'wsname'])
    assert rc == 0
    spec = fake_start.call_args[0][0]
    assert spec.host
    assert spec.grant == ['gmail_creds']

  def test_ss_non_guided_hold_with_host_is_accepted(self):
    # host sessions may skip permission prompts; the container default is the
    # sandbox, an explicit --host opts out of it
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', '--host', '--hold', 'attended', 'w'])
    assert rc == 0
    spec = fake_start.call_args[0][0]
    assert spec.host
    assert spec.hold == 'attended'

  def test_ss_hold_defaults_to_guided(self):
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', 'w'])
    assert rc == 0
    assert fake_start.call_args[0][0].hold == 'guided'

  def test_ss_rejects_an_unknown_hold(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--hold', 'automatic', 'w'])
    assert 'invalid choice' in capsys.readouterr().err

  def test_ss_raw_with_host_errors(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--host', '--raw', 'w'])
    assert '--raw cannot be combined with --host' in capsys.readouterr().err

  def test_ss_raw_requires_the_anthropic_key(self, capsys):
    with pytest.raises(SystemExit):
      with patch('cw.cli._load_anthropic_key', return_value=None):
        cw.cli.main(['cw', 'ss', '--raw', 'w'])
    assert '--raw requires the `anthropic` secret' in capsys.readouterr().err

  def test_ss_bro_with_host_is_accepted(self):
    # --bro only themes the session; host mode fences out --raw, not the bro
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', '--host', '--bro', 'pm', 'w'])
    assert rc == 0
    assert fake_start.call_args[0][0].bro == 'pm'

  def test_raw_with_resume_is_accepted(self):
    with (
      patch('cw.cli._load_anthropic_key', return_value={'api_key': 'k'}),
      patch('cw.cli.start_session', return_value=0) as fake_start,
    ):
      rc = cw.cli.main(['cw', 'ss', '--raw', '--bro', 'ppp-dev', '--resume', 'w'])
    assert rc == 0
    assert fake_start.call_count == 1

  def test_ss_builds_spec_with_grant_revoke_normalized_to_lists(self):
    # the grant/revoke parser defaults are None; the SessionSpec the cli
    # builds must carry [] so to_command_argv can iterate them
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      cw.cli.main(['cw', 'ss', 'w'])
    spec = fake_start.call_args[0][0]
    assert spec.grant == []
    assert spec.revoke == []

  def test_ss_bro_grant_does_not_require_container(self):
    # a host session has a broker root too, so a summon grant is mode-agnostic
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', '--grant', '@devoops', 'w'])
    assert rc == 0
    assert fake_start.call_args[0][0].grant == ['@devoops']


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
      cw.cli.main(['cw', 'ss', '--in-place', '--host', '--drop', 'w'])
    error = capsys.readouterr().err
    assert '--in-place cannot be combined with --host, --drop' in error

  def test_rejects_grant_revoke_flags(self, capsys):
    # the outer consumed them; the inner argv never carries them
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--in-place', '--grant', '@devoops', 'w'])
    assert '--in-place cannot be combined with --grant' in capsys.readouterr().err

  def test_hold_carried_in_the_inner_argv(self):
    # the inner argv carries --hold but never --host; the outer consumed the
    # execution mode
    with patch('cw.cli.run_in_place', return_value=0) as fake_run:
      rc = cw.cli.main(['cw', 'ss', '--in-place', '--hold', 'guided', 'w'])
    assert rc == 0
    spec = fake_run.call_args[0][0]
    assert spec.hold == 'guided'
    assert not spec.host

  def test_skips_the_raw_gates(self):
    # no anthropic-key probe (deliberately unpatched here)
    with patch('cw.cli.run_in_place', return_value=0) as fake_run:
      rc = cw.cli.main(['cw', 'ss', '--in-place', '--raw', '--bro', 'pm', 'w'])
    assert rc == 0
    assert fake_run.call_args[0][0].raw
    assert fake_run.call_args[0][0].bro == 'pm'

  def test_hidden_from_help(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--help'])
    assert '--in-place' not in capsys.readouterr().out
