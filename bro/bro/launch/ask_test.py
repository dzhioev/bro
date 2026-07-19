from typing import Optional
from unittest.mock import patch

import pytest

import llm.llms.echo
from bro.bro import BaseBro
from bro.launch.ask import main
from bro.launch.identity import bro_git_identity_env
from llm.llm import LLM
from llm.mcp import MCPServer
from llm.observer import NullObserver, Observer


@pytest.fixture(autouse=True)
def _stub_scoped_store(monkeypatch):
  # the container-hop preflight hydrates the scoped store; stub the build so the
  # CLI tests never read (or mint from) the developer host's real store
  monkeypatch.setattr(
    'bro.launch.scope.credentials.build_scoped_store', lambda names, optional=(): {}
  )


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: Optional[list[MCPServer]] = None):
    super().__init__(mcp_servers)
    self.response = response
    self.send_calls: list[list[dict]] = []

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    self.send_calls.append(messages)
    return self.response


class RecordBro(BaseBro):
  name = 'record'
  description = 'records inputs'
  # no fast mode on the echo spec: ask's implied fast falls back to the plain
  # spec, so an in-place run resolves through the patchable create_bro path
  llm_spec = llm.llms.echo.LLMSpec()

  def __init__(self, response: str = 'done'):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)

  def _make_observer(self) -> Observer:
    return NullObserver()

  def _create_llm(self, *, hold: str) -> LLM:
    return self.mock_llm


def test_main_re_execs_into_container_when_outside():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('PPP_SHELL_COMMAND', None)
    rc = main(['ask', 'ppp-dev', 'hello world', '--rich'])
    assert rc == 0
    assert env['PPP_SHELL_COMMAND'] == 'ask --rich ppp-dev hello world'
    assert run.call_count == 1
    launch = run.call_args.args[0]
    assert launch.name.startswith('ask-ppp-dev-')
    assert launch.command == [
      'bro',
      'run',
      'ppp-dev',
      'hello world',
      '--rich',
      '--fast',
      '--in-place',
    ]
    assert run.call_args.kwargs['drop'] is True
    # ppp-dev's manifest (github + brog) plus the mandatory trails sink
    assert {'github', 'brog', 'trails'} <= launch.secrets
    # ppp-dev doesn't deploy → no docker socket
    assert launch.docker_sock is False


def test_main_forwards_implied_fast_into_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hello'])
    assert rc == 0
    command = run.call_args.args[0].command
    # ask implies --fast; the inner bro run defaults to the plain spec, so the
    # implied fast must ride the inner argv explicitly
    assert command == ['bro', 'run', 'ppp-dev', 'hello', '--fast', '--in-place']


def test_main_forwards_effort_into_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hello', '--effort', 'low'])
    assert rc == 0
    command = run.call_args.args[0].command
    # --effort is forwarded like the implied --fast; the in-container run applies with_effort
    assert command == ['bro', 'run', 'ppp-dev', 'hello', '--fast', '--effort', 'low', '--in-place']


def test_main_forwards_hold_into_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hello', '--hold', 'attended'])
    assert rc == 0
    command = run.call_args.args[0].command
    assert command == [
      'bro',
      'run',
      'ppp-dev',
      'hello',
      '--fast',
      '--hold',
      'attended',
      '--in-place',
    ]

  # omitted, the flag stays off the inner argv — the run's own unattended default applies
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    assert main(['ask', 'ppp-dev', 'hello']) == 0
    assert '--hold' not in run.call_args.args[0].command


def test_main_in_place_hold_reaches_the_run(monkeypatch):
  holds: list[str] = []

  class CaptureBro(RecordBro):
    async def run(
      self, input, observer=None, tracker=None, request_timeout=None, hold='unattended'
    ):
      holds.append(hold)
      return 'ok'

  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('bro.registry.get_class', return_value=CaptureBro),
    patch('bro.registry.create_bro', return_value=CaptureBro()),
  ):
    assert main(['ask', 'record', 'hi', '--in-place']) is None
    assert main(['ask', 'record', 'hi', '--hold', 'detached', '--in-place']) is None
  assert holds == ['unattended', 'detached']


