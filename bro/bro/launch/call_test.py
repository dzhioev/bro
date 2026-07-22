import io
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Optional
from unittest.mock import MagicMock, patch

import pytest

import llm.llms.chat_gpt
import llm.llms.echo
from bro.bros.bro import Bro
from bro.launch.call import TextRenderer, call_text, chat_main, main
from bro.launch.identity import bro_git_identity_env
from llm.llm import LLM, LLMSpec
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


class RecordBro(Bro):
  name = 'record'
  description = 'records inputs'
  # no fast mode on the echo spec: call's implied fast falls back to the plain
  # spec, so an in-place run resolves through the patchable create_bro path
  llm_spec = llm.llms.echo.LLMSpec()

  def __init__(self, response: str = 'reply'):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)

  def _make_observer(self) -> Observer:
    return NullObserver()

  def _create_llm(self, *, hold: str) -> LLM:
    return self.mock_llm


class _ScriptedLines:
  """callable that yields each scripted line in turn, then raises EOFError."""

  def __init__(self, lines: list[str]):
    self._lines = list(lines)

  def __call__(self) -> str:
    if len(self._lines) == 0:
      raise EOFError
    return self._lines.pop(0)


def _fixed_now() -> datetime:
  return datetime(2026, 5, 28, 12, 34, 56)


@pytest.mark.asyncio
async def test_text_drives_send_until_eof(capsys):
  bro = RecordBro(response='reply')
  await call_text(
    bro,
    'first',
    observer=NullObserver(),
    read_line=_ScriptedLines(['second', 'third']),
    now=_fixed_now,
  )
  assert len(bro.mock_llm.send_calls) == 3
  assert bro.mock_llm.send_calls[0][-1] == {'role': 'user', 'content': 'first'}
  assert bro.mock_llm.send_calls[1] == [{'role': 'user', 'content': 'second'}]
  assert bro.mock_llm.send_calls[2] == [{'role': 'user', 'content': 'third'}]
  out = capsys.readouterr().out
  # each reply line is `[HH:MM:SS] <bro-name>: <reply>`
  assert out.count('[12:34:56] record: reply') == 3


@pytest.mark.asyncio
async def test_text_emits_banner_before_first_reply(capsys, monkeypatch):
  monkeypatch.setattr(
    'workspace.banner.render_banner', lambda llm=False, bro=None: f'BANNER[{bro}]'
  )
  bro = RecordBro(response='reply')
  await call_text(
    bro, 'first', observer=NullObserver(), read_line=_ScriptedLines([]), now=_fixed_now
  )
  out = capsys.readouterr().out
  # banner is the opening bro message, before the first reply line; the bro name
  # is passed through so the logo renders on an in-process run, whose
  # environment doesn't carry this bro's CW_BRO
  assert out.index('BANNER[record]') < out.index('[12:34:56] record: reply')
  assert '[12:34:56] record:\nBANNER[record]' in out


@pytest.mark.asyncio
async def test_text_skips_empty_input(capsys):
  bro = RecordBro(response='reply')
  await call_text(
    bro,
    'first',
    observer=NullObserver(),
    read_line=_ScriptedLines(['', '   ', 'real']),
    now=_fixed_now,
  )
  # empty string skipped; whitespace-only is sent through (boundary belongs upstream)
  assert len(bro.mock_llm.send_calls) == 3  # first + '   ' + 'real'
  assert bro.mock_llm.send_calls[1] == [{'role': 'user', 'content': '   '}]
  assert bro.mock_llm.send_calls[2] == [{'role': 'user', 'content': 'real'}]


@pytest.mark.asyncio
async def test_text_returns_on_immediate_eof(capsys):
  bro = RecordBro(response='reply')
  await call_text(
    bro, 'only', observer=NullObserver(), read_line=_ScriptedLines([]), now=_fixed_now
  )
  assert len(bro.mock_llm.send_calls) == 1
  assert bro.mock_llm.send_calls[0][-1] == {'role': 'user', 'content': 'only'}


