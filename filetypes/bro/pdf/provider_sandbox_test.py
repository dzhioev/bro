import hashlib
from contextlib import closing

import pytest

from bro.files.operations import Operations, read_reference
from bro.files.protocol import Job
from bro.files.runner import SubprocessRunner
from bro.pdf import TYPE
from bro.pdf.pdf_test_helper import make_pdf


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
