import importlib.metadata

from bros.bro_dev import BroDev


def test_bro_dev_is_registered_as_a_persona():
  entries = importlib.metadata.entry_points(group='bro', name=BroDev.name)
  assert [entry.load() for entry in entries] == [BroDev]