def _make_text_renderer() -> tuple[TextRenderer, io.StringIO]:
  out = io.StringIO()
  renderer = TextRenderer(prefix='bro', file=out, now=lambda: '12:34:56')
  return renderer, out


def test_text_renderer_renders_reasoning_one_liner():
  renderer, out = _make_text_renderer()
  renderer.on_reasoning('user wants a movie rec\nwithout horror, please')
  assert (
    out.getvalue() == '[12:34:56] bro · thinking: user wants a movie rec without horror, please\n'
  )


def test_text_renderer_renders_tool_call_with_compact_json():
  renderer, out = _make_text_renderer()
  renderer.on_tool_call('web_search', {'query': 'sci-fi movies'})
  assert out.getvalue() == '[12:34:56] bro → web_search {"query":"sci-fi movies"}\n'


def test_text_renderer_renders_tool_result_compacts_json_string():
  renderer, out = _make_text_renderer()
  renderer.on_tool_result('web_search', '[\n  "Arrival",\n  "Annihilation"\n]')
  assert out.getvalue() == '[12:34:56] bro ← web_search ["Arrival","Annihilation"]\n'


def test_text_renderer_truncates_long_interim_message():
  renderer, out = _make_text_renderer()
  renderer.on_assistant_message('x' * 500, terminal=False)
  # body capped at _MESSAGE_LIMIT (240); the line ends with the overflow marker
  assert '<260 more chars>' in out.getvalue()


def test_text_renderer_skips_terminal_message():
  renderer, out = _make_text_renderer()
  renderer.on_assistant_message('here is the answer', terminal=True)
  # call_text renders the reply itself; the renderer must not double-emit
  assert out.getvalue() == ''


@dataclass(frozen=True)
class _FastlessSpec(LLMSpec):
  """test spec that intentionally has no fast-mode equivalent."""

  TYPE: ClassVar[str] = 'fastless'

  model: str = 'whatever'

  def create_llm(self, mcp_servers=None, observer=None, tracker=None, agent=None) -> LLM:
    raise NotImplementedError('not constructible in tests')

  def dump(self) -> dict:
    return {'type': self.TYPE, 'model': self.model}

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    return cls(model=data['model'])


class _ChatBro(Bro):
  name = 'record'
  description = 'records inputs'
  llm_spec = llm.llms.chat_gpt.LLMSpec(model='gpt-5.4-mini')

  def __init__(self):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response='reply')

  def _make_observer(self) -> Observer:
    return NullObserver()

  def _create_llm(self, *, hold: str) -> LLM:
    return self.mock_llm


class _FastlessBro(Bro):
  name = 'fastless'
  description = 'has a spec without fast support'
  llm_spec = _FastlessSpec(model='whatever')

  def __init__(self):
    super().__init__(system_prompt='fastless')
    self.mock_llm = MockLLM(response='reply')

  def _make_observer(self) -> Observer:
    return NullObserver()

  def _create_llm(self, *, hold: str) -> LLM:
    return self.mock_llm


def test_default_invokes_spec_fast(monkeypatch):
  built: list[Bro] = []

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    built.append(bro)

  # exercise the in-process path: outside a container, main() would re-exec into one.
  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: _ChatBro)
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  # no flag — the call alias implies fast
  rc = main(['call', 'record', 'hi', '--in-place'])
  assert rc is None
  assert len(built) == 1
  spec = built[0].llm_spec
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  assert spec.service_tier == 'priority'
  # class default untouched — fast() returns a fresh spec
  default = _ChatBro.llm_spec
  assert isinstance(default, llm.llms.chat_gpt.LLMSpec)
  assert default.service_tier is None


