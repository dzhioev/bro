import subprocess
import sys
from dataclasses import replace

import pytest

from bro.files.operations import validate_manual
from bro.files.protocol import InvalidInput
from bro.pdf import TYPE
from bro.pdf.attachments import TEXT_BYTES
from bro.pdf.engine import TextPage
from bro.pdf.page_text import (
  HIDDEN_EXCERPT_CHARACTERS,
  HIDDEN_NOTES,
  Character,
  assemble,
  text_and_notes,
)
from bro.pdf.pdf_test_helper import (
  attachments_view_cases,
  decoy_font,
  links_view_cases,
  make_pdf,
  text_operators,
  text_view_cases,
)
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


@pytest.mark.parametrize(
  'name,arguments,text,notes',
  text_view_cases(note_limit=HIDDEN_NOTES, excerpt_characters=HIDDEN_EXCERPT_CHARACTERS),
)
def test_text_view_fixtures(name, arguments, text, notes):
  result = provider.view(make_pdf(**arguments), 'text', None, TYPE.limits)
  assert result == {'pages': [{'page_number': 1, 'notes': notes, 'text': text}]}
  TYPE.get_view('text').content.model_validate(result, strict=True)


def test_hidden_runs_use_priority_and_include_only_internal_generated_characters():
  text, notes = text_and_notes(
    [
      Character(ord('a'), reason='white'),
      Character(13, generated=True),
      Character(10, generated=True),
      Character(ord('b'), reason='invisible'),
      Character(32, generated=True),
      Character(ord('c')),
      Character(ord('d'), reason='tiny'),
      Character(32, generated=True),
    ]
  )
  assert text == 'a\nb cd '
  assert [note.model_dump() for note in notes] == [
    {'note': 'hidden_text', 'reason': 'invisible', 'text': 'a b'},
    {'note': 'hidden_text', 'reason': 'tiny', 'text': 'd'},
  ]


def test_tag_and_physical_runs_are_ordered_and_bounded_with_unmapped_count():
  characters = [Character(ord('X'))]
  characters += [Character(0xE0000 + ord(character)) for character in 'first']
  for _ in range(HIDDEN_NOTES + 2):
    characters += [Character(ord('V'))]
    characters += [Character(ord('h'), reason='white', unmapped=True)] * (
      HIDDEN_EXCERPT_CHARACTERS + 2
    )
  text, notes = text_and_notes(characters)
  assert text.startswith('XVhh')
  values = [note.model_dump() for note in notes]
  assert values[0] == {'note': 'hidden_text', 'reason': 'tag_characters', 'text': 'first'}
  assert values[1:HIDDEN_NOTES] == [
    {'note': 'hidden_text', 'reason': 'white', 'text': 'h' * HIDDEN_EXCERPT_CHARACTERS}
  ] * (HIDDEN_NOTES - 1)
  assert values[-2:] == [
    {'note': 'hidden_text_more', 'run_count': 3},
    {
      'note': 'unmapped_characters',
      'character_count': (HIDDEN_NOTES + 2) * (HIDDEN_EXCERPT_CHARACTERS + 2),
    },
  ]


def test_text_page_selection_and_unreadable_pages():
  data = make_pdf(['first', 'second'], count=3)
  assert provider.view(data, 'text', [3, 1, 1], TYPE.limits) == {
    'pages': [
      {'page_number': 1, 'notes': [], 'text': 'first'},
      {'page_number': 3, 'notes': [{'note': 'unreadable'}], 'text': ''},
    ]
  }
  with pytest.raises(InvalidInput, match='document count'):
    provider.view(data, 'text', [4], TYPE.limits)
  with pytest.raises(InvalidInput, match='unknown PDF view'):
    provider.view(data, 'absent', None, TYPE.limits)


@pytest.mark.parametrize('limit,cap', [('max_view_characters', 10), ('max_view_entries', 1)])
def test_text_caps_are_cumulative_and_name_only_the_page(limit, cap):
  data = make_pdf(['first', 'untrusted-document-name'])
  limits = replace(TYPE.limits, **{limit: cap})
  with pytest.raises(InvalidInput, match=f'{limit} at page 2; narrow pages') as error:
    provider.view(data, 'text', None, limits)
  assert 'untrusted-document-name' not in str(error.value)
  assert provider.view(data, 'text', [1], limits)['pages'][0]['text'] == 'first'


