import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.benchmark import import_trails, retention
from bro.benchmark.retention_test import FakeS3, _configure, _write_job
from bro.trails.local import LocalStore


def _retained_run(monkeypatch, tmp_path, **job):
  source = tmp_path / 'raw-job'
  _write_job(source, **job)
  storage = FakeS3()
  _configure(monkeypatch, storage)
  retained = retention.retain_job(source)
  monkeypatch.setattr(
    import_trails.boto3,
    'Session',
    lambda region_name: SimpleNamespace(client=lambda service: storage),
  )
  return source, storage, retained


def test_import_trails_moves_each_trial_store_into_the_registry(monkeypatch, tmp_path):
  source, _, retained = _retained_run(monkeypatch, tmp_path)
  downloads: list[list[Path]] = []
  download_files = retention.download_files

  def observed(client, config, prefix, files, destination):
    downloads.append([file.path for file in files])
    download_files(client, config, prefix, files, destination)

  monkeypatch.setattr(import_trails.retention, 'download_files', observed)
  registry = LocalStore(tmp_path / 'registry')

  imported = import_trails.import_run(retained.prefix + '/', registry)
  again = import_trails.import_run(retained.prefix, registry)

  trial_store = LocalStore(source / 'trial-one' / retention.TRAILS_DIRECTORY)
  [root] = trial_store.stored_trail_ids()
  assert imported == again == [import_trails.ImportedTrial('trial-one', [root])]
  assert registry.get_trail(root) == trial_store.get_trail(root)
  assert list(registry.iter_steps(root)) == list(trial_store.iter_steps(root))
  store_directory = Path('trial-one') / retention.TRAILS_DIRECTORY
  assert len(downloads) == 2
  assert all(path.is_relative_to(store_directory) for batch in downloads for path in batch)


def test_import_trails_skips_a_trial_that_recorded_no_trail(monkeypatch, tmp_path):
  _, _, retained = _retained_run(monkeypatch, tmp_path, error_trial=True)

  imported = import_trails.import_run(retained.prefix, LocalStore(tmp_path / 'registry'))

  assert [trial.trial for trial in imported] == ['trial-one']


def test_import_trails_refuses_a_store_without_the_trail_the_manifest_names(monkeypatch, tmp_path):
  _, storage, retained = _retained_run(monkeypatch, tmp_path)
  key = (retained.bucket, f'{retained.prefix}/{retention.MANIFEST_FILENAME}')
  manifest = json.loads(storage.objects[key][0])
  manifest['trials'][0]['root_trail_id'] = 'elsewhere'
  storage.objects[key] = (json.dumps(manifest).encode(), '')

  with pytest.raises(ValueError, match='reports trail elsewhere, which its store does not hold'):
    import_trails.import_run(retained.prefix, LocalStore(tmp_path / 'registry'))
