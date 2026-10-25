import hashlib
from contextlib import closing

import pytest

from bro.files.operations import Operations, read_reference
from bro.files.protocol import Job
from bro.files.runner import SubprocessRunner
from bro.pdf import TYPE
from bro.pdf.attachments import TEXT_BYTES
from bro.pdf.page_text import HIDDEN_EXCERPT_CHARACTERS, HIDDEN_NOTES
from bro.pdf.pdf_test_helper import (
  attachments_view_cases,
  fields_view_cases,
  links_view_cases,
  make_pdf,
  tax_form_fixture,
  text_view_cases,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'arguments,expected',
  [
    (
      {'pages': ['Invoice 42', None, '  ']},
      {'page_count': 3, 'text_layer': 'partial', 'page_numbers_without_text': [2, 3]},
    ),
    ({'pages': [None, '']}, {'text_layer': 'none'}),
    ({'user_password': 'secret'}, {'password_required': True, 'page_count': None}),
    ({'user_password': '', 'revision': 3}, {'password_required': False, 'text_layer': 'full'}),
    ({'fields': [{'name': 'invoice', 'value': '42'}]}, {'form': 'acroform'}),
    ({'xfa': True}, {'form': 'xfa'}),
    ({'count': 3}, {'page_count': 3, 'page_numbers_without_text': [2, 3]}),
    ({'pages': ['A'], 'character_map': {65: '\U0001f600'}}, {'text_layer': 'full'}),
    ({'pages': ['A'], 'character_map': {65: '\ufffe'}}, {'text_layer': 'full'}),
    ({'pages': ['A'], 'character_map': {65: '\u200b'}}, {'text_layer': 'none'}),
  ],
)
async def test_info_through_installed_type_and_real_worker(tmp_path, arguments, expected):
  data = make_pdf(**arguments)
  path = tmp_path / 'invoice.unrelated-extension'
  path.write_bytes(data)
  with closing(Operations()) as state:
    result = await state.info(str(path))
  assert result.type == 'pdf'
  assert result.sha256 == hashlib.sha256(data).hexdigest()
  assert result.size_bytes == len(data)
  assert all(result.details[key] == value for key, value in expected.items())
  assert 'password_required' in read_reference('pdf')


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'data,message',
  [
    (b'damaged bytes', 'not a readable PDF'),
    (make_pdf(security_handler='Adobe.PubSec'), 'unsupported PDF security handler'),
    (make_pdf([]), 'no pages'),
    (make_pdf(count=5001), 'above the limit'),
  ],
)
async def test_document_errors_do_not_break_following_call(tmp_path, data, message):
  path = tmp_path / 'document'
  path.write_bytes(data)
  with closing(Operations()) as state:
    with pytest.raises(ValueError, match=message):
      await state.info(str(path), type='pdf')
    path.write_bytes(make_pdf())
    assert (await state.info(str(path))).details['text_layer'] == 'full'


@pytest.mark.asyncio
async def test_native_engine_version_and_large_attachment_names_without_reading_content():
  from bro.pdf.pdf_test_helper import Name, PdfWriter, flate_stream, text_string

  writer = PdfWriter()
  attachment = writer.add(flate_stream(1500 * 1024**2))
  filespec = writer.add({'F': b'bomb.xml', 'UF': text_string('bomb.xml'), 'EF': {'F': attachment}})
  tree = writer.add({'Type': Name('Pages'), 'Count': 1, 'Kids': []})
  catalog = writer.add(
    {
      'Type': Name('Catalog'),
      'Pages': tree,
      'Names': {'EmbeddedFiles': {'Names': [b'bomb', filespec]}},
    }
  )
  with closing(SubprocessRunner()) as runner:
    result = await runner.run(Job(TYPE, 'info', {}, writer.build(catalog)))
  assert result.engine
  assert result.ok['attachments'] == ['bomb.xml']
  assert result.ok['attachment_count'] == 1