def test_in_place_hold_defaults_diverge_per_alias(monkeypatch):
  holds: list[str] = []

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    holds.append(hold)

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: _ChatBro)
  monkeypatch.setattr('bro.registry.create_bro', lambda name: _ChatBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  assert main(['call', 'record', 'hi', '--in-place']) is None
  assert chat_main(['bro', 'record', 'hi', '--in-place'], program=['bro', 'chat']) is None
  assert (
    chat_main(
      ['bro', 'record', 'hi', '--hold', 'unattended', '--in-place'], program=['bro', 'chat']
    )
    is None
  )
  assert holds == ['attended', 'guided', 'unattended']


def test_bro_chat_default_builds_plain_spec(monkeypatch):
  built: list[Bro] = []

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    built.append(bro)

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.create_bro', lambda name: _ChatBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  # the canonical verb defaults to the plain class spec; fast is opt-in
  rc = chat_main(['bro', 'record', 'hi', '--in-place'], program=['bro', 'chat'])
  assert rc is None
  assert len(built) == 1
  spec = built[0].llm_spec
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  assert spec.service_tier is None


def test_bro_chat_fast_flag_invokes_spec_fast(monkeypatch):
  built: list[Bro] = []

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    built.append(bro)

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: _ChatBro)
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  rc = chat_main(['bro', 'record', 'hi', '--fast', '--in-place'], program=['bro', 'chat'])
  assert rc is None
  assert len(built) == 1
  spec = built[0].llm_spec
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  assert spec.service_tier == 'priority'


def test_default_falls_back_to_plain_when_no_fast_mode(monkeypatch):
  built: list[Bro] = []

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    built.append(bro)

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: _FastlessBro)
  monkeypatch.setattr('bro.registry.create_bro', lambda name: _FastlessBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  # fast is implicit, so a provider with no fast mode degrades to the plain spec
  # instead of erroring out.
  rc = main(['call', 'fastless', 'hi', '--in-place'])
  assert rc is None
  assert len(built) == 1
  assert isinstance(built[0], _FastlessBro)


def test_call_re_execs_into_container_when_outside():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
    patch('bro.launch.call._tty_supported', return_value=True),
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('PPP_SHELL_COMMAND', None)
    rc = main(['call', 'ppp-dev', 'hey'])
    assert rc == 0
    assert env['PPP_SHELL_COMMAND'] == 'call ppp-dev hey'
    assert run.call_count == 1
    launch = run.call_args.args[0]
    assert launch.name.startswith('call-ppp-dev-')
    # host is a tty → the TUI runs in-container, so no --text is forwarded; call
    # implies --fast, which must ride the inner argv (the inner verb defaults plain)
    assert launch.command == [
      'bro',
      'chat',
      'ppp-dev',
      'hey',
      '--fast',
      '--hold',
      'attended',
      '--in-place',
    ]
    assert run.call_args.kwargs['drop'] is True
    # ppp-dev's manifest (github + brog) plus the mandatory trails sink
    assert {'github', 'brog', 'trails'} <= launch.secrets
    # ppp-dev doesn't deploy → no docker socket
    assert launch.docker_sock is False


def test_call_forwards_text_when_host_not_a_tty():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
    patch('bro.launch.call._tty_supported', return_value=False),
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['call', 'ppp-dev', 'hey'])
    assert rc == 0
    command = run.call_args.args[0].command
    # host can't back the TUI → force text mode inside the container (the container's
    # PTY always reports a TTY, so the decision has to be made here on the host)
    assert command == [
      'bro',
      'chat',
      'ppp-dev',
      'hey',
      '--text',
      '--fast',
      '--hold',
      'attended',
      '--in-place',
    ]


def test_call_forwards_effort_into_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
    patch('bro.launch.call._tty_supported', return_value=True),
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['call', 'ppp-dev', 'hey', '--effort', 'high'])
    assert rc == 0
    command = run.call_args.args[0].command
    # --effort is forwarded like the implied --fast; the in-container run applies with_effort
    assert command == [
      'bro',
      'chat',
      'ppp-dev',
      'hey',
      '--fast',
      '--effort',
      'high',
      '--hold',
      'attended',
      '--in-place',
    ]


