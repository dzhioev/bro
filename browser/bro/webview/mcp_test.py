import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bro import mission, tool_files
from bro.artifact import ArtifactError, Minted
from bro.llm.mcp import Context
from bro.webview import mcp
from bro.webview.cli import OpenedWebview, WebviewError

REF = f'sha256:{"1" * 64}/upload.txt'
OTHER_REF = f'sha256:{"2" * 64}/other.txt'


@pytest.fixture(autouse=True)
def minted_files(monkeypatch, tmp_path):
  directory = tmp_path / 'mint'
  directory.mkdir()
  monkeypatch.setenv('RIDE_MINT_DIR', str(directory))
  files = {}

  def mint(entry, *, name):
    content = (directory / entry).read_bytes()
    ref = f'sha256:{hashlib.sha256(content).hexdigest()}/{name}'
    path = tmp_path / 'artifacts' / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    files[ref] = path
    return Minted(ref, len(content))

  monkeypatch.setattr(tool_files, 'mint_entry', mint)
  monkeypatch.setattr(tool_files, 'fetch_artifact', lambda ref, **options: str(files[ref]))
  return files


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


def test_command_returns_files_and_remembers_the_page_url(monkeypatch, tmp_path) -> None:
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

  artifact = tmp_path / 'download.txt'
  artifact.write_text('download')
  monkeypatch.setattr(tool_files, 'fetch_artifact', lambda ref, **options: str(artifact))
  rendered = mcp.command(context, 'webview-1', 'browser_click', {'target': '#done'})

  assert rendered == {
    'text': 'done\n- Page URL: https://example.test/after',
    'files': [{'ref': REF, 'path': str(artifact), 'size': 8}],
    'failed': [],
  }
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
  before = set(tmp_path.iterdir())
  rendered = mcp.command(context, 'webview-1', 'browser_snapshot')

  assert len(rendered['text'].encode()) == mcp.INLINE_REPLY_BYTES
  assert 'whole 5,000-byte reply at sha256:' in rendered['text']
  assert len(rendered['files']) == 1
  file = rendered['files'][0]
  assert file['ref'].endswith('/reply.txt')
  assert file['size'] == len(text.encode())
  assert Path(file['path']).read_text() == text
  assert set(tmp_path.iterdir()) - before == {tmp_path / 'artifacts'}
  assert list((tmp_path / 'mint').iterdir()) == []


def test_command_reads_a_spilled_reply_head_without_minting(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  spilled = tmp_path / 'spilled.txt'
  spilled.write_text('first line\n' + 'z' * 8000)
  monkeypatch.setattr(tool_files, 'fetch_artifact', lambda ref, **options: str(spilled))
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *_arguments, **_keywords: _answer(
      {'spilled': 'text', 'ref': REF, 'bytes': spilled.stat().st_size, 'files': []}
    ),
  )
  monkeypatch.setattr(
    tool_files, 'mint_entry', lambda *args, **options: pytest.fail('spill must be reused')
  )

  rendered = mcp.command(context, 'webview-1', 'browser_snapshot')

  assert rendered['text'].startswith('first line\n')
  assert len(rendered['text'].encode()) == mcp.INLINE_REPLY_BYTES
  assert rendered['text'].endswith(f'at {REF}]')
  assert rendered['files'] == [{'ref': REF, 'path': str(spilled), 'size': spilled.stat().st_size}]


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


ROSTER = [
  {
    'name': 'browser_click',
    'description': 'Click an element. The page changes afterward.',
    'inputSchema': {
      'type': 'object',
      'properties': {'target': {'type': 'string'}},
      'required': ['target'],
    },
  },
  {
    'name': 'browser_fill_form',
    'description': 'Fill multiple form fields',
    'inputSchema': {
      'type': 'object',
      'properties': {
        'fields': {
          'type': 'array',
          'items': {
            'type': 'object',
            'properties': {'target': {'type': 'string'}, 'value': {'type': 'string'}},
            'required': ['target', 'value'],
            'additionalProperties': False,
          },
        }
      },
      'required': ['fields'],
    },
  },
  {'name': 'browser_close', 'inputSchema': {'type': 'object', 'properties': {}}},
]


