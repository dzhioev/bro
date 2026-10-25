import hashlib
from contextlib import closing

import pytest

from bro.files import operations, registry
from bro.files.operations_test_helper import Content, Details
from bro.files.types import Limits, Signature, View
from bro.files.types_test_helper import EntryPoint, make_type


@pytest.fixture
def state(monkeypatch, tmp_path):
  file_type = make_type(
    tmp_path,
    details=Details,
    views=(View('text', 'Read text', Content, True),),
    signatures=(Signature(b'SAMPLE', 0),),
    provider='bro.files.operations_test_helper:provider',
    limits=Limits(100000, 5, 100000, 20),
  )
  monkeypatch.setattr(registry, '_entry_points', lambda: (EntryPoint('sample', file_type),))
  monkeypatch.setattr(operations, 'registry', registry.TypeRegistry())
  with closing(operations.Operations()) as state:
    yield state


@pytest.mark.asyncio
async def test_snapshot_info_and_view_through_real_worker(state, tmp_path):
  path = tmp_path / 'document'
  data = 'SAMPLE\nЗдравствуйте'.encode()
  path.write_bytes(data)
  info = await state.info(str(path))
  assert info.sha256 == hashlib.sha256(data).hexdigest() == info.details['title']
  assert info.size_bytes == len(data)
  view = await state.view(str(path), 'text', '2')
  assert view.sha256 == info.sha256
  assert view.content['pages'] == [
    {'page_number': 2, 'notes': ['untrusted text'], 'text': data.decode()}
  ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'data,exception,cause',
  [
    (b'invalid', ValueError, 'damaged document; provide'),
    (b'engine', RuntimeError, 'file engine failure'),
  ],
)
async def test_worker_failures_surface_named_causes_and_allow_next_call(
  state, tmp_path, data, exception, cause
):
  path = tmp_path / 'document'
  path.write_bytes(data)
  with pytest.raises(exception, match=cause) as error:
    await state.info(str(path), type='sample')
  assert 'private document' not in str(error.value)
  path.write_bytes(b'SAMPLE')
  assert await state.info(str(path))


@pytest.mark.asyncio
async def test_real_operations_validate_declared_payloads_exactly_once(monkeypatch, tmp_path):
  from contextlib import closing

  from pydantic import field_validator

  class PrefixedDetails(Details):
    @field_validator('title')
    @classmethod
    def prefix_title(cls, value):
      if value.startswith('sha256:'):
        raise ValueError('expected a raw digest')
      return 'sha256:' + value

  class PrefixedContent(Content):
    @field_validator('pages')
    @classmethod
    def prefix_text(cls, pages):
      for page in pages:
        page.text = 'text:' + page.text
      return pages

  file_type = make_type(
    tmp_path,
    details=PrefixedDetails,
    views=(View('text', 'Read text', PrefixedContent, True),),
    provider='bro.files.operations_test_helper:provider',
  )
  monkeypatch.setattr(registry, '_entry_points', lambda: (EntryPoint('sample', file_type),))
  monkeypatch.setattr(operations, 'registry', registry.TypeRegistry())
  path = tmp_path / 'document'
  path.write_bytes(b'SAMPLE')
  with closing(operations.Operations()) as state:
    info = await state.info(str(path))
    view = await state.view(str(path), 'text', '1')
  assert info.details['title'] == 'sha256:' + info.sha256
  assert view.content['pages'][0]['text'] == 'text:SAMPLE'
