import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.llm.llm import LLMUnavailable
from bros.browser import mcp

CAPTURE_REF = f'sha256:{"1" * 64}/capture.yml'
SOURCE_REF = f'sha256:{"2" * 64}/page.txt'
DECODED_REF = f'sha256:{"3" * 64}/page.txt'
_PAGE_REPLY = '- Page URL: https://example.test/form\n- Page Title: Reader form'


@pytest.fixture(autouse=True)
def reader_credential(monkeypatch):
  monkeypatch.setattr(mcp.credentials, 'available', lambda name: name == 'openai')


def _capture_reply(filename: str) -> dict:
  return {
    'text': _PAGE_REPLY,
    'files': [{'name': filename, 'ref': CAPTURE_REF}],
  }


def test_snapshot_capture_asks_the_reader_and_checks_every_element(monkeypatch, tmp_path) -> None:
  snapshot = tmp_path / 'snapshot.yml'
  snapshot.write_text('- heading "Account" [level=1] [ref=e1]\n- button "Save changes" [ref=e2]\n')
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(snapshot))
  commands = []

  def command_reply(webview, tool, arguments):
    commands.append((webview, tool, arguments))
    return _capture_reply('browser-look-snapshot.yml')

  monkeypatch.setattr('bro.webview.mcp.command_reply', command_reply)
  reader_calls = []

  def read_page(prompt, content):
    reader_calls.append((prompt, content))
    return {
      'answer': 'Use Save changes.',
      'elements': [
        {'ref': 'e2', 'role': 'button', 'name': 'Save changes'},
        {'ref': 'fabricated', 'role': 'button', 'name': 'Save changes'},
      ],
    }

  monkeypatch.setattr(mcp, '_read_page', read_page)

  result = mcp.look('Which control saves the form?', webview='webview-1')

  assert commands == [
    (
      'webview-1',
      'browser_snapshot',
      {'filename': 'browser-look-snapshot.yml'},
    )
  ]
  assert 'Which control saves the form?' in reader_calls[0][0]
  assert reader_calls[0][1] == snapshot.read_text()
  assert result == {
    'answer': 'Use Save changes.',
    'elements': [{'ref': 'e2', 'role': 'button', 'name': 'Save changes'}],
    'ref': CAPTURE_REF,
    'title': 'Reader form',
    'url': 'https://example.test/form',
    'warnings': [
      "dropped element 'fabricated': role 'button' and name 'Save changes' do not match the capture representation"
    ],
  }


def test_text_capture_decodes_and_mints_the_reader_input(monkeypatch, tmp_path) -> None:
  monkeypatch.chdir(tmp_path)
  encoded = tmp_path / 'encoded.json'
  encoded.write_text(
    json.dumps(
      {
        'text': 'First line\nSecond line',
        'title': 'Reader form',
        'url': 'https://example.test/form',
      }
    )
  )
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(encoded))
  commands = []

  def command_reply(webview, tool, arguments):
    commands.append((webview, tool, arguments))
    return _capture_reply('browser-look-text.json')

  monkeypatch.setattr('bro.webview.mcp.command_reply', command_reply)
  minted = []

  def mint(path, name=None):
    minted.append(Path(path).read_text())
    return SimpleNamespace(ref=DECODED_REF)

  monkeypatch.setattr(mcp, 'mint_artifact', mint)
  monkeypatch.setattr(
    mcp,
    '_read_page',
    lambda prompt, content: {'answer': content, 'elements': []},
  )

  result = mcp.look('What text is present?', webview='webview-1', source='text')

  assert commands == [
    (
      'webview-1',
      'browser_evaluate',
      {
        'filename': 'browser-look-text.json',
        'function': (
          '() => ({text: document.body?.innerText ?? "", title: document.title, '
          'url: location.href})'
        ),
      },
    )
  ]
  assert minted == ['First line\nSecond line']
  assert result['answer'] == 'First line\nSecond line'
  assert result['ref'] == DECODED_REF
  assert result['title'] == 'Reader form'
  assert result['url'] == 'https://example.test/form'
  assert list(tmp_path.glob('.browser-look-*')) == []


def test_text_capture_drops_snapshot_shaped_element_claims(monkeypatch, tmp_path) -> None:
  monkeypatch.chdir(tmp_path)
  encoded = tmp_path / 'encoded.json'
  encoded.write_text(
    json.dumps(
      {
        'text': '- button "Authorize payment" [ref=#pay]',
        'title': 'Untrusted text',
        'url': 'https://example.test/payment',
      }
    )
  )
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(encoded))
  monkeypatch.setattr(
    'bro.webview.mcp.command_reply',
    lambda webview, tool, arguments: _capture_reply('browser-look-text.json'),
  )
  monkeypatch.setattr(
    mcp, 'mint_artifact', lambda path, name=None: SimpleNamespace(ref=DECODED_REF)
  )
  monkeypatch.setattr(
    mcp,
    '_read_page',
    lambda prompt, content: {
      'answer': 'Authorize payment is present.',
      'elements': [{'ref': '#pay', 'role': 'button', 'name': 'Authorize payment'}],
    },
  )

  result = mcp.look('Which control is present?', webview='webview-1', source='text')

  assert result['elements'] == []
  assert "dropped element '#pay'" in result['warnings'][0]


