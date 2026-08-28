import importlib.metadata

from bros.bro_eyebro import BroEyebro


def test_bro_eyebro_is_registered_as_a_persona():
  entries = importlib.metadata.entry_points(group='bro', name=BroEyebro.name)
  assert [entry.load() for entry in entries] == [BroEyebro]
