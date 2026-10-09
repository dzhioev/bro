import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro import mission
from bro.llm.mcp import Context
from bro.webview import mcp
from bro.webview.cli import OpenedWebview, WebviewError

REF = f'sha256:{"1" * 64}/upload.txt'
OTHER_REF = f'sha256:{"2" * 64}/other.txt'


def _context() -> Context[mcp.Webviews]:
  return Context(mcp.Webviews())


def _answer(payload: dict) -> mission.Asked:
  return mission.Asked('webview-1', 'question-1', payload)


def _open(
  monkeypatch: pytest.MonkeyPatch,
  context: Context[mcp.Webviews],
  mission_id: str = 'webview-1',
) -> None:
  monkeypatch.setattr(mcp, 'open_webview', lambda **_options: OpenedWebview(mission_id, None))
  assert mcp.open(context).webview == mission_id


def test_module_imports_without_mcp_sdk_or_ride() -> None:
  probe = subprocess.run(
    [
      sys.executable,
      '-c',
      'import sys; import bro.webview.mcp; '
      "assert 'mcp' not in sys.modules; "
      "assert not any(name == 'ride' or name.startswith('ride.') for name in sys.modules)",
    ],
    capture_output=True,
    text=True,
  )

  assert probe.returncode == 0, probe.stderr


def test_command_renders_text_files_and_remembers_the_page_url(monkeypatch) -> None:
  context = _context()
  _open(monkeypatch, context)
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *_arguments, **_keywords: _answer(
      {
        'text': 'done\n- Page URL: https://example.test/after',
        'files': [{'name': 'output/page.yml', 'ref': REF}],
      }
    ),
  )

  rendered = mcp.command(context, 'webview-1', 'browser_click', {'target': '#done'})

  assert rendered == (
    f'done\n- Page URL: https://example.test/after\n\nFiles:\n- output/page.yml: {REF}'
  )
  assert context.state.get('webview-1').last_url == 'https://example.test/after'


def test_command_mints_and_cuts_a_large_inline_reply(monkeypatch, tmp_path) -> None:
  monkeypatch.chdir(tmp_path)
  context = _context()
  _open(monkeypatch, context)
  text = 'x' * 5000
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *_arguments, **_keywords: _answer({'text': text, 'files': []}),
  )
  minted: list[bytes] = []

  def mint(path: str, name: str):
    content = Path(path).read_bytes()
    minted.append(content)
    return SimpleNamespace(ref=f'sha256:{hashlib.sha256(content).hexdigest()}/{name}')

  monkeypatch.setattr(mcp, 'mint_artifact', mint)

  rendered = mcp.command(context, 'webview-1', 'browser_snapshot')

  assert minted == [text.encode()]
  assert len(rendered.encode()) == mcp.INLINE_REPLY_BYTES
  assert 'whole 5,000-byte reply at sha256:' in rendered
  assert list(tmp_path.iterdir()) == []


def test_command_reads_a_spilled_reply_head_without_minting(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  spilled = tmp_path / 'spilled.txt'
  spilled.write_text('first line\n' + 'z' * 8000)
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(spilled))
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *_arguments, **_keywords: _answer(
      {'spilled': 'text', 'ref': REF, 'bytes': spilled.stat().st_size, 'files': []}
    ),
  )
  monkeypatch.setattr(mcp, 'mint_artifact', lambda _path, name: pytest.fail('spill must be reused'))

  rendered = mcp.command(context, 'webview-1', 'browser_snapshot')

  assert rendered.startswith('first line\n')
  assert len(rendered.encode()) == mcp.INLINE_REPLY_BYTES
  assert rendered.endswith(f'at {REF}]')


def test_command_turns_worker_error_into_tool_error(monkeypatch) -> None:
  context = _context()
  _open(monkeypatch, context)
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *_arguments, **_keywords: _answer({'error': 'page rejected the action'}),
  )

  with pytest.raises(ValueError, match='page rejected the action'):
    mcp.command(context, 'webview-1', 'browser_click')


def test_command_reports_how_an_ended_webview_ended(monkeypatch) -> None:
  context = _context()
  _open(monkeypatch, context)

  def ended(*_arguments, **_keywords):
    raise mission.MissionError("mission 'webview-1' already ended")

  monkeypatch.setattr(mcp.mission, 'ask', ended)
  monkeypatch.setattr(
    mcp.mission,
    'check',
    lambda _webview: mission.Outcome(
      'webview-1', 'webview', {'outcome': 'failed', 'error': 'browser process exited'}
    ),
  )

  with pytest.raises(ValueError, match='ended failed: browser process exited'):
    mcp.command(context, 'webview-1', 'browser_snapshot')


def test_open_propagates_launch_failure_without_retaining_the_webview(monkeypatch) -> None:
  context = _context()

  def fail(**_options):
    raise WebviewError('launch failed and was cancelled')

  monkeypatch.setattr(mcp, 'open_webview', fail)

  with pytest.raises(WebviewError, match='cancelled'):
    mcp.open(context)
  with pytest.raises(ValueError, match='was not opened'):
    context.state.get('unopened')


def test_tools_renders_the_spilled_live_roster_compactly(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  roster = [
    {
      'name': 'browser_click',
      'description': 'Click an element. The page changes afterward.',
      'inputSchema': {
        'type': 'object',
        'properties': {
          'element': {'type': 'string', 'description': 'Human-readable element description.'},
          'target': {'type': 'string', 'description': 'Exact element reference. Or a selector.'},
          'button': {'enum': ['left', 'right']},
        },
        'required': ['target'],
      },
    }
  ]
  artifact = tmp_path / 'tools.json'
  artifact.write_text(json.dumps(roster))
  monkeypatch.setattr(mcp, 'get_artifact', lambda _ref: str(artifact))
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *_arguments, **_keywords: _answer(
      {'spilled': 'text', 'ref': REF, 'bytes': artifact.stat().st_size, 'files': []}
    ),
  )

  assert mcp.tools(context, 'webview-1') == (
    'browser_click(element?: string, target: string, button?: "left"|"right") — Click an element.\n'
    '  element — Human-readable element description.\n'
    '  target — Exact element reference.'
  )