def test_effort_flag_overrides_spec_effort(monkeypatch):
  built: list[Bro] = []

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    built.append(bro)

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: _ChatBro)
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  rc = main(['call', 'record', 'hi', '--effort', 'max', '--in-place'])
  assert rc is None
  assert len(built) == 1
  spec = built[0].llm_spec
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  # max caps at the provider top, on top of the implicit fast()
  assert spec.reasoning_effort == 'xhigh'
  assert spec.service_tier == 'priority'


def test_effort_flag_on_effortless_provider_exits_1(monkeypatch, capsys):
  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: _FastlessBro)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  # --effort is an explicit ask — a provider without the knob errors instead of
  # falling back the way implicit fast does.
  rc = main(['call', 'fastless', 'hi', '--effort', 'high', '--in-place'])
  assert rc == 1
  assert 'does not support an effort override' in capsys.readouterr().err


def test_call_no_trails_disables_recording_in_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
    patch('bro.launch.call._tty_supported', return_value=True),
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['call', 'ppp-dev', 'hey', '--no-trails'])
    assert rc == 0
    launch = run.call_args.args[0]
    # the env var carries the effect in, so --no-trails isn't forwarded into the inner argv
    assert launch.command == [
      'bro',
      'chat',
      'ppp-dev',
      'hey',
      '--fast',
      '--hold',
      'attended',
      '--in-place',
    ]
    assert 'trails' not in launch.secrets
    assert launch.env == {
      'CW_BRO': 'ppp-dev',
      'TRAILS_DISABLED': '1',
      **bro_git_identity_env('ppp-dev'),
    }


def test_call_no_trails_with_in_place_is_an_error():
  with pytest.raises(SystemExit):
    main(['call', 'ppp-dev', 'hey', '--no-trails', '--in-place'])


def test_call_rejects_removed_host_flag():
  with pytest.raises(SystemExit):
    main(['call', 'ppp-dev', 'hey', '--host'])


def test_call_without_message_requires_resume(capsys):
  rc = main(['call', 'ppp-dev'])
  assert rc == 1
  assert 'what is required unless --resume' in capsys.readouterr().err


def test_call_resume_with_no_trails_is_an_error():
  with pytest.raises(SystemExit):
    main(['call', 'ppp-dev', '--resume', '--no-trails'])


def test_call_forwards_resume_into_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
    patch('bro.launch.call._tty_supported', return_value=True),
  ):
    env.pop('CW_IN_CONTAINER', None)
    # the bare flag resolves to the 'latest' sentinel, forwarded explicitly so
    # the in-container run resolves the trail itself
    rc = main(['call', 'ppp-dev', '--resume'])
    assert rc == 0
    command = run.call_args.args[0].command
    assert command == [
      'bro',
      'chat',
      'ppp-dev',
      '--resume',
      'latest',
      '--fast',
      '--hold',
      'attended',
      '--in-place',
    ]


def test_call_forwards_resume_trail_id_with_message():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('bro.launch.root.run_in_container', return_value=0) as run,
    patch('bro.launch.call._tty_supported', return_value=True),
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['call', 'ppp-dev', 'and then?', '--resume', 'trail-id-1'])
    assert rc == 0
    command = run.call_args.args[0].command
    assert command == [
      'bro',
      'chat',
      'ppp-dev',
      'and then?',
      '--resume',
      'trail-id-1',
      '--fast',
      '--hold',
      'attended',
      '--in-place',
    ]