def test_nonembedded_helvetica_with_decoy_fonts_before_and_after_confinement(tmp_path):
  import json
  import subprocess
  import sys

  from bro.pdf.pdf_test_helper import decoy_font

  (tmp_path / 'Arial.ttf').write_bytes(decoy_font())
  results = []
  for confined in ('no', 'yes'):
    result = subprocess.run(
      [sys.executable, '-m', 'bro.pdf.font_test_helper', str(tmp_path), confined],
      check=True,
      capture_output=True,
      text=True,
    )
    results.append(json.loads(result.stdout))
  assert results[0] == results[1] == ['HelloWorld', 'Invoice INV-2024-001']


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'name,arguments,text,notes',
  text_view_cases(note_limit=HIDDEN_NOTES, excerpt_characters=HIDDEN_EXCERPT_CHARACTERS),
)
async def test_text_view_fixtures_through_real_worker(tmp_path, name, arguments, text, notes):
  path = tmp_path / name
  path.write_bytes(make_pdf(**arguments))
  with closing(Operations()) as state:
    result = await state.view(str(path), 'text')
  assert result.content == {'pages': [{'page_number': 1, 'notes': notes, 'text': text}]}


@pytest.mark.asyncio
async def test_text_selection_unreadable_page_and_cap_through_worker(tmp_path):
  from dataclasses import replace

  path = tmp_path / 'document'
  path.write_bytes(make_pdf(['first', 'second'], count=3))
  with closing(Operations()) as state:
    result = await state.view(str(path), 'text', pages='3,1,1')
    assert result.content == {
      'pages': [
        {'page_number': 1, 'notes': [], 'text': 'first'},
        {'page_number': 3, 'notes': [{'note': 'unreadable'}], 'text': ''},
      ]
    }
    with pytest.raises(ValueError, match='document count'):
      await state.view(str(path), 'text', pages='4')
  file_type = replace(TYPE, limits=replace(TYPE.limits, max_view_characters=6))
  with closing(SubprocessRunner()) as runner:
    with pytest.raises(ValueError, match='page 2; narrow pages'):
      await runner.run(Job(file_type, 'view', {'view': 'text', 'pages': None}, path.read_bytes()))
    result = await runner.run(
      Job(file_type, 'view', {'view': 'text', 'pages': [2]}, path.read_bytes())
    )
  assert result.ok['pages'][0]['text'] == 'second'


@pytest.mark.asyncio
@pytest.mark.parametrize('name,arguments,expected', links_view_cases())
async def test_links_view_fixtures_through_real_worker(tmp_path, name, arguments, expected):
  path = tmp_path / name
  path.write_bytes(make_pdf(**arguments))
  with closing(Operations()) as state:
    result = await state.view(str(path), 'links')
  assert result.content == {'links': expected}


@pytest.mark.asyncio
@pytest.mark.parametrize('name,entries,expected', attachments_view_cases(TEXT_BYTES))
async def test_attachments_view_fixtures_through_real_worker(tmp_path, name, entries, expected):
  path = tmp_path / name
  path.write_bytes(make_pdf(attachments=entries))
  with closing(Operations()) as state:
    result = await state.view(str(path), 'attachments')
    with pytest.raises(ValueError, match='takes no pages'):
      await state.view(str(path), 'attachments', pages='1')
  assert result.content == {'attachment_count': len(entries), 'attachments': expected}


@pytest.mark.asyncio
async def test_link_selection_and_attachment_caps_through_worker(tmp_path):
  from dataclasses import replace

  path = tmp_path / 'document'
  path.write_bytes(make_pdf(['first', 'second'], count=3, links=[{}, {'page': 1}]))
  with closing(Operations()) as state:
    result = await state.view(str(path), 'links', pages='3,2,2')
    assert [entry['page_number'] for entry in result.content['links']] == [2]
  file_type = replace(TYPE, limits=replace(TYPE.limits, max_view_entries=1))
  with closing(SubprocessRunner()) as runner:
    with pytest.raises(ValueError, match='link 2 on page 2; narrow pages'):
      await runner.run(Job(file_type, 'view', {'view': 'links', 'pages': None}, path.read_bytes()))
    data = make_pdf(attachments=[('first.xml', b'a', None), ('second.xml', b'b', None)])
    with pytest.raises(ValueError, match='attachment 2; reduce attachment content'):
      await runner.run(Job(file_type, 'view', {'view': 'attachments', 'pages': None}, data))


