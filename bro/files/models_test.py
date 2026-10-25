import pytest
from pydantic import BaseModel, RootModel

from bro.files.models import InfoResult, ViewResult, validate_payload
from bro.files.types import View
from bro.files.types_test_helper import make_type

DIGEST = 'a' * 64


def test_info_envelope_preserves_validated_payload(tmp_path):
  file_type = make_type(tmp_path)
  payload = validate_payload(
    file_type.details, {'title': ' Invoice\u200b ', 'page_count': 2}, label='sample details'
  )
  assert payload == {'title': 'Invoice', 'page_count': 2}
  result = InfoResult.from_validated(
    file_type, size_bytes=123, sha256=DIGEST, details=payload
  ).model_dump()
  assert result == {
    'type': file_type.name,
    'size_bytes': 123,
    'sha256': DIGEST,
    'views': [{'name': view.name, 'about': view.about} for view in file_type.views],
    'details': payload,
  }


def test_view_envelope_preserves_the_selected_views_validated_payload(tmp_path):
  file_type = make_type(tmp_path)
  payload = validate_payload(
    file_type.get_view('text').content,
    {'pages': [{'page_number': 2, 'text': 'Кир\u200bиллица'}]},
    label='sample view text',
  )
  assert payload == {'pages': [{'page_number': 2, 'text': 'Кириллица'}]}
  result = ViewResult.from_validated(file_type, sha256=DIGEST, view='text', content=payload)
  assert result.model_dump() == {
    'type': file_type.name,
    'sha256': DIGEST,
    'view': 'text',
    'content': payload,
  }
  with pytest.raises(RuntimeError, match='sample view summary'):
    validate_payload(file_type.get_view('summary').content, payload, label='sample view summary')
  with pytest.raises(ValueError, match='unknown view'):
    ViewResult.from_validated(file_type, sha256=DIGEST, view='absent', content={})


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
    validate_payload(make_type(tmp_path).details, details, label='sample details')
  assert 'Invoice' not in str(error.value)


def test_invalid_strings_are_checked_before_extra_fields_can_be_dropped(tmp_path):
  with pytest.raises(RuntimeError, match='invalid file reply'):
    validate_payload(
      make_type(tmp_path).get_view('text').content,
      {'pages': [], 'ignored': '\udfff'},
      label='sample view text',
    )


def test_payload_must_serialize_as_an_object():
  with pytest.raises(RuntimeError, match='invalid file reply'):
    validate_payload(RootModel[list[str]], [], label='sample')  # type: ignore[arg-type]


@pytest.mark.parametrize('value', ['\x00', '\u200b'])
def test_validated_serialization_is_checked_for_invalid_strings(value):
  class BadDefault(BaseModel):
    title: str = value

  with pytest.raises(RuntimeError, match='invalid file reply'):
    validate_payload(BadDefault, {}, label='sample')


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
  payload = validate_payload(Aliased, {'nested_child': {'document_title': 'hello'}}, label='sample')
  details = InfoResult.from_validated(
    file_type, size_bytes=1, sha256=DIGEST, details=payload
  ).details
  content = ViewResult.from_validated(
    file_type, sha256=DIGEST, view='text', content=payload
  ).content
  assert details == content == {'child': {'title': 'hello'}}
  validate_manual(file_type, ' '.join([*details, *details['child']]))
  with pytest.raises(ValueError, match='manual omits fields: child, title'):
    validate_manual(file_type, 'nested_child document_title')
