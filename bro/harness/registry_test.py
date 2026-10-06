import pytest

import bro.harness as harness_registry
from bro.harness import Harness


class EntryPoint:
  def __init__(self, name: str, value: object):
    self.name = name
    self.value = f'test:{name}'
    self._value = value
    self.loaded = False

  def load(self):
    self.loaded = True
    return self._value


class NamedHarness(Harness):
  name = 'named'


class OtherHarness(Harness):
  name = 'other'


def _install(monkeypatch, *entries: EntryPoint) -> None:
  monkeypatch.setattr(harness_registry, '_entry_points', lambda: entries)


def test_installed_names_read_metadata_without_loading_harnesses(monkeypatch):
  first = EntryPoint('named', NamedHarness())
  second = EntryPoint('other', OtherHarness())
  _install(monkeypatch, second, first)

  assert harness_registry.installed_harness_names() == ('named', 'other')
  assert not first.loaded
  assert not second.loaded


def test_get_harness_loads_only_the_selected_entry(monkeypatch):
  selected = EntryPoint('named', NamedHarness())
  untouched = EntryPoint('other', OtherHarness())
  _install(monkeypatch, selected, untouched)

  assert harness_registry.get_harness('named') is selected._value
  assert selected.loaded
  assert not untouched.loaded


def test_get_harness_refuses_a_non_harness_object(monkeypatch):
  _install(monkeypatch, EntryPoint('named', object()))

  with pytest.raises(TypeError, match='must load a Harness instance'):
    harness_registry.get_harness('named')


def test_get_harness_refuses_a_different_loaded_name(monkeypatch):
  _install(monkeypatch, EntryPoint('named', OtherHarness()))

  with pytest.raises(ValueError, match="loads a harness named 'other'"):
    harness_registry.get_harness('named')


def test_get_harness_names_the_installed_roster_on_a_miss(monkeypatch):
  _install(monkeypatch, EntryPoint('other', OtherHarness()))

  with pytest.raises(
    ValueError, match="harness 'named' is not installed; installed harnesses: other"
  ):
    harness_registry.get_harness('named')


def test_registry_refuses_duplicate_names(monkeypatch):
  _install(monkeypatch, EntryPoint('named', NamedHarness()), EntryPoint('named', NamedHarness()))

  with pytest.raises(ValueError, match="duplicate harness 'named'"):
    harness_registry.installed_harness_names()


@pytest.mark.parametrize('name', ['', 'Claude', 'two words', '-leading'])
def test_harness_names_have_a_stable_grammar(name):
  with pytest.raises(ValueError, match='invalid harness name'):
    Harness(name)