@pytest.mark.asyncio
async def test_attachment_bomb_fails_the_whole_view_and_following_call_succeeds(tmp_path):
  from bro.pdf.pdf_test_helper import flate_stream

  bomb = flate_stream(1500 * 1024**2)
  path = tmp_path / 'invoice'
  path.write_bytes(
    make_pdf(
      attachments=[
        ('factur-x.xml', b'<invoice/>', 'application/xml'),
        ('bomb.xml', bomb, 'application/xml'),
      ]
    )
  )
  with closing(Operations()) as state:
    assert (await state.info(str(path))).details['attachment_count'] == 2
    with pytest.raises(RuntimeError, match='SIGABRT'):
      await state.view(str(path), 'attachments')
    path.write_bytes(
      make_pdf(
        attachments=[
          ('factur-x.xml', b'<invoice/>', 'application/xml'),
          ('bomb.xml', bomb, 'application/octet-stream'),
        ]
      )
    )
    result = await state.view(str(path), 'attachments')
    assert result.content['attachments'][0]['text'] == '<invoice/>'
    assert result.content['attachments'][1]['size_bytes'] is None
    path.write_bytes(make_pdf(['still healthy']))
    assert (await state.view(str(path), 'text')).content['pages'][0]['text'] == 'still healthy'


@pytest.mark.asyncio
@pytest.mark.parametrize('name,arguments,expected', fields_view_cases())
async def test_fields_view_fixtures_through_real_worker(tmp_path, name, arguments, expected):
  path = tmp_path / name
  path.write_bytes(make_pdf(**arguments))
  with closing(Operations()) as state:
    result = await state.view(str(path), 'fields')
  assert result.content == {'fields': expected}


@pytest.mark.asyncio
async def test_tax_form_through_real_worker(tmp_path):
  arguments, expected = tax_form_fixture()
  path = tmp_path / 'tax-form.pdf'
  path.write_bytes(make_pdf(**arguments))
  with closing(Operations()) as state:
    assert (await state.info(str(path))).details['form'] == 'xfa'
    fields = (await state.view(str(path), 'fields')).content['fields']
  assert len(fields) == len(expected)
  assert [
    {key: field[key] for key in sample} for field, sample in zip(fields, expected, strict=True)
  ] == expected


@pytest.mark.asyncio
async def test_field_options_cap_selection_and_following_call_through_worker(tmp_path):
  from dataclasses import replace

  data = make_pdf(
    [None, None],
    count=3,
    fields=[
      {'name': 'first'},
      {'page': 1, 'name': 'choice', 'type': 'Ch', 'options': [('x', 'Label')], 'value': 'x'},
    ],
  )
  file_type = replace(TYPE, limits=replace(TYPE.limits, max_view_entries=2))
  with closing(SubprocessRunner()) as runner:
    with pytest.raises(ValueError, match='field 2 on page 2; narrow pages'):
      await runner.run(Job(file_type, 'view', {'view': 'fields', 'pages': None}, data))
    result = await runner.run(Job(file_type, 'view', {'view': 'fields', 'pages': [2, 3]}, data))
    assert [(field['page_number'], field['name']) for field in result.ok['fields']] == [
      (2, 'choice')
    ]
  path = tmp_path / 'document'
  path.write_bytes(data)
  with closing(Operations()) as state:
    result = await state.view(str(path), 'fields', pages='3,1,1')
    assert [field['name'] for field in result.content['fields']] == ['first']
