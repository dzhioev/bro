from unittest.mock import patch

import pytest

import cw.cli


class TestSsValidation:
  def test_ss_grant_cred_with_host_is_accepted(self):
    # host sessions hydrate the same scoped store as containers (a convenience
    # scope, not a boundary), so the grant/revoke pair applies to both modes
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', '--host', '--grant-cred', 'gmail_creds', 'wsname'])
    assert rc == 0
    spec = fake_start.call_args[0][0]
    assert spec.host
    assert spec.grant_cred == ['gmail_creds']

  def test_ss_non_guided_mode_with_host_is_accepted(self):
    # host sessions may skip permission prompts; the container default is the
    # sandbox, an explicit --host opts out of it
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', '--host', '--mode', 'attended', 'w'])
    assert rc == 0
    spec = fake_start.call_args[0][0]
    assert spec.host
    assert spec.mode == 'attended'

  def test_ss_mode_defaults_to_attended(self):
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', 'w'])
    assert rc == 0
    assert fake_start.call_args[0][0].mode == 'attended'

  def test_ss_rejects_an_unknown_mode(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--mode', 'automatic', 'w'])
    assert 'invalid choice' in capsys.readouterr().err

  def test_ss_bro_with_host_errors(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--host', '--bro', 'pm', 'w'])
    assert '--bro cannot be combined with --host' in capsys.readouterr().err

  def test_bro_with_resume_is_accepted(self):
    with (
      patch('cw.cli._load_anthropic_key', return_value={'api_key': 'k'}),
      patch('cw.cli.start_session', return_value=0) as fake_start,
    ):
      rc = cw.cli.main(['cw', 'ss', '--bro', 'ppp-dev', '--resume', 'w'])
    assert rc == 0
    assert fake_start.call_count == 1

  def test_ss_builds_spec_with_grant_revoke_normalized_to_lists(self):
    # the grant/revoke parser defaults are None; the SessionSpec the cli
    # builds must carry [] so to_command_argv can iterate them
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      cw.cli.main(['cw', 'ss', 'w'])
    spec = fake_start.call_args[0][0]
    assert spec.grant_cred == []
    assert spec.revoke_cred == []
    assert spec.grant_summon == []
    assert spec.revoke_summon == []

  def test_ss_summon_flags_do_not_require_container(self):
    # a host session has a broker root too, so the summon pair is mode-agnostic —
    # unlike --grant-cred/--revoke-cred
    with patch('cw.cli.start_session', return_value=0) as fake_start:
      rc = cw.cli.main(['cw', 'ss', '--grant-summon', 'devoops', 'w'])
    assert rc == 0
    assert fake_start.call_args[0][0].grant_summon == ['devoops']


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

  def test_rejects_summon_flags(self, capsys):
    # the outer consumed them; the inner argv never carries them
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--in-place', '--grant-summon', 'devoops', 'w'])
    assert '--in-place cannot be combined with --grant-summon' in capsys.readouterr().err

  def test_mode_carried_in_the_inner_argv(self):
    # the inner argv carries --mode but never --host; the outer consumed the
    # execution mode
    with patch('cw.cli.run_in_place', return_value=0) as fake_run:
      rc = cw.cli.main(['cw', 'ss', '--in-place', '--mode', 'guided', 'w'])
    assert rc == 0
    spec = fake_run.call_args[0][0]
    assert spec.mode == 'guided'
    assert not spec.host

  def test_skips_the_bro_gates(self):
    # no anthropic-key probe (deliberately unpatched here)
    with patch('cw.cli.run_in_place', return_value=0) as fake_run:
      rc = cw.cli.main(['cw', 'ss', '--in-place', '--bro', 'pm', 'w'])
    assert rc == 0
    assert fake_run.call_args[0][0].bro == 'pm'

  def test_bro_persona_exclusivity_still_enforced(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--in-place', '--bro', 'pm', '--persona', 'ppp-dev', 'w'])
    assert 'cannot be combined with --persona' in capsys.readouterr().err

  def test_hidden_from_help(self, capsys):
    with pytest.raises(SystemExit):
      cw.cli.main(['cw', 'ss', '--help'])
    assert '--in-place' not in capsys.readouterr().out
