from unittest.mock import patch

from bro.run import main


def test_run_uses_canonical_container_launch(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as environment,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    environment.pop('CW_IN_CONTAINER', None)
    environment.pop('PPP_SHELL_COMMAND', None)
    assert main(['bro', 'run', 'ppp-dev', 'hello']) == 0
    shell_command = environment['PPP_SHELL_COMMAND']
  launch = run.call_args.args[0]
  assert launch.name.startswith('bro-run-ppp-dev-')
  assert launch.command == ['ask', 'ppp-dev', 'hello', '--in-place']
  assert shell_command == 'bro run ppp-dev hello'
  assert capsys.readouterr().out == ''


def test_global_flag_before_run_reaches_the_launcher():
  with (
    patch.dict('os.environ', {}, clear=False) as environment,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    environment.pop('CW_IN_CONTAINER', None)
    assert main(['bro', '--verbose', 'run', 'ppp-dev', 'hello']) == 0
  assert run.call_args.args[0].command == ['ask', 'ppp-dev', 'hello', '--in-place']


def test_run_summon_delegates_to_the_summon_library():
  with patch('summon.relay_summon', return_value=0) as relay:
    assert main(['bro', 'run', '--summon', '--timeout', '60', 'devoops', 'deploy']) == 0
  relay.assert_called_once_with('devoops', 'deploy', timeout=60.0, into=None)


def test_run_refuses_implicit_in_container_execution(capsys):
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('summon.relay_summon') as relay,
  ):
    assert main(['bro', 'run', 'ppp-dev', 'hello']) == 1
  assert relay.call_count == 0
  error = capsys.readouterr().err
  assert '--summon' in error
  assert '--in-place' in error


def test_chat_uses_canonical_container_launch():
  with (
    patch.dict('os.environ', {}, clear=False) as environment,
    patch('cw.run_in_container', return_value=0) as run,
    patch('bro.launch.call._tty_supported', return_value=True),
  ):
    environment.pop('CW_IN_CONTAINER', None)
    environment.pop('PPP_SHELL_COMMAND', None)
    assert main(['bro', 'chat', 'ppp-dev', 'hello']) == 0
    shell_command = environment['PPP_SHELL_COMMAND']
  launch = run.call_args.args[0]
  assert launch.name.startswith('bro-chat-ppp-dev-')
  assert launch.command == ['call', 'ppp-dev', 'hello', '--in-place']
  assert shell_command == 'bro chat ppp-dev hello'


def test_chat_refuses_implicit_in_container_execution(capsys):
  with patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}):
    assert main(['bro', 'chat', 'ppp-dev', 'hello']) == 1
  assert '--in-place' in capsys.readouterr().err