def test_call_resume_runs_the_resumed_bro(monkeypatch, capsys):
  from datetime import datetime

  from bro.launch.resume import HistoryMessage, ResumedCall

  captured: dict = {}

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    captured['bro'] = bro
    captured['initial'] = initial
    captured['history'] = history

  resumed_bro = RecordBro()
  resumed_bro.trail_id = 'new-trail'
  history = [HistoryMessage(by_user=True, text='hello', when=datetime(2026, 5, 27, 9, 0, 0))]

  def fake_resume(client, bro_name, trail_ref, *, llm_spec):
    captured['trail_ref'] = trail_ref
    captured['llm_spec'] = llm_spec
    return ResumedCall(bro=resumed_bro, history=history, trail_id='old-trail')

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: _ChatBro)
  monkeypatch.setattr('bro.launch.resume.resume', fake_resume)
  monkeypatch.setattr('trails.client.default_client', lambda: MagicMock())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  rc = main(['call', 'record', '--resume', '--in-place'])
  assert rc is None
  assert captured['trail_ref'] == 'latest'
  # call implies fast, so the continuation runs the class spec's fast variant
  spec = captured['llm_spec']
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  assert spec.service_tier == 'priority'
  assert captured['bro'] is resumed_bro
  assert captured['initial'] is None
  assert captured['history'] is history
  # the exit hint points at the continuation's own trail
  err = capsys.readouterr().err
  assert 'call record --resume new-trail' in err