def test_share_returns_upload_path_and_directory_entries(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  directory = tmp_path / 'artifact'
  (directory / 'nested').mkdir(parents=True)
  (directory / 'resume.txt').write_text('resume')
  (directory / 'nested' / 'notes.txt').write_text('notes')
  shared: list[tuple[str, str]] = []
  monkeypatch.setattr(mcp.mission, 'share', lambda webview, ref: shared.append((webview, ref)))
  monkeypatch.setattr(mcp, 'get_artifact', lambda _ref: str(directory))

  result = mcp.share(context, 'webview-1', REF)

  assert shared == [('webview-1', REF)]
  assert result == {
    'path': f'/workspace/artifacts/{REF}',
    'entries': ['nested/', 'nested/notes.txt', 'resume.txt'],
  }
  assert context.state.get('webview-1').shares == [REF]


def test_reopen_repeats_options_shares_and_last_url(monkeypatch, tmp_path) -> None:
  context = _context()
  opened = iter(
    [OpenedWebview('webview-1', 'http://view-1'), OpenedWebview('webview-2', 'http://view-2')]
  )
  launches: list[dict] = []

  def launch(**options):
    launches.append(options)
    return next(opened)

  monkeypatch.setattr(mcp, 'open_webview', launch)
  first = mcp.open(
    context,
    cookies='profile',
    vnc=True,
    allow=['https://allowed.test'],
    block=['https://blocked.test'],
    share=[REF],
  )
  assert first.webview == 'webview-1'
  artifact = tmp_path / 'later.txt'
  artifact.write_text('later')
  monkeypatch.setattr(mcp, 'get_artifact', lambda _ref: str(artifact))
  monkeypatch.setattr(mcp.mission, 'share', lambda *_arguments: None)
  mcp.share(context, 'webview-1', OTHER_REF)
  context.state.get('webview-1').last_url = 'https://example.test/last'
  monkeypatch.setattr(
    mcp.mission,
    'check',
    lambda _webview: mission.Outcome('webview-1', 'webview', {'outcome': 'failed'}),
  )
  commands: list[tuple[str, dict]] = []

  def ask(webview, payload, **_keywords):
    commands.append((webview, payload))
    return _answer({'text': '- Page URL: https://example.test/last', 'files': []})

  monkeypatch.setattr(mcp.mission, 'ask', ask)

  replacement = mcp.reopen(context, 'webview-1')

  assert replacement == mcp.OpenResult('webview-2', 'http://view-2')
  assert launches == [
    {
      'cookies': 'profile',
      'vnc': True,
      'allowed_origins': ['https://allowed.test'],
      'blocked_origins': ['https://blocked.test'],
      'share': [REF],
    },
    {
      'cookies': 'profile',
      'vnc': True,
      'allowed_origins': ['https://allowed.test'],
      'blocked_origins': ['https://blocked.test'],
      'share': [REF, OTHER_REF],
    },
  ]
  assert commands == [
    (
      'webview-2',
      {'tool': 'browser_navigate', 'arguments': {'url': 'https://example.test/last'}},
    )
  ]


def _restoration_failure_context(monkeypatch: pytest.MonkeyPatch) -> Context[mcp.Webviews]:
  context = _context()
  opened = iter([OpenedWebview('webview-1', None), OpenedWebview('webview-2', None)])
  monkeypatch.setattr(mcp, 'open_webview', lambda **_options: next(opened))
  mcp.open(context)
  context.state.get('webview-1').last_url = 'https://unreachable.test/'
  monkeypatch.setattr(
    mcp.mission,
    'check',
    lambda _webview: mission.Outcome(
      'webview-1', 'webview', {'outcome': 'failed', 'error': 'browser exited'}
    ),
  )
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *_arguments, **_keywords: _answer({'error': 'net::ERR_NAME_NOT_RESOLVED'}),
  )
  return context


def test_reopen_cancels_and_forgets_a_replacement_when_url_restoration_fails(
  monkeypatch,
) -> None:
  context = _restoration_failure_context(monkeypatch)
  cancelled: list[str] = []
  monkeypatch.setattr(mcp.mission, 'cancel', lambda webview: cancelled.append(webview))

  with pytest.raises(ValueError, match='ERR_NAME_NOT_RESOLVED'):
    mcp.reopen(context, 'webview-1')

  assert cancelled == ['webview-2']
  with pytest.raises(ValueError, match='was not opened'):
    context.state.get('webview-2')
  assert context.state.get('webview-1').last_url == 'https://unreachable.test/'


def test_reopen_keeps_a_replacement_controllable_when_cancellation_fails(monkeypatch) -> None:
  context = _restoration_failure_context(monkeypatch)

  def fail_cancel(_webview: str) -> None:
    raise mission.MissionError('broker channel closed')

  monkeypatch.setattr(mcp.mission, 'cancel', fail_cancel)

  with pytest.raises(RuntimeError, match='replacement remains available as webview-2'):
    mcp.reopen(context, 'webview-1')

  assert context.state.get('webview-2') is context.state.get('webview-1')


def test_close_returns_the_webview_outcome(monkeypatch) -> None:
  context = _context()
  _open(monkeypatch, context)
  outcome = {'outcome': 'ok', 'value': {'commands': 3}}
  monkeypatch.setattr(mcp, 'close_webview', lambda _webview: outcome)

  assert mcp.close(context, 'webview-1') == outcome
