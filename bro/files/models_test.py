import pytest
from pydantic import BaseModel, RootModel

from bro.files.models import InfoResult, ViewResult
from bro.files.types import View
from bro.files.types_test_helper import make_type

DIGEST = 'a' * 64


def test_info_envelope_uses_declared_shape_and_validated_values(tmp_path):
  file_type = make_type(tmp_path)
  result = InfoResult.from_reply(
    file_type, size_bytes=123, sha256=DIGEST, details={'title': ' Invoice\u200b ', 'page_count': 2}
  ).model_dump()
  assert result == {
    'type': file_type.name,
    'size_bytes': 123,
    'sha256': DIGEST,
    'views': [{'name': view.name, 'about': view.about} for view in file_type.views],
    'details': {'title': 'Invoice', 'page_count': 2},
  }


def test_view_validates_the_selected_views_model_and_returns_a_dict(tmp_path):
  file_type = make_type(tmp_path)
  result = ViewResult.from_reply(
    file_type,
    sha256=DIGEST,
    view='text',
    content={'pages': [{'page_number': 2, 'text': 'Кир\u200bиллица'}]},
  ).model_dump()
  assert result == {
    'type': file_type.name,
    'sha256': DIGEST,
    'view': 'text',
    'content': {'pages': [{'page_number': 2, 'text': 'Кириллица'}]},
  }
  with pytest.raises(RuntimeError, match='sample view summary'):
    ViewResult.from_reply(file_type, sha256=DIGEST, view='summary', content=result['content'])
  assert ViewResult.from_reply(
    file_type, sha256=DIGEST, view='summary', content={'count': 2}
  ).content == {'count': 2}
  with pytest.raises(ValueError, match='unknown view'):
    ViewResult.from_reply(file_type, sha256=DIGEST, view='absent', content={})


@pytest.mark.parametrize(
  'details',
  [
    {'title': 'Invoice'},
    {'title': 'Invoice', 'page_count': '2'},
    {'title': 'Invoice', 'page_count': 2, 'other': 'extra'},
    {'title': '\ud800', 'page_count': 2},
    {'title': 'Invoice\x00', 'page_count': 2},
  ],
)
def test_invalid_payloads_are_named_failures_without_echoing_data(tmp_path, details):
  with pytest.raises(RuntimeError, match='invalid file reply: sample details') as error:
    InfoResult.from_reply(make_type(tmp_path), size_bytes=1, sha256=DIGEST, details=details)
  assert 'Invoice' not in str(error.value)


def test_invalid_strings_are_checked_before_extra_fields_can_be_dropped(tmp_path):
  with pytest.raises(RuntimeError, match='invalid file reply'):
    ViewResult.from_reply(
      make_type(tmp_path), sha256=DIGEST, view='text', content={'pages': [], 'ignored': '\udfff'}
    )


def test_payload_must_serialize_as_an_object(tmp_path):
  file_type = make_type(tmp_path, details=RootModel[list[str]])
  with pytest.raises(RuntimeError, match='invalid file reply'):
    InfoResult.from_reply(file_type, size_bytes=1, sha256=DIGEST, details=[])  # type: ignore[arg-type]


@pytest.mark.parametrize('value', ['\x00', '\u200b'])
def test_validated_serialization_is_checked_for_invalid_strings(tmp_path, value):
  class BadDefault(BaseModel):
    title: str = value

  file_type = make_type(tmp_path, views=(View('text', 'Read text', BadDefault, False),))
  with pytest.raises(RuntimeError, match='invalid file reply'):
    ViewResult.from_reply(file_type, sha256=DIGEST, view='text', content={})


def test_manual_coverage_uses_emitted_names_even_when_models_prefer_aliases(tmp_path):
  from pydantic import ConfigDict, Field

  from bro.files.operations import validate_manual

  class Child(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    title: str = Field(alias='document_title')

  class Aliased(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    child: Child = Field(alias='nested_child')

  file_type = make_type(
    tmp_path, details=Aliased, views=(View('text', 'Read nested text', Aliased, False),)
  )
  payload = {'nested_child': {'document_title': 'hello'}}
  details = InfoResult.from_reply(file_type, size_bytes=1, sha256=DIGEST, details=payload).details
  content = ViewResult.from_reply(file_type, sha256=DIGEST, view='text', content=payload).content
  assert details == content == {'child': {'title': 'hello'}}
  validate_manual(file_type, ' '.join([*details, *details['child']]))
  with pytest.raises(ValueError, match='manual omits fields: child, title'):
    validate_manual(file_type, 'nested_child document_title')