def test_caps_include_notes_and_their_string_values():
  data = make_pdf([text_operators('x', mode=3)])
  with pytest.raises(InvalidInput, match='max_view_characters at page 1'):
    provider.view(data, 'text', None, replace(TYPE.limits, max_view_characters=2))
  with pytest.raises(InvalidInput, match='max_view_entries at page 1'):
    provider.view(data, 'text', None, replace(TYPE.limits, max_view_entries=1))
  assert (
    provider.view(
      make_pdf(['12345', '67890']),
      'text',
      None,
      replace(TYPE.limits, max_view_characters=10, max_view_entries=2),
    )['pages'][1]['text']
    == '67890'
  )


def test_text_styles_are_read_once_per_text_object(monkeypatch):
  reads = 0
  matrix_reads = 0
  original_matrix = provider.engine.character_matrix
  original = provider.engine.render_mode

  def read(handle):
    nonlocal reads
    reads += 1
    return original(handle)

  def read_matrix(*arguments):
    nonlocal matrix_reads
    matrix_reads += 1
    return original_matrix(*arguments)

  monkeypatch.setattr(provider.engine, 'character_matrix', read_matrix)
  monkeypatch.setattr(provider.engine, 'render_mode', read)
  provider.view(make_pdf(['Long enough to share a text object']), 'text', None, TYPE.limits)
  assert reads == matrix_reads == 1


@pytest.mark.parametrize('name,arguments,expected', links_view_cases())
def test_links_view_fixtures(name, arguments, expected):
  result = provider.view(make_pdf(**arguments), 'links', None, TYPE.limits)
  assert result == {'links': expected}
  TYPE.get_view('links').content.model_validate(result, strict=True)


def test_links_selection_skips_unreadable_pages():
  data = make_pdf(['first', 'second'], count=3, links=[{}, {'page': 1}])
  result = provider.view(data, 'links', [3, 2, 2], TYPE.limits)
  assert [link['page_number'] for link in result['links']] == [2]
  with pytest.raises(InvalidInput, match='document count'):
    provider.view(data, 'links', [4], TYPE.limits)


def test_link_anchor_sanitization_and_truncation():
  from bro.pdf.links import ANCHOR_CHARACTERS

  data = make_pdf(
    ['A' * (ANCHOR_CHARACTERS + 9)],
    links=[{'rectangle': [0, 0, 4000, 792]}],
    media_box=[0, 0, 4000, 792],
    character_map={65: 'A\u200b'},
  )
  assert (
    provider.view(data, 'links', None, TYPE.limits)['links'][0]['text'] == 'A' * ANCHOR_CHARACTERS
  )


@pytest.mark.parametrize('limit,cap', [('max_view_characters', 30), ('max_view_entries', 1)])
def test_link_caps_are_cumulative_and_count_targets(limit, cap):
  data = make_pdf(['anchor', 'anchor'], links=[{}, {'page': 1, 'uri': b'private:document-name'}])
  with pytest.raises(InvalidInput, match=f'{limit} at link 2 on page 2; narrow pages') as error:
    provider.view(data, 'links', None, replace(TYPE.limits, **{limit: cap}))
  assert 'private:document-name' not in str(error.value)


@pytest.mark.parametrize('name,entries,expected', attachments_view_cases(TEXT_BYTES))
def test_attachments_view_fixtures(name, entries, expected):
  result = provider.view(make_pdf(attachments=entries), 'attachments', None, TYPE.limits)
  assert result == {'attachment_count': len(entries), 'attachments': expected}
  TYPE.get_view('attachments').content.model_validate(result, strict=True)


def test_attachment_buffer_protocol_and_decoded_size(monkeypatch):
  import zlib

  from bro.pdf.pdf_test_helper import Name, Stream

  original = provider.engine.attachment_file
  calls = []

  def read(handle, buffer, size, length):
    calls.append((buffer is None, size))
    return original(handle, buffer, size, length)

  monkeypatch.setattr(provider.engine, 'attachment_file', read)
  text = b'Invoice\r\n\t42\r\n'
  stream = Stream(zlib.compress(text), {'Filter': Name('FlateDecode'), 'Params': {'Size': 999999}})
  entry = provider.view(
    make_pdf(attachments=[('invoice.xml', stream, None)]), 'attachments', None, TYPE.limits
  )['attachments'][0]
  assert calls == [(True, 0), (False, len(text))]
  assert entry['text'] == text.decode()
  assert entry['size_bytes'] == len(text)


