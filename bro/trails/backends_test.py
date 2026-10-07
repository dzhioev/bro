from dataclasses import replace

import pytest

from bro.trails import backends
from bro.trails.bro_format import BRO_FORMAT
from bro.trails.claude_format import CLAUDE_FORMAT


class EntryPoint:
  def __init__(self, name: str, value: object):
    self.name = name
    self.value = f'test:{name}'
    self._value = value
    self.loaded = 0

  def load(self):
    self.loaded += 1
    return self._value


def _install(monkeypatch, *entries: EntryPoint) -> None:
  monkeypatch.setattr(backends, '_entry_points', lambda: entries)


def test_installed_names_read_metadata_without_loading_formats(monkeypatch):
  bro = EntryPoint('bro', BRO_FORMAT)
  claude = EntryPoint('claude', CLAUDE_FORMAT)
  _install(monkeypatch, claude, bro)

  assert backends.installed_format_names() == ('bro', 'claude')
  assert not bro.loaded
  assert not claude.loaded


def test_get_format_loads_only_the_selected_entry(monkeypatch):
  selected = EntryPoint('bro', BRO_FORMAT)
  untouched = EntryPoint('claude', CLAUDE_FORMAT)
  _install(monkeypatch, selected, untouched)

  assert backends.get_format('bro') is BRO_FORMAT
  assert selected.loaded
  assert not untouched.loaded


def test_format_registry_caches_each_selected_format(monkeypatch):
  entry = EntryPoint('bro', BRO_FORMAT)
  _install(monkeypatch, entry)
  registry = backends.FormatRegistry()

  assert registry.get('bro') is BRO_FORMAT
  assert registry.get('bro') is BRO_FORMAT
  assert entry.loaded == 1


def test_get_format_refuses_a_non_format_object(monkeypatch):
  _install(monkeypatch, EntryPoint('bro', object()))

  with pytest.raises(TypeError, match='must load a TrailFormat instance'):
    backends.get_format('bro')


def test_get_format_refuses_a_different_loaded_name(monkeypatch):
  _install(monkeypatch, EntryPoint('bro', replace(BRO_FORMAT, name='other')))

  with pytest.raises(ValueError, match="loads a format named 'other'"):
    backends.get_format('bro')


def test_get_format_names_the_installed_roster_on_a_miss(monkeypatch):
  _install(monkeypatch, EntryPoint('claude', CLAUDE_FORMAT))

  with pytest.raises(
    ValueError,
    match="trail format 'bro' is not installed; installed trail formats: claude",
  ):
    backends.get_format('bro')


def test_registry_refuses_duplicate_names(monkeypatch):
  _install(monkeypatch, EntryPoint('bro', BRO_FORMAT), EntryPoint('bro', BRO_FORMAT))

  with pytest.raises(ValueError, match="duplicate trail format 'bro'"):
    backends.installed_format_names()


@pytest.mark.parametrize('name', ['', 'Claude', 'two words', '-leading'])
def test_format_names_have_a_stable_grammar(name):
  with pytest.raises(ValueError, match='invalid trail-format name'):
    replace(BRO_FORMAT, name=name)
