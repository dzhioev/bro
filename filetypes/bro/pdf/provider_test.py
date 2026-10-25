import subprocess
import sys
from dataclasses import replace

import pytest

from bro.files.operations import validate_manual
from bro.files.protocol import InvalidInput
from bro.pdf import TYPE
from bro.pdf.engine import TextPage
from bro.pdf.page_text import Character, assemble
from bro.pdf.pdf_test_helper import decoy_font, make_pdf, text_operators
from bro.pdf.provider import (
  ATTACHMENT_NAMES,
  NAME_CHARACTERS,
  PAGE_NUMBERS_WITHOUT_TEXT,
  TITLE_CHARACTERS,
  provider,
)


@pytest.fixture(scope='module', autouse=True)
def prepared():
  provider.prepare()


def inspect(data):
  return provider.info(data, TYPE.limits)


def page_text(data, index=0):
  with provider.engine.document(data, TYPE.limits) as document:
    assert document is not None
    with document.page(index) as page:
      assert page is not None
      return assemble(page.characters())


def test_declaration_does_not_import_the_parser():
  subprocess.run(
    [
      sys.executable,
      '-c',
      'import sys; import bro.pdf; assert not any(name.startswith("pypdfium2") for name in sys.modules); assert "bro.pdf.engine" not in sys.modules',
    ],
    check=True,
    capture_output=True,
  )


def test_manual_covers_the_details_model():
  validate_manual(TYPE, TYPE.manual.read_text())


def test_digital_empty_and_whitespace_pages():
  details = inspect(make_pdf(['Invoice INV-2024-001', '', None, ' \t', 'Amount 42']))
  assert details['page_count'] == 5
  assert details['text_layer'] == 'partial'
  assert details['page_numbers_without_text'] == [2, 3, 4]
  assert details['without_text_page_count'] == 3
  assert inspect(make_pdf([None, '']))['text_layer'] == 'none'
  assert inspect(make_pdf(['A', 'B']))['text_layer'] == 'full'


def test_capped_metadata_and_page_numbers_have_total_counts():
  data = make_pdf(
    [None] * (PAGE_NUMBERS_WITHOUT_TEXT + 3),
    title='\u200b' + 'T' * (TITLE_CHARACTERS + 9),
    attachments=[
      ('\u202e' + 'a' * (NAME_CHARACTERS + 9) + str(index), b'payload', 'text/plain')
      for index in range(ATTACHMENT_NAMES + 3)
    ],
  )
  details = inspect(data)
  assert details['title'] == 'T' * TITLE_CHARACTERS
  assert details['attachment_count'] == ATTACHMENT_NAMES + 3
  assert details['attachments'] == ['a' * NAME_CHARACTERS] * ATTACHMENT_NAMES
  assert details['without_text_page_count'] == PAGE_NUMBERS_WITHOUT_TEXT + 3
  assert details['page_numbers_without_text'] == list(range(1, PAGE_NUMBERS_WITHOUT_TEXT + 1))


@pytest.mark.parametrize('revision', [2, 3])
def test_password_and_owner_restrictions(revision):
  protected = make_pdf(user_password='secret', revision=revision)
  details = inspect(protected)
  assert details['password_required'] is True
  assert all(value is None for key, value in details.items() if key != 'password_required')
  with pytest.raises(InvalidInput, match='requires a password'):
    provider.view(protected, 'text', None, TYPE.limits)
  restricted = make_pdf(
    ['Restricted permissions still allow text'], user_password='', revision=revision
  )
  assert inspect(restricted)['password_required'] is False
  assert page_text(restricted) == 'Restricted permissions still allow text'


@pytest.mark.parametrize(
  'data,message',
  [
    (b'not a PDF', 'not a readable PDF'),
    (b'%PDF-1.7\ntrailer << /Root 99 0 R >>\n%%EOF', 'not a readable PDF'),
    (make_pdf(security_handler='Adobe.PubSec'), 'unsupported PDF security handler'),
    (make_pdf([]), 'no pages'),
    (make_pdf(count=5001), 'above the limit'),
  ],
)
def test_document_faults(data, message):
  with pytest.raises(InvalidInput, match=message):
    inspect(data)


def test_unsupported_standard_encryption_needs_a_password():
  data = make_pdf(user_password='', encryption_overrides={'V': 99, 'R': 99})
  assert inspect(data)['password_required'] is True


@pytest.mark.parametrize(
  'count,expected,missing',
  [
    (1, 1, []),
    (3, 3, [3]),
    (0, 2, []),
    (-1, 2, []),
    (1048575, 2, []),
  ],
)
def test_root_count_semantics(count, expected, missing):
  details = inspect(make_pdf(['first', 'second'], count=count))
  assert details['page_count'] == expected
  assert details['page_numbers_without_text'] == missing