def test_call_prints_resume_hint_when_a_trail_was_recorded(monkeypatch, capsys):
  async def fake_call_text(bro, initial, history=None, hold='guided'):
    bro.trail_id = 'trail-xyz'

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: RecordBro)
  monkeypatch.setattr('bro.registry.create_bro', lambda name: RecordBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  rc = main(['call', 'record', 'hi', '--in-place'])
  assert rc is None
  err = capsys.readouterr().err
  assert 'conversation recorded as trail trail-xyz' in err
  assert 'call record --resume trail-xyz' in err


def test_call_skips_resume_hint_without_a_trail(monkeypatch, capsys):
  async def fake_call_text(bro, initial, history=None, hold='guided'):
    pass

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: RecordBro)
  monkeypatch.setattr('bro.registry.create_bro', lambda name: RecordBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  rc = main(['call', 'record', 'hi', '--in-place'])
  assert rc is None
  assert 'conversation recorded' not in capsys.readouterr().err


def test_call_skips_container_with_in_place_flag(monkeypatch):
  built: list[Bro] = []

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    built.append(bro)

  monkeypatch.delenv('CW_IN_CONTAINER', raising=False)
  monkeypatch.setattr('bro.registry.get_class', lambda name: RecordBro)
  monkeypatch.setattr('bro.registry.create_bro', lambda name: RecordBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)
  with patch('bro.launch.root.run_in_container') as run:
    rc = main(['call', 'record', 'hi', '--in-place'])
  assert rc is None
  assert run.call_count == 0
  assert len(built) == 1


def test_initial_slash_invocation_passes_through_verbatim(monkeypatch):
  captured: list[str] = []

  async def fake_call_text(bro, initial, history=None, hold='guided'):
    captured.append(initial)

  monkeypatch.setenv('CW_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.registry.get_class', lambda name: RecordBro)
  monkeypatch.setattr('bro.registry.create_bro', lambda name: RecordBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  # no client-side expansion: the bro's system prompt describes the /-syntax and
  # the model loads the script body through its `@::` tool.
  rc = main(['call', 'record', '/ask devoops to ping', '--in-place'])
  assert rc is None
  assert captured == ['/ask devoops to ping']


class _FakeApp:
  """captures `append_thinking` / `append_trace_line` calls; stands in for
  `ChatApp` in TUIRenderer tests so we don't have to spin up a Textual runtime."""

  def __init__(self):
    self.posted: list[str] = []
    self.thinking: list[str] = []
    self.tool_events: list[str] = []

  def call_from_thread(self, function, *args, **kwargs):
    function(*args, **kwargs)

  def append_thinking(self, text: str) -> None:
    self.thinking.append(text)

  def append_trace_line(self, text: str) -> None:
    self.posted.append(text)

  def note_tool_call(self, name: str) -> None:
    self.tool_events.append(f'call {name}')

  def note_tool_result(self) -> None:
    self.tool_events.append('result')


def test_chat_markdown_carries_bold_and_link_styles():
  from rich.console import Console

  from bro.launch.call_tui import ChatMarkdown

  console = Console(width=80)
  segments = console.render(ChatMarkdown('**bold** and [docs](https://example.com/x)'))
  styles = [segment.style for segment in segments if segment.style is not None]
  assert any(style.bold is True for style in styles)
  assert any(style.link == 'https://example.com/x' for style in styles)


def test_chat_markdown_measurement_hugs_short_text():
  from rich.console import Console
  from rich.measure import Measurement

  from bro.launch.call_tui import ChatMarkdown

  console = Console(width=80)
  measurement = Measurement.get(console, console.options, ChatMarkdown('ok'))
  assert measurement.maximum == 2


def test_message_bubble_selection_honors_offsets():
  from textual.geometry import Offset
  from textual.selection import Selection

  from bro.launch.call_tui import MessageBubble

  bubble = MessageBubble('first\nsecond', kind='user')
  extraction = bubble.get_selection(Selection(Offset(0, 1), None))
  assert extraction is not None
  assert extraction[0] == 'second'


@pytest.mark.asyncio
async def test_tui_drag_inside_markdown_bubble_selects_rendered_text(monkeypatch):
  from bro.launch.call_tui import ChatApp, MessageBubble

  monkeypatch.setattr('workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(RecordBro(), None)
  async with app.run_test(size=(100, 40)) as pilot:
    app._append_bro_message('a **bold** reply')
    await pilot.pause()
    bubble = app.query(MessageBubble).last()
    region = bubble.content_region
    # drag across the first rendered line, strictly inside the bubble — this is
    # the gesture that used to select nothing (rich renderables carry no offset
    # meta, so hit-testing found no text)
    await pilot.mouse_down(offset=(region.x + 2, region.y))
    await pilot.hover(offset=(region.x + 5, region.y))
    await pilot.mouse_up(offset=(region.x + 5, region.y))
    await pilot.pause()
    selection = app.screen.selections.get(bubble)
    assert selection is not None
    # a precise span, not a whole-widget fallback
    assert selection.start is not None
    # markdown renders '**bold**' as 'bold': the drag covers chars 2-5 of the
    # rendered 'a bold reply', release cell included
    assert app.screen.get_selected_text() == 'bold'
    assert app.clipboard == 'bold'
    # the selected span paints the selection background; the rest keeps the base
    backgrounds = {
      segment.text: segment.style.bgcolor
      for segment in bubble.render_line(0)
      if segment.style is not None and segment.style.bgcolor is not None
    }
    assert backgrounds['bold'] != backgrounds['a ']


@pytest.mark.asyncio
async def test_tui_markdown_bubble_copy_reflows_to_logical_lines(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp, MessageBubble

  monkeypatch.setattr('workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  command = (
    'dive-in --auto --grant notion -t "https://example.com/x" '
    '"a long quoted argument that certainly wraps across the bubble width"'
  )
  paragraph = 'Great — moving to the verification phase next, with the notion grant in place.'
  reply = f'{paragraph}\n\n```\n{command}\n```\n\n```python\ndef f():\n    return 1\n```'
  app = ChatApp(RecordBro(), None)
  async with app.run_test(size=(80, 40)) as pilot:
    app._append_bro_message(reply)
    await pilot.pause()
    bubble = app.query(MessageBubble).last()
    app.screen.selections = {bubble: SELECT_ALL}
    copied = app.screen.get_selected_text()
    assert copied is not None
    lines = copied.split('\n')
    # wrap points and padding never reach the copy: the paragraph and the fenced
    # command each come back as the single line they were authored as, and the
    # python block keeps its indentation
    assert lines[0] == paragraph
    assert lines[1] == ''
    assert lines[2] == command
    assert lines[3] == ''
    assert lines[4:6] == ['def f():', '    return 1']
    assert all(line == line.rstrip() for line in lines)


@pytest.mark.asyncio
async def test_tui_survives_markup_like_text(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp, MessageBubble, StatsScreen, SystemBubble

  monkeypatch.setattr('workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  # the shape that crashed the compositor: a bare `[` opens what Textual's
  # content-markup grammar reads as a tag with key=value pairs inside, and
  # rich.markup.escape does not neutralize it
  trace = "→ flow::list_tasks [x=1, statuses=['done', 'dropped'], input=3]"
  message = "please retry [a=b, statuses=['done', 'dropped']]"
  app = ChatApp(RecordBro(), None)
  async with app.run_test(size=(120, 40)) as pilot:
    app.append_trace_line(trace)
    app._append_user_message(message, when=datetime(2026, 5, 28, 12, 34, 56))
    # reflow renders every bubble — where the MarkupError used to raise
    await pilot.pause()
    system = app.query(SystemBubble).last()
    app.screen.selections = {system: SELECT_ALL}
    assert app.screen.get_selected_text() == trace
    user = app.query(MessageBubble).last()
    app.screen.selections = {user: SELECT_ALL}
    assert app.screen.get_selected_text() == message
    # the timestamp widget keeps its dim style without a markup parse
    timestamp = app.query('.timestamp').last()
    timestamp_styles = {
      segment.text.strip(): segment.style
      for segment in timestamp.render_line(0)
      if segment.style is not None
    }
    assert timestamp_styles['12:34:56'].dim is True
    # the stats card is arbitrary text too
    await app.push_screen(StatsScreen(f'card {trace}'))
    await pilot.pause()


@pytest.mark.asyncio
async def test_tui_thinking_renders_as_muted_bubble_above_typing(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import BubbleRow, ChatApp, MessageBubble, TypingIndicator

  monkeypatch.setattr('workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(RecordBro(), None)
  async with app.run_test(size=(80, 40)) as pilot:
    app._show_typing()
    app.append_thinking('**Planning**\n\nweighing the options')
    await pilot.pause()
    row = app.query(BubbleRow).last()
    assert row.has_class('thinking')
    bubble = row.query_one(MessageBubble)
    # the full summary block is in the bubble, markdown-rendered
    app.screen.selections = {bubble: SELECT_ALL}
    assert app.screen.get_selected_text() == 'Planning\n\nweighing the options'
    # lighter theme than a bro bubble: faded bar, muted text
    banner = app.query(MessageBubble).first()
    assert bubble.styles.border_left[1].a < banner.styles.border_left[1].a
    assert bubble.styles.color.a < banner.styles.color.a
    # mounted into the history stream above the typing indicator
    children = list(app.query_one('#history').children)
    assert children.index(row) < children.index(app.query_one(TypingIndicator))


@pytest.mark.asyncio
async def test_tui_timestamp_hugs_the_row_edge_with_seconds(monkeypatch):
  from bro.launch.call_tui import BubbleRow, ChatApp, MessageBubble

  monkeypatch.setattr('workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(RecordBro(), None)
  async with app.run_test(size=(80, 40)) as pilot:
    app._append_user_message('a message from the user', when=datetime(2026, 5, 28, 12, 34, 56))
    app._append_bro_message('a reply', when=datetime(2026, 5, 28, 12, 35, 7))
    await pilot.pause()
    user_row = app.query_one('BubbleRow.user', BubbleRow)
    user_stamp = user_row.query_one('.timestamp')
    segments = [segment.text for segment in user_stamp.render_line(0)]
    assert ''.join(segments).strip() == '12:34:56'
    # the user bubble hugs the right edge and its timestamp hugs it too, below
    # the bubble — clear of the sender bar, which stops with the bubble
    user_bubble = user_row.query_one(MessageBubble)
    assert user_bubble.region.right == user_row.region.right
    assert user_stamp.region.right == user_row.region.right
    bro_row = app.query(BubbleRow).last()
    bro_stamp = bro_row.query_one('.timestamp')
    bro_bubble = bro_row.query_one(MessageBubble)
    # a bro-side timestamp starts in the column the sender bar occupies
    assert bro_bubble.region.x == bro_row.region.x
    assert bro_stamp.region.x == bro_row.region.x


@pytest.mark.asyncio
async def test_tui_copies_selection_to_clipboard_on_mouse_up(monkeypatch):
  from textual.events import TextSelected
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp, MessageBubble

  monkeypatch.setattr('workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(RecordBro(), None)
  async with app.run_test() as pilot:
    # a plain click also posts TextSelected; with nothing selected the
    # clipboard must stay untouched
    app.screen.post_message(TextSelected())
    await pilot.pause()
    assert app.clipboard == ''
    banner_bubble = app.query_one(MessageBubble)
    app.screen.selections = {banner_bubble: SELECT_ALL}
    app.screen.post_message(TextSelected())
    await pilot.pause()
    # the banner bubble's copy text is the plain banner; the timestamp lives in
    # its own widget below and never rides along
    assert app.clipboard == 'BANNER'


def test_tui_renderer_posts_one_line_per_event():
  from bro.launch.call_tui import TUIRenderer

  app = _FakeApp()
  renderer = TUIRenderer(app)  # type: ignore[arg-type]
  renderer.on_reasoning('user wants\na movie rec')
  renderer.on_tool_call('web_search', {'query': 'sci-fi'})
  renderer.on_tool_result('web_search', '[\n  "Arrival"\n]')
  renderer.on_assistant_message('thinking out loud', terminal=False)
  renderer.on_assistant_message('the final answer', terminal=True)

  # reasoning becomes a thinking bubble carrying the summary block verbatim
  assert app.thinking == ['user wants\na movie rec']
  assert app.posted == [
    '→ web_search {"query":"sci-fi"}',
    '← web_search ["Arrival"]',
    '✎ says: thinking out loud',
    # terminal message is skipped — ChatApp renders the reply itself
  ]
  assert app.tool_events == ['call web_search', 'result']


def test_typing_status_text():
  from bro.launch.call_tui import _typing_status

  assert _typing_status([], 5) == 'Thinking for 5 seconds'
  assert _typing_status([], 61) == 'Thinking for a minute'
  assert _typing_status(['brog__create_task'], 0.5) == 'Calling brog::create_task()'
  assert _typing_status(['brog__create_task'], 5) == 'Calling brog::create_task for 5 seconds'
  assert _typing_status(['banner'], 0.5) == 'Calling banner()'
  assert _typing_status(['a', 'b', 'c'], 5) == 'Calling 3 tools'


@pytest.mark.asyncio
async def test_tui_typing_indicator_tracks_run_state(monkeypatch):
  from textual.widgets import Static

  from bro.launch.call_tui import ChatApp, TypingIndicator

  monkeypatch.setattr('workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(RecordBro(), None)
  async with app.run_test() as pilot:
    app._show_typing()
    await pilot.pause()
    indicator = app.query_one(TypingIndicator)
    label = indicator.query_one(Static)
    assert str(label.content).startswith('Thinking for')
    app.note_tool_call('web_search')
    assert str(label.content).startswith('Calling web_search()')
    # a call running longer than a second gains its elapsed time
    indicator._phase_since -= 5
    indicator.tick()
    assert str(label.content).startswith('Calling web_search for 5 seconds')
    app.note_tool_call('flow__list_tasks')
    assert str(label.content).startswith('Calling 2 tools')
    app.note_tool_result()
    assert str(label.content).startswith('Calling flow::list_tasks()')
    app.note_tool_result()
    # the batch's results are all in — the next LLM roundtrip starts, so the
    # thinking clock restarts from zero
    assert str(label.content).startswith('Thinking for a moment')
