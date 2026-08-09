import importlib.metadata

from bro_dev.bro import BroDev


def test_bro_dev_persona_and_registration():
  assert BroDev.name == 'bro-dev'
  assert BroDev.features == {'brog': True}
  assert BroDev.extra_secrets == ('github',)
  entries = importlib.metadata.entry_points(group='bro', name='bro-dev')
  assert [entry.value for entry in entries] == ['bro_dev.bro:BroDev']
