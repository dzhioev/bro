import importlib.metadata

from bro.bench import credentials


def test_bro_bench_contributes_the_harbor_credential_kind():
  entries = importlib.metadata.entry_points(group='bro.credentials', name='harbor')

  [entry] = entries
  assert entry.value == 'bro.bench.credentials:HARBOR'
  assert entry.load() == credentials.HARBOR


def test_bro_bench_contributes_the_retention_credential_kind():
  entries = importlib.metadata.entry_points(group='bro.credentials', name='benchmark_retention')

  [entry] = entries
  assert entry.value == 'bro.bench.credentials:RETENTION'
  assert entry.load() == credentials.RETENTION
