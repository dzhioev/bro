from unittest.mock import patch

import pytest

from bro.run import main


@pytest.fixture(autouse=True)
def _stub_scoped_store(monkeypatch):
  # the container-hop preflight hydrates the scoped store; stub the build so the
  # CLI tests never read (or mint from) the developer host's real store
  monkeypatch.setattr(
    'bro.launch.scope.credentials.build_scoped_store', lambda names, optional=(): {}
  )
  monkeypatch.setattr('bro.launch.bro_run.local_trails_mounts', lambda scoped: ())


def test_run_uses_canonical_container_launch(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as environment,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
  ):
    environment.pop('CW_IN_CONTAINER', None)
    environment.pop('BRO_SHELL_COMMAND', None)
    assert main(['bro', 'run', 'dev', 'hello']) == 0
    shell_command = environment['BRO_SHELL_COMMAND']
  launch = run.call_args.args[0]
  assert launch.name.startswith('bro-run-dev-')
  assert launch.command == ['bro', 'run', 'dev', 'hello', '--in-place']
  assert shell_command == 'bro run dev hello'
  assert capsys.readouterr().out == ''


def test_global_flag_before_run_reaches_the_launcher():
  with (
    patch.dict('os.environ', {}, clear=False) as environment,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
  ):
    environment.pop('CW_IN_CONTAINER', None)
    assert main(['bro', '--verbose', 'run', 'dev', 'hello']) == 0
  assert run.call_args.args[0].command == ['bro', 'run', 'dev', 'hello', '--in-place']


def test_run_summon_delegates_to_the_summon_library():
  with patch('bro.summon.relay_summon', return_value=0) as relay:
    assert main(['bro', 'run', '--summon', '--timeout', '60', 'reviewer', 'deploy']) == 0
  relay.assert_called_once_with(
    'reviewer',
    'deploy',
    timeout=60.0,
    into=None,
    hold=None,
    grant=None,
    revoke=None,
    llm=None,
  )


def test_run_summon_forwards_the_scope_and_spec_flags():
  with patch('bro.summon.relay_summon', return_value=0) as relay:
    argv = ['bro', 'run', '--summon', '--grant', '@auditor', '--revoke', 'openai']
    argv += ['--effort', 'high', '--fast', 'reviewer', 'deploy']
    assert main(argv) == 0
  relay.assert_called_once_with(
    'reviewer',
    'deploy',
    timeout=None,
    into=None,
    hold=None,
    grant=['@auditor'],
    revoke=['openai'],
    llm='::high+fast',
  )


def test_run_refuses_implicit_in_container_execution(capsys):
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('bro.summon.relay_summon') as relay,
  ):
    assert main(['bro', 'run', 'dev', 'hello']) == 1
  assert relay.call_count == 0
  error = capsys.readouterr().err
  assert '--summon' in error
  assert '--in-place' in error


def test_chat_uses_canonical_container_launch():
  with (
    patch.dict('os.environ', {}, clear=False) as environment,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
    patch('bro.launch.call._tty_supported', return_value=True),
  ):
    environment.pop('CW_IN_CONTAINER', None)
    environment.pop('BRO_SHELL_COMMAND', None)
    assert main(['bro', 'chat', 'dev', 'hello']) == 0
    shell_command = environment['BRO_SHELL_COMMAND']
  launch = run.call_args.args[0]
  assert launch.name.startswith('bro-chat-dev-')
  assert launch.command == ['bro', 'chat', 'dev', 'hello', '--hold', 'guided', '--in-place']
  assert shell_command == 'bro chat dev hello'


def test_chat_refuses_implicit_in_container_execution(capsys):
  with patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}):
    assert main(['bro', 'chat', 'dev', 'hello']) == 1
  assert '--in-place' in capsys.readouterr().err
