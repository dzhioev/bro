from typing import Optional

import pytest
from pydantic import BaseModel

from bro.files import operations, registry
from bro.files.types_test_helper import EntryPoint, make_type


@pytest.fixture
def installed(monkeypatch):
  monkeypatch.setattr(operations, 'registry', registry.TypeRegistry())

  def install(*entries):
    monkeypatch.setattr(registry, '_entry_points', lambda: entries)

  return install


def test_shared_reference_works_without_installed_types(installed):
  installed()
  text = operations.read_reference()
  assert '## Installed file types\n\n(none)' in text
  assert 'Untrusted data' in text
  with pytest.raises(
    ValueError, match="unknown file type 'sample'.*installed file types: \\(none\\)"
  ):
    operations.read_reference('sample')


def test_shared_reference_lists_metadata_without_importing_types(installed):
  entries = [EntryPoint(name, ImportError('not importable')) for name in ('second', 'first')]
  installed(*entries)
  text = operations.read_reference()
  assert text.endswith('- first\n- second\n')
  assert all(entry.loaded == 0 for entry in entries)


def test_type_reference_loads_only_selected_type_and_returns_its_manual(installed, tmp_path):
  file_type = make_type(tmp_path)
  selected = EntryPoint('sample', file_type)
  broken = EntryPoint('broken', ImportError('unavailable'))
  installed(selected, broken)
  assert operations.read_reference('sample') == file_type.manual.read_text()
  assert selected.loaded == 1
  assert broken.loaded == 0
  with pytest.raises(ValueError, match='installed file types: broken, sample'):
    operations.read_reference('absent')


def test_manual_check_reaches_nested_and_recursive_model_fields(tmp_path):
  class Node(BaseModel):
    leaf_value: str
    children: list['Node']

  class Tree(BaseModel):
    root_node: Optional[Node]

  file_type = make_type(tmp_path, details=Tree)
  manual = file_type.manual.read_text().replace('title page_count', 'root_node children')
  with pytest.raises(ValueError, match='manual omits fields: leaf_value'):
    operations.validate_manual(file_type, manual)
  operations.validate_manual(file_type, manual + ' leaf_value')


def test_manual_check_requires_whole_field_names(tmp_path):
  file_type = make_type(tmp_path)
  with pytest.raises(ValueError, match='omits fields: title'):
    operations.validate_manual(file_type, 'subtitle page_count pages page_number text count')


def test_read_reference_checks_manual_coverage_and_sanitizes(installed, tmp_path):
  file_type = make_type(tmp_path)
  file_type.manual.write_text('title page_count pages page_number te\u200bxt count')
  installed(EntryPoint('sample', file_type))
  assert operations.read_reference('sample') == 'title page_count pages page_number text count'
  file_type.manual.write_text('title page_count')
  with pytest.raises(ValueError, match='manual omits fields: count, page_number, pages, text'):
    operations.read_reference('sample')


@pytest.mark.parametrize('unrelated_names', [('BAD',), ('other', 'other')])
def test_type_reference_ignores_unrelated_invalid_metadata(installed, tmp_path, unrelated_names):
  file_type = make_type(tmp_path)
  selected = EntryPoint('sample', file_type)
  unrelated = [EntryPoint(name, ImportError('not loaded')) for name in unrelated_names]
  installed(*unrelated, selected)
  assert operations.read_reference('sample') == file_type.manual.read_text()
  assert selected.loaded == 1
  assert all(entry.loaded == 0 for entry in unrelated)
  with pytest.raises(ValueError, match='invalid file-type name|duplicate file type'):
    operations.read_reference()


def test_type_reference_refuses_duplicate_selected_entries(installed, tmp_path):
  file_type = make_type(tmp_path)
  entry = EntryPoint('sample', file_type)
  installed(entry, entry)
  with pytest.raises(ValueError, match="duplicate file type 'sample'"):
    operations.read_reference('sample')
  assert entry.loaded == 0