def test_main_no_trails_disables_recording_in_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hello', '--no-trails'])
    assert rc == 0
    launch = run.call_args.args[0]
    # the env var carries the effect in, so --no-trails isn't forwarded into the inner argv
    assert launch.command == ['bro', 'run', 'ppp-dev', 'hello', '--fast', '--in-place']
    assert 'trails' not in launch.secrets
    assert launch.env == {
      'CW_BRO': 'ppp-dev',
      'TRAILS_DISABLED': '1',
      **bro_git_identity_env('ppp-dev'),
    }


def test_main_no_trails_with_in_place_is_an_error():
  with pytest.raises(SystemExit):
    main(['ask', 'ppp-dev', 'hello', '--no-trails', '--in-place'])


def test_main_rejects_removed_host_flag():
  with pytest.raises(SystemExit):
    main(['ask', 'ppp-dev', 'hello', '--host'])


def test_main_refuses_implicit_run_inside_container(capsys):
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1', 'BROKER_CHANNEL': 'unix:/tmp/x.sock'}),
    patch('bro.launch.root.run_in_container') as run,
    patch('bro.summon.relay_summon') as relay,
  ):
    rc = main(['ask', 'ppp-dev', 'hi'])
  assert rc == 1
  assert run.call_count == 0
  assert relay.call_count == 0
  error = capsys.readouterr().err
  assert '--summon' in error
  assert '--in-place' in error


def test_main_summon_forwards_timeout_and_into():
  with patch('bro.summon.relay_summon', return_value=0) as relay:
    rc = main(['ask', 'ppp-dev', 'hi', '--summon', '--timeout', '7200', '--into', 'feature-branch'])
  assert rc == 0
  relay.assert_called_once_with('ppp-dev', 'hi', timeout=7200.0, into='feature-branch', hold=None)


def test_main_summon_detaches(capsys):
  with patch('bro.summon.summon_detached', return_value='REQUEST-ID') as detached:
    rc = main(['ask', 'ppp-dev', 'hi', '--summon', '--detach'])
  assert rc == 0
  detached.assert_called_once_with('ppp-dev', 'hi', timeout=None, into=None, hold=None)
  assert capsys.readouterr().out == 'REQUEST-ID\n'


def test_main_summon_statically_rejects_local_flags():
  with pytest.raises(SystemExit):
    main(['ask', 'ppp-dev', 'hi', '--summon', '--rich'])


def test_main_timeout_without_summon_errors(capsys):
  with patch.dict('os.environ', {}, clear=False) as env:
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hi', '--timeout', '60'])
  assert rc == 1
  assert 'require --summon' in capsys.readouterr().err


def test_main_in_place_inside_container_runs_in_process():
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('bro.launch.root.run_in_container') as run,
    patch('bro.registry.get_class', return_value=RecordBro),
    patch('bro.registry.create_bro', return_value=RecordBro(response='ok')),
  ):
    rc = main(['ask', 'record', 'hi', '--in-place'])
    assert rc is None
    assert run.call_count == 0


def test_main_skips_container_with_in_place_flag():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container') as run,
    patch('bro.registry.get_class', return_value=RecordBro),
    patch('bro.registry.create_bro', return_value=RecordBro(response='ok')),
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'record', 'hi', '--in-place'])
    assert rc is None
    assert run.call_count == 0


def test_main_sends_unknown_slash_input_to_the_bro(capsys):
  # no client-side script validation: `/nope …` reaches the bro verbatim; the
  # model follows the script fallback contract and raises when no tool matches.
  bro = RecordBro(response='ok')
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('bro.registry.get_class', return_value=RecordBro),
    patch('bro.registry.create_bro', return_value=bro),
  ):
    rc = main(['ask', 'record', '/nope something', '--in-place'])
  assert rc is None
  assert bro.mock_llm.send_calls[0][-1]['content'] == '/nope something'
  assert capsys.readouterr().out.strip() == 'ok'