def test_page_limit_uses_the_reported_count():
  with pytest.raises(InvalidInput, match='above the limit'):
    provider.info(make_pdf(count=3), replace(TYPE.limits, max_pages=2))


@pytest.mark.parametrize('offset,readable', [(1024, True), (1025, False)])
def test_header_offset(offset, readable):
  data = b'x' * offset + make_pdf()
  if readable:
    assert inspect(data)['text_layer'] == 'full'
  else:
    with pytest.raises(InvalidInput, match='not a readable PDF'):
      inspect(data)


@pytest.mark.parametrize(
  'arguments,form',
  [
    ({}, 'none'),
    ({'fields': [{'name': 'invoice', 'value': '42'}]}, 'acroform'),
    ({'xfa': True}, 'xfa'),
  ],
)
def test_form_type(arguments, form):
  assert inspect(make_pdf(**arguments))['form'] == form


def test_info_fast_path_avoids_per_character_assembly(monkeypatch):
  def fail(*arguments):
    pytest.fail('dense and empty pages must not assemble characters')

  monkeypatch.setattr(TextPage, 'characters', fail)
  assert inspect(make_pdf(['Normal text', None]))['text_layer'] == 'partial'


@pytest.mark.parametrize(
  'value,expected,has_text',
  [
    ('\U0001f600', '\U0001f600', True),
    ('\x02', '\ufffd', True),
    ('\ufffe', '\ufffd', True),
    ('\ud800', '\ufffd', True),
    ('\u200b', '', False),
    ('\u200d', '\u200d', True),
    ('\U000e0061', '', False),
  ],
)
def test_unicode_map_assembly_and_has_text_fallback(value, expected, has_text):
  data = make_pdf(['A'], character_map={65: value})
  assert page_text(data) == expected
  assert inspect(data)['text_layer'] == ('full' if has_text else 'none')


def test_supplementary_glyph_name_arrives_as_one_code_point():
  data = make_pdf(['A'], glyph_names={65: 'u1F600'}, embedded_font=decoy_font())
  assert page_text(data) == '\U0001f600'
  assert inspect(data)['text_layer'] == 'full'


def test_generated_lines_spaces_and_hyphens():
  data = make_pdf([b'BT /F1 12 Tf 72 720 Td (inter-) Tj 0 -15 Td (national) Tj ET'])
  assert page_text(data) == 'inter-national'
  assert page_text(make_pdf([text_operators('one') + text_operators('two', x=110)])) == 'one two'


def test_character_rules_and_all_controls():
  controls = [code for code in range(160) if code < 32 or code >= 127]
  assert assemble(Character(code) for code in controls) == ''.join(
    chr(code) if code in (9, 10) else '\ufffd' for code in controls
  )
  characters = [
    Character(32, generated=True),
    Character(13, generated=True),
    Character(10, generated=True),
    Character(2, hyphen=True),
    Character(0xD83D),
    Character(0xDE00),
    Character(0x1F600),
    Character(0xD800),
    Character(ord('X')),
    Character(0xDC00),
    Character(0x110000),
    Character(0xFFFE),
    Character(0x00AD),
    Character(0x200C),
    Character(0x200D),
    Character(0xA0),
    Character(0xD800),
  ]
  assert (
    assemble(characters)
    == ' \n-\U0001f600\U0001f600\ufffdX\ufffd\ufffd\ufffd\u200c\u200d\u00a0\ufffd'
  )


def test_engine_errors_are_not_document_faults(monkeypatch):
  monkeypatch.setattr(provider.engine, 'load_document', lambda *arguments: None)
  monkeypatch.setattr(provider.engine, 'last_error', lambda: 2)
  with pytest.raises(RuntimeError, match='error 2'):
    inspect(b'anything')


def test_nonembedded_helvetica_is_independent_of_a_decoy_font_directory(tmp_path):
  import json

  empty = tmp_path / 'empty'
  decoy = tmp_path / 'decoy'
  empty.mkdir()
  decoy.mkdir()
  (decoy / 'Arial.ttf').write_bytes(decoy_font())
  results = []
  for directory in (empty, decoy):
    result = subprocess.run(
      [sys.executable, '-m', 'bro.pdf.font_test_helper', str(directory), 'no'],
      check=True,
      capture_output=True,
      text=True,
    )
    results.append(json.loads(result.stdout))
  assert results[0] == results[1] == ['HelloWorld', 'Invoice INV-2024-001']
