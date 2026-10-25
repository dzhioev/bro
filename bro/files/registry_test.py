from dataclasses import replace

import pytest

from bro.files import registry
from bro.files.types import Signature
from bro.files.types_test_helper import EntryPoint, make_type


def install(monkeypatch, *entries):
  monkeypatch.setattr(registry, '_entry_points', lambda: entries)


def test_metadata_names_load_nothing_and_cache_loads_one_entry(monkeypatch, tmp_path):
  file_type = make_type(tmp_path)
  selected = EntryPoint('sample', file_type)
  untouched = EntryPoint('broken', ImportError('parser missing'))
  install(monkeypatch, selected, untouched)
  assert registry.installed_type_names() == ('broken', 'sample')
  assert selected.loaded == untouched.loaded == 0
  cache = registry.TypeRegistry()
  assert cache.get('sample') is file_type
  assert cache.get('sample') is file_type
  assert selected.loaded == 1
  assert untouched.loaded == 0


def test_identify_loads_every_type_and_names_the_broken_entry(monkeypatch, tmp_path):
  good = EntryPoint('sample', make_type(tmp_path))
  broken = EntryPoint('z-broken', ImportError('missing dependency'))
  install(monkeypatch, good, broken)
  cache = registry.TypeRegistry()
  assert cache.resolve(b'not a matching header', type='sample') is good._value
  assert broken.loaded == 0
  with pytest.raises(RuntimeError, match="failed to load file type 'z-broken'.*missing dependency"):
    cache.identify(b'SAMPLE')
  assert good.loaded == broken.loaded == 1


def test_explicit_type_overrides_signature_and_reaches_signatureless_type(monkeypatch, tmp_path):
  sample = make_type(tmp_path)
  other = replace(sample, name='other', signatures=())
  install(monkeypatch, EntryPoint('sample', sample), EntryPoint('other', other))
  cache = registry.TypeRegistry()
  assert cache.identify(b'xxSAMPLEtail') is sample
  assert cache.resolve(b'SAMPLE', type='other') is other
  with pytest.raises(ValueError, match='no file type matches.*other, sample.*pass type'):
    cache.identify(b'xxxSAMPLE')


def test_identify_refuses_ambiguity(monkeypatch, tmp_path):
  sample = make_type(tmp_path)
  install(
    monkeypatch, EntryPoint('sample', sample), EntryPoint('other', replace(sample, name='other'))
  )
  with pytest.raises(ValueError, match='ambiguous file type: other, sample.*pass type'):
    registry.TypeRegistry().identify(b'SAMPLE')


def test_multiple_signatures_match_a_type_only_once(monkeypatch, tmp_path):
  sample = make_type(tmp_path, signatures=(Signature(b'SAMPLE', 0), Signature(b'SAM', 0)))
  install(monkeypatch, EntryPoint('sample', sample))
  assert registry.TypeRegistry().identify(b'SAMPLE') is sample


@pytest.mark.parametrize('operation', ['names', 'get', 'identify'])
def test_duplicates_are_rejected_before_loading(monkeypatch, tmp_path, operation):
  entry = EntryPoint('sample', make_type(tmp_path))
  install(monkeypatch, entry, entry)
  with pytest.raises(ValueError, match="duplicate file type 'sample'"):
    if operation == 'names':
      registry.installed_type_names()
    elif operation == 'get':
      registry.get_type('sample')
    else:
      registry.TypeRegistry().identify(b'SAMPLE')
  assert entry.loaded == 0


@pytest.mark.parametrize('operation', ['get', 'identify'])
def test_no_types_instructs_installation(monkeypatch, operation):
  install(monkeypatch)
  with pytest.raises(ValueError, match='no file types installed; install.*bro-filetypes'):
    if operation == 'get':
      registry.TypeRegistry().get('sample')
    else:
      registry.TypeRegistry().identify(b'')


def test_unknown_type_names_installed_types(monkeypatch, tmp_path):
  install(monkeypatch, EntryPoint('sample', make_type(tmp_path)))
  with pytest.raises(ValueError, match="unknown file type 'other'.*sample"):
    registry.get_type('other')


@pytest.mark.parametrize('loaded', [object(), ImportError('broken')])
def test_identify_names_invalid_or_unloadable_entry(monkeypatch, loaded):
  install(monkeypatch, EntryPoint('sample', loaded))
  with pytest.raises(RuntimeError, match="failed to load file type 'sample'"):
    registry.TypeRegistry().identify(b'')


def test_entry_name_must_match_loaded_type(monkeypatch, tmp_path):
  install(monkeypatch, EntryPoint('sample', make_type(tmp_path, name='other')))
  with pytest.raises(ValueError, match="loads a type named 'other'"):
    registry.get_type('sample')


def test_entry_must_load_a_file_type(monkeypatch):
  install(monkeypatch, EntryPoint('sample', object()))
  with pytest.raises(TypeError, match='must load a FileType instance'):
    registry.get_type('sample')


def test_entry_point_metadata_uses_the_public_group(monkeypatch, tmp_path):
  entry = EntryPoint('sample', make_type(tmp_path))

  def entries(*, group):
    assert group == 'bro.file_types'
    return (entry,)

  monkeypatch.setattr(registry.importlib.metadata, 'entry_points', entries)
  assert registry.get_type('sample') is entry._value


@pytest.mark.parametrize('unrelated_names', [('BAD',), ('other', 'other')])
def test_explicit_selection_ignores_unrelated_invalid_metadata(
  monkeypatch, tmp_path, unrelated_names
):
  file_type = make_type(tmp_path)
  selected = EntryPoint('sample', file_type)
  unrelated = [EntryPoint(name, ImportError('not loaded')) for name in unrelated_names]
  install(monkeypatch, *unrelated, selected)
  assert registry.TypeRegistry().resolve(b'no matching signature', type='sample') is file_type
  assert selected.loaded == 1
  assert all(entry.loaded == 0 for entry in unrelated)
  with pytest.raises(ValueError, match='invalid file-type name|duplicate file type'):
    registry.TypeRegistry().identify(b'SAMPLE')
