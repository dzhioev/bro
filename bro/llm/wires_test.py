import importlib.metadata
from types import SimpleNamespace

import pytest

from bro.llm import wires


def entry(name, target):
  return importlib.metadata.EntryPoint(name, target, wires.ENTRY_POINT_GROUP)


def test_roster_names_read_only_metadata(monkeypatch):
  monkeypatch.setattr(
    wires,
    '_entry_points',
    lambda: (entry('later', 'missing.module'), entry('first', 'also.missing')),
  )
  assert wires.known_names() == ('first', 'later')


def test_duplicate_and_invalid_names_fail_before_loading(monkeypatch):
  monkeypatch.setattr(
    wires, '_entry_points', lambda: (entry('same', 'missing'), entry('same', 'missing'))
  )
  with pytest.raises(ValueError, match='duplicate wire'):
    wires.known_names()
  monkeypatch.setattr(wires, '_entry_points', lambda: (entry('Bad', 'missing'),))
  with pytest.raises(ValueError, match='invalid wire name'):
    wires.known_names()


def test_explicit_wire_does_not_load_an_unrelated_broken_plugin(monkeypatch):
  monkeypatch.setattr(
    wires,
    '_entry_points',
    lambda: (entry('responses', 'bro.wire_responses.decode'), entry('broken', 'missing.module')),
  )
  registry = wires.WireRegistry()
  assert registry.decode('responses', {'status': 'completed', 'output': []}).stop == 'completed'
  with pytest.raises(RuntimeError, match='failed to load wire'):
    registry.get('broken')


def test_legacy_is_claimed_not_defaulted(monkeypatch):
  monkeypatch.setattr(
    wires, '_entry_points', lambda: (entry('responses', 'bro.wire_responses.decode'),)
  )
  registry = wires.WireRegistry()
  raw = {'status': 'completed', 'output': []}
  assert registry.decode(None, raw) == registry.decode('responses', raw)
  assert registry.legacy_name() == 'responses'
  monkeypatch.setattr(wires, '_entry_points', lambda: ())
  with pytest.raises(wires.WireUnavailable, match='no installed wire claims'):
    wires.WireRegistry().decode(None, raw)


def test_two_legacy_claimants_are_an_error(monkeypatch):
  monkeypatch.setattr(
    wires, '_entry_points', lambda: (entry('one', 'unused'), entry('two', 'unused'))
  )
  registry = wires.WireRegistry()
  monkeypatch.setattr(registry, 'get', lambda name: SimpleNamespace(LEGACY=True))
  with pytest.raises(ValueError, match='multiple wires claim untagged records'):
    registry.decode(None, {})


def test_open_refuses_a_wire_without_a_client():
  from bro.anthropic.declaration import LLMSpec

  with pytest.raises(wires.WireUnavailable, match='messages.*no client'):
    wires.open(LLMSpec(), {})


def test_open_refuses_wire_owned_recipe_options():
  from bro.openai.declaration import LLMSpec

  class Collision(LLMSpec):
    def request_options(self) -> dict:
      return {'previous_response_id': 'foreign-chain'}

  with pytest.raises(ValueError, match='wire-owned.*previous_response_id'):
    wires.open(Collision(), {})


def test_client_roster_reads_metadata_without_loading(monkeypatch):
  monkeypatch.setattr(
    wires.importlib.metadata,
    'entry_points',
    lambda **kwargs: (entry('later', 'missing.module'), entry('first', 'also.missing')),
  )
  assert wires.client_names() == ('first', 'later')
  monkeypatch.setattr(
    wires.importlib.metadata,
    'entry_points',
    lambda **kwargs: (entry('same', 'missing'), entry('same', 'missing')),
  )
  with pytest.raises(ValueError, match='duplicate wire client'):
    wires.client_names()


def test_entry_name_and_decode_side_name_must_match(monkeypatch):
  monkeypatch.setattr(
    wires, '_entry_points', lambda: (entry('different', 'bro.wire_responses.decode'),)
  )
  with pytest.raises(ValueError, match='same NAME'):
    wires.WireRegistry().get('different')


def test_open_names_the_wire_and_distribution_extra_when_the_sdk_is_missing(tmp_path, monkeypatch):
  from bro.native.echo_declaration import LLMSpec

  metadata = tmp_path / 'fixture_wire-1.dist-info'
  metadata.mkdir()
  (metadata / 'METADATA').write_text('Name: fixture-wire\nVersion: 1\n')
  (metadata / 'entry_points.txt').write_text(
    '[bro.llm.wire_clients]\nfixture = fixture_client:Client\n'
  )
  (tmp_path / 'fixture_client.py').write_text('import missing_fixture_sdk\n')
  monkeypatch.syspath_prepend(str(tmp_path))
  distribution = wires.importlib.metadata.PathDistribution(metadata)
  monkeypatch.setattr(
    wires, '_client_entries', lambda: {'fixture': distribution.entry_points['fixture']}
  )

  class Recipe(LLMSpec):
    WIRE = 'fixture'

  with pytest.raises(
    wires.WireUnavailable, match=r"wire 'fixture'.*fixture-wire\[client\]"
  ) as refused:
    wires.open(Recipe(), {})
  assert isinstance(refused.value.__cause__, ModuleNotFoundError)