def _spilled_roster(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, roster: list) -> None:
  artifact = tmp_path / 'tools.json'
  artifact.write_text(json.dumps(roster))
  monkeypatch.setattr(tool_files, 'fetch_artifact', lambda _ref, **options: str(artifact))
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *_arguments, **_keywords: _answer(
      {'spilled': 'text', 'ref': REF, 'bytes': artifact.stat().st_size, 'files': []}
    ),
  )


def test_tools_lists_the_spilled_live_roster_by_first_sentence(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  _spilled_roster(monkeypatch, tmp_path, ROSTER)

  assert mcp.tools(context, 'webview-1') == (
    'browser_click — Click an element.\n'
    'browser_fill_form — Fill multiple form fields\n'
    'browser_close'
  )


def test_tools_returns_the_named_tool_entry_whole(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  _spilled_roster(monkeypatch, tmp_path, ROSTER)

  assert json.loads(mcp.tools(context, 'webview-1', 'browser_fill_form')) == ROSTER[1]


def test_tools_refuses_a_tool_outside_the_roster(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  _spilled_roster(monkeypatch, tmp_path, ROSTER)

  with pytest.raises(ValueError, match="webview webview-1 has no tool 'browser_unknown'"):
    mcp.tools(context, 'webview-1', 'browser_unknown')


def test_tools_refuses_a_roster_naming_a_tool_twice(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  _spilled_roster(monkeypatch, tmp_path, [ROSTER[0], ROSTER[0]])

  with pytest.raises(ValueError, match="names 'browser_click' twice"):
    mcp.tools(context, 'webview-1')


def test_share_returns_upload_path_and_directory_entries(monkeypatch, tmp_path) -> None:
  context = _context()
  _open(monkeypatch, context)
  directory = tmp_path / 'artifact'
  (directory / 'nested').mkdir(parents=True)
  (directory / 'resume.txt').write_text('resume')
  (directory / 'nested' / 'notes.txt').write_text('notes')
  shared: list[tuple[str, str]] = []
  monkeypatch.setattr(mcp.mission, 'share', lambda webview, ref: shared.append((webview, ref)))
  monkeypatch.setattr(
    mcp,
    'fetch_artifact',
    lambda _ref, *, checked: str(directory) if checked else pytest.fail('unchecked fetch'),
  )

  result = mcp.share(context, 'webview-1', REF)

  assert shared == [('webview-1', REF)]
  assert result == {
    'path': f'/tree/artifacts/{REF}',
    'entries': ['nested/', 'nested/notes.txt', 'resume.txt'],
  }
  assert context.state.get('webview-1').shares == [REF]


def test_share_refuses_an_unreachable_ref_before_sharing_it(monkeypatch) -> None:
  context = _context()
  _open(monkeypatch, context)
  shared: list[tuple[str, str]] = []
  monkeypatch.setattr(mcp.mission, 'share', lambda webview, ref: shared.append((webview, ref)))

  def refuse(ref: str, *, checked: bool) -> str:
    raise ArtifactError(f'artifact {ref} is not shared with this peer')

  monkeypatch.setattr(mcp, 'fetch_artifact', refuse)

  with pytest.raises(ArtifactError, match='not shared'):
    mcp.share(context, 'webview-1', REF)
  assert shared == []
  assert context.state.get('webview-1').shares == []


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
    downloads=True,
    allow=['https://allowed.test'],
    block=['https://blocked.test'],
    share=[REF],
  )
  assert first.webview == 'webview-1'
  artifact = tmp_path / 'later.txt'
  artifact.write_text('later')
  monkeypatch.setattr(mcp, 'fetch_artifact', lambda _ref, **options: str(artifact))
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
      'downloads': True,
      'allowed_origins': ['https://allowed.test'],
      'blocked_origins': ['https://blocked.test'],
      'share': [REF],
    },
    {
      'cookies': 'profile',
      'vnc': True,
      'downloads': True,
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


def test_command_separates_mint_failures_from_normalized_files(monkeypatch, tmp_path):
  context = _context()
  _open(monkeypatch, context)
  original_ref = REF.rsplit('/', 1)[0] + '/statement (1).pdf'
  normalized_ref = REF.rsplit('/', 1)[0] + '/statement_1_.pdf'
  artifact = tmp_path / 'download.pdf'
  artifact.write_bytes(b'pdf payload')
  fetched = []

  def fetch(ref, *, checked):
    assert checked
    fetched.append(ref)
    return str(artifact)

  monkeypatch.setattr(tool_files, 'fetch_artifact', fetch)
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *args, **options: _answer(
      {
        'text': 'downloaded',
        'files': [
          {'name': 'output/statement (1).pdf', 'ref': original_ref},
          {'name': 'output/trace.zip', 'error': 'named file was not written by the command'},
        ],
      }
    ),
  )

  result = mcp.command(context, 'webview-1', 'browser_snapshot')

  assert fetched == [normalized_ref]
  assert result == {
    'text': 'downloaded',
    'files': [{'ref': normalized_ref, 'path': str(artifact), 'size': len(b'pdf payload')}],
    'failed': [{'name': 'output/trace.zip', 'error': 'named file was not written by the command'}],
  }


def test_command_propagates_a_returned_file_fetch_failure(monkeypatch):
  context = _context()
  _open(monkeypatch, context)
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *args, **options: _answer(
      {'text': 'done', 'files': [{'name': 'upload.txt', 'ref': REF}]}
    ),
  )

  def refuse(ref, *, checked):
    raise ArtifactError('stored content no longer matches its digest')

  monkeypatch.setattr(tool_files, 'fetch_artifact', refuse)
  with pytest.raises(ArtifactError, match='no longer matches'):
    mcp.command(context, 'webview-1', 'browser_snapshot')


def test_command_reads_a_whole_reply_spill_through_a_checked_fetch(monkeypatch, tmp_path):
  context = _context()
  _open(monkeypatch, context)
  artifact = tmp_path / 'reply.json'
  artifact.write_text(json.dumps({'text': 'whole reply', 'files': []}))
  fetched = []

  def fetch(ref, *, checked):
    assert checked
    fetched.append(ref)
    return str(artifact)

  monkeypatch.setattr(tool_files, 'fetch_artifact', fetch)
  monkeypatch.setattr(
    mcp.mission,
    'ask',
    lambda *args, **options: _answer({'spilled': 'reply', 'ref': REF}),
  )

  assert mcp.command(context, 'webview-1', 'browser_snapshot') == {
    'text': 'whole reply',
    'files': [],
    'failed': [],
  }
  assert fetched == [REF]


@pytest.mark.asyncio
async def test_tool_metadata_exposes_the_file_contract_and_typed_command_result():
  tools = {tool.name: tool for tool in await mcp.toolset.build().list_tools()}
  assert tool_files.RETURNED_FILE_GUIDANCE in tools['command'].description
  assert 'size: int' in tools['command'].description
  assert 'failed:' in tools['command'].description
  for name, parameter in [('share', 'ref'), ('open', 'share')]:
    assert (
      tools[name].parameters['properties'][parameter]['description']
      == tool_files.FILE_PARAMETER_DESCRIPTION
    )


def test_share_fetches_a_file_with_checked_integrity_before_sharing(monkeypatch, tmp_path):
  context = _context()
  _open(monkeypatch, context)
  artifact = tmp_path / 'upload.txt'
  artifact.write_text('upload')
  events = []

  def fetch(ref, *, checked):
    assert checked
    events.append(('fetch', ref))
    return str(artifact)

  monkeypatch.setattr(mcp, 'fetch_artifact', fetch)
  monkeypatch.setattr(mcp.mission, 'share', lambda webview, ref: events.append(('share', ref)))

  assert mcp.share(context, 'webview-1', REF) == {'path': f'/tree/artifacts/{REF}'}
  assert events == [('fetch', REF), ('share', REF)]


def test_share_refuses_a_malformed_ref_without_fetching_or_sharing(monkeypatch):
  context = _context()
  _open(monkeypatch, context)
  monkeypatch.setattr(mcp, 'fetch_artifact', lambda *args, **options: pytest.fail('must not fetch'))
  monkeypatch.setattr(mcp.mission, 'share', lambda *args: pytest.fail('must not share'))
  with pytest.raises(ValueError, match='malformed artifact ref'):
    mcp.share(context, 'webview-1', '/etc/passwd')