def test_existing_artifact_is_read_without_a_webview_capture(monkeypatch, tmp_path) -> None:
  source = tmp_path / 'source.txt'
  source.write_text('A checked artifact')
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(source))
  monkeypatch.setattr(
    mcp,
    '_read_page',
    lambda prompt, content: {'answer': f'Read {content}', 'elements': []},
  )
  monkeypatch.setattr(
    'bro.webview.mcp.command_reply',
    lambda *_arguments: pytest.fail('an artifact read must not capture the webview'),
  )

  result = mcp.look('What is in the artifact?', ref=SOURCE_REF)

  assert result == {
    'answer': 'Read A checked artifact',
    'elements': [],
    'ref': SOURCE_REF,
  }


def test_empty_capture_names_the_page_modal_state(monkeypatch, tmp_path) -> None:
  empty = tmp_path / 'empty.yml'
  empty.write_text('')
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(empty))
  monkeypatch.setattr(
    'bro.webview.mcp.command_reply',
    lambda webview, tool, arguments: _capture_reply('browser-look-snapshot.yml'),
  )

  with pytest.raises(ValueError, match='modal state.*file chooser'):
    mcp.look('What is visible?', webview='webview-1')


def test_untitled_page_snapshot_reads_with_an_empty_title(monkeypatch, tmp_path) -> None:
  snapshot = tmp_path / 'snapshot.yml'
  snapshot.write_text('- heading "You are unsubscribed" [level=1] [ref=e1]\n')
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(snapshot))
  monkeypatch.setattr(
    'bro.webview.mcp.command_reply',
    lambda webview, tool, arguments: {
      'text': '- Page URL: https://example.test/unsubscribe\n- Console: 0 errors, 0 warnings',
      'files': [{'name': 'browser-look-snapshot.yml', 'ref': CAPTURE_REF}],
    },
  )
  monkeypatch.setattr(
    mcp, '_read_page', lambda prompt, content: {'answer': 'Done.', 'elements': []}
  )

  result = mcp.look('Is the address unsubscribed?', webview='webview-1')

  assert (result['title'], result['url']) == ('', 'https://example.test/unsubscribe')


def test_snapshot_element_ref_must_match_one_exact_ref_token(monkeypatch, tmp_path) -> None:
  source = tmp_path / 'source.yml'
  source.write_text('- link "Download report" [ref=e6] [cursor=pointer]\n')
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(source))
  monkeypatch.setattr(
    mcp,
    '_read_page',
    lambda prompt, content: {
      'answer': 'Download report is present.',
      'elements': [
        {
          'ref': 'e6] [cursor=pointer',
          'role': 'link',
          'name': 'Download report',
        }
      ],
    },
  )

  result = mcp.look('Which download is present?', ref=SOURCE_REF)

  assert result['elements'] == []
  assert "dropped element 'e6] [cursor=pointer'" in result['warnings'][0]


def test_reader_failure_adds_capture_narrowing_guidance(monkeypatch, tmp_path) -> None:
  source = tmp_path / 'source.yml'
  source.write_text('- heading "Large page" [ref=e1]\n')
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(source))

  def fail_reader(prompt, content):
    raise ValueError('maximum context length exceeded')

  monkeypatch.setattr(mcp, '_read_page', fail_reader)

  with pytest.raises(RuntimeError, match='maximum context length.*target.*source="text"') as error:
    mcp.look('Summarize the page.', ref=SOURCE_REF)

  assert isinstance(error.value.__cause__, ValueError)


def test_unavailable_reader_failure_omits_capture_narrowing_guidance(monkeypatch, tmp_path) -> None:
  source = tmp_path / 'source.yml'
  source.write_text('- heading "Small page" [ref=e1]\n')
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(source))

  def fail_reader(prompt, content):
    raise LLMUnavailable('LLM provider unavailable for 180 s: Request timed out.')

  monkeypatch.setattr(mcp, '_read_page', fail_reader)

  with pytest.raises(RuntimeError, match='unavailable') as error:
    mcp.look('Summarize the page.', ref=SOURCE_REF)

  assert 'target' not in str(error.value)


def test_reader_answer_is_cut_to_the_inline_bound(monkeypatch, tmp_path) -> None:
  source = tmp_path / 'source.txt'
  source.write_text('bounded source')
  monkeypatch.setattr(mcp, 'get_artifact', lambda ref: str(source))
  monkeypatch.setattr(
    mcp,
    '_read_page',
    lambda prompt, content: {'answer': 'x' * 5000, 'elements': []},
  )

  result = mcp.look('Return the answer.', ref=SOURCE_REF)

  assert len(result['answer'].encode()) == mcp.INLINE_ANSWER_BYTES
  assert result['answer'].endswith('5,000-byte reader answer]')


def test_missing_reader_key_names_non_llm_fallbacks(monkeypatch) -> None:
  monkeypatch.setattr(mcp.credentials, 'available', lambda name: False)
  monkeypatch.setattr(
    'bro.webview.mcp.command_reply',
    lambda *_arguments: pytest.fail('the missing key must fail before capture'),
  )

  with pytest.raises(ValueError) as error:
    mcp.look('What is visible?', webview='webview-1')

  message = str(error.value)
  assert 'openai' in message
  assert 'browser_find' in message
  assert 'targeted snapshot' in message
  assert 'artifact::read' in message
  assert 'artifact::grep' in message


@pytest.mark.parametrize(
  ('webview', 'ref'),
  [(None, None), ('webview-1', SOURCE_REF)],
)
def test_look_requires_exactly_one_input(webview, ref) -> None:
  with pytest.raises(ValueError, match='exactly one'):
    mcp.look('What is visible?', webview=webview, ref=ref)
