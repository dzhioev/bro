import dataclasses

import pytest

from bro.files.types import Limits, Signature, View
from bro.files.types_test_helper import Content, make_type


def test_contract_is_frozen_and_never_imports_the_provider(tmp_path):
  file_type = make_type(tmp_path)
  with pytest.raises(dataclasses.FrozenInstanceError):
    file_type.name = 'changed'  # type: ignore[misc]
  assert file_type.get_view('text').content is Content
  with pytest.raises(ValueError, match='use one of: text, summary'):
    file_type.get_view('absent')


@pytest.mark.parametrize('name', ['', 'PDF', '-type', 'has space', 'has_underscore'])
def test_invalid_type_names(tmp_path, name):
  with pytest.raises(ValueError, match='invalid file-type name'):
    make_type(tmp_path, name=name)


@pytest.mark.parametrize('marker,within', [(b'', 0), (b'x', -1), (b'xx', 4095), (b'x', True)])
def test_invalid_signatures(marker, within):
  with pytest.raises(ValueError):
    Signature(marker, within)


def test_signature_offset_is_inclusive_and_complete():
  signature = Signature(b'MAGIC', within=3)
  assert signature.matches(b'MAGICbody')
  assert signature.matches(b'xxxMAGICbody')
  assert not signature.matches(b'xxxxMAGIC')
  assert not signature.matches(b'xxxMAGI')
  assert Signature(b'x', within=4095).matches(b'_' * 4095 + b'x')


@pytest.mark.parametrize('about', ['', 'two\nlines', 'two\rlines', '  ', 'text\n', 'text\r'])
def test_view_about_is_one_nonempty_line(about):
  with pytest.raises(ValueError, match='one nonempty line'):
    View('text', about, Content, True)


@pytest.mark.parametrize(
  'field', ['max_bytes', 'max_pages', 'max_view_characters', 'max_view_entries']
)
@pytest.mark.parametrize('value', [0, -1, True])
def test_limits_require_positive_integers(tmp_path, field, value):
  limits = make_type(tmp_path).limits
  with pytest.raises(ValueError, match=field):
    dataclasses.replace(limits, **{field: value})


def test_unpaged_type_rejects_a_paged_view(tmp_path):
  with pytest.raises(ValueError, match='without pages'):
    make_type(tmp_path, limits=Limits(10, None, 10, 10))
  file_type = make_type(tmp_path, limits=Limits(10, None, 10, 10), views=())
  assert file_type.limits.max_pages is None


@pytest.mark.parametrize(
  'provider', ['parser', 'parser:', ':build', 'parser:build()', 'bad-name:build']
)
def test_provider_is_an_import_spec(tmp_path, provider):
  with pytest.raises(ValueError, match='module:attribute'):
    make_type(tmp_path, provider=provider)


def test_duplicate_views_are_rejected(tmp_path):
  view = View('text', 'Read text', Content, True)
  with pytest.raises(ValueError, match='duplicate view'):
    make_type(tmp_path, views=(view, view))


@pytest.mark.parametrize('field', ['details', 'content'])
def test_declared_payloads_are_model_classes(tmp_path, field):
  with pytest.raises(TypeError, match='pydantic model classes'):
    if field == 'details':
      make_type(tmp_path, details=dict)
    else:
      View('text', 'Read text', dict, True)  # type: ignore[arg-type]