def test_binary_attachment_is_never_queried(monkeypatch):
  def fail(*arguments):
    pytest.fail('a binary attachment must not be inflated')

  monkeypatch.setattr(provider.engine, 'attachment_file', fail)
  result = provider.view(
    make_pdf(attachments=[('invoice.xml', b'payload', 'application/octet-stream')]),
    'attachments',
    None,
    TYPE.limits,
  )
  assert result['attachments'][0]['size_bytes'] is None


def test_attachment_size_boundary_and_large_stream_only_queried(monkeypatch):
  from bro.pdf.attachments import TEXT_BYTES

  original = provider.engine.attachment_file
  calls = []

  def read(handle, buffer, size, length):
    calls.append((buffer is None, size))
    return original(handle, buffer, size, length)

  monkeypatch.setattr(provider.engine, 'attachment_file', read)
  data = make_pdf(
    attachments=[
      ('limit.txt', b'a' * TEXT_BYTES, None),
      ('above.txt', b'a' * (TEXT_BYTES + 1), None),
    ]
  )
  result = provider.view(data, 'attachments', None, TYPE.limits)['attachments']
  assert result[0]['text'] == 'a' * TEXT_BYTES
  assert result[1]['text'] is None
  assert result[1]['size_bytes'] == TEXT_BYTES + 1
  assert calls == [(True, 0), (False, TEXT_BYTES), (True, 0)]


def test_attachment_names_and_listing_are_capped_before_content():
  from bro.pdf.view_limits import ATTACHMENT_NAMES, NAME_CHARACTERS

  data = make_pdf(
    attachments=[('a' * (NAME_CHARACTERS + 3) + '.xml', b'', None)] * (ATTACHMENT_NAMES + 2)
  )
  result = provider.view(data, 'attachments', None, TYPE.limits)
  assert result['attachment_count'] == ATTACHMENT_NAMES + 2
  assert len(result['attachments']) == ATTACHMENT_NAMES
  assert all(
    entry['name'] == 'a' * NAME_CHARACTERS and entry['text'] == ''
    for entry in result['attachments']
  )


@pytest.mark.parametrize('limit,cap', [('max_view_characters', 10), ('max_view_entries', 1)])
def test_attachment_caps_are_cumulative_and_offer_no_pages(limit, cap):
  data = make_pdf(attachments=[('a.xml', b'x', None), ('secret-name.xml', b'y', None)])
  with pytest.raises(
    InvalidInput, match=f'{limit} at attachment 2; reduce attachment content'
  ) as error:
    provider.view(data, 'attachments', None, replace(TYPE.limits, **{limit: cap}))
  assert 'secret-name' not in str(error.value)
  assert 'pages' not in str(error.value)


def test_attachments_reject_pages_and_empty_tree_is_empty():
  with pytest.raises(InvalidInput, match='takes no pages'):
    provider.view(make_pdf(), 'attachments', [], TYPE.limits)
  assert provider.view(make_pdf(), 'attachments', None, TYPE.limits) == {
    'attachment_count': 0,
    'attachments': [],
  }


def test_attachment_name_fallback_kids_order_and_annotation_exclusion():
  from bro.pdf.pdf_test_helper import Name, PdfWriter, Stream, text_string

  writer = PdfWriter()
  embedded = writer.add(Stream(b'<invoice/>', {'Subtype': Name('application/xml')}))
  fallback = writer.add({'F': b'fallback.xml', 'EF': {'F': embedded}})
  unicode = writer.add(
    {'UF': text_string('factur-x.xml'), 'F': b'ignored.bin', 'EF': {'F': embedded}}
  )
  annotation = writer.add(
    {'Subtype': Name('FileAttachment'), 'Rect': [0, 0, 10, 10], 'FS': fallback}
  )
  page_tree = writer.add()
  page = writer.add(
    {
      'Type': Name('Page'),
      'Parent': page_tree,
      'MediaBox': [0, 0, 612, 792],
      'Annots': [annotation],
    }
  )
  writer.set(page_tree, {'Type': Name('Pages'), 'Count': 1, 'Kids': [page]})
  root = writer.add(
    {
      'Type': Name('Catalog'),
      'Pages': page_tree,
      'Names': {
        'EmbeddedFiles': {
          'Kids': [writer.add({'Names': [b'a', fallback, b'b', unicode], 'Limits': [b'a', b'b']})]
        }
      },
    }
  )
  result = provider.view(writer.build(root), 'attachments', None, TYPE.limits)
  assert result['attachment_count'] == 2
  assert [entry['name'] for entry in result['attachments']] == ['fallback.xml', 'factur-x.xml']
  assert all(entry['text'] == '<invoice/>' for entry in result['attachments'])
