import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.benchmark import publication, retention
from bro.benchmark.retention_test import JOB_ID, FakeS3, _configure, _write_job


def _retained_run(monkeypatch, tmp_path):
  source = tmp_path / 'raw-job'
  _write_job(source)
  storage = FakeS3()
  _configure(monkeypatch, storage)
  retained = retention.retain_job(source)
  monkeypatch.setattr(
    publication.retention,
    'configured_retention',
    lambda: retention.RetentionConfig(bucket='benchmark-runs', region='us-east-1'),
  )
  monkeypatch.setattr(
    publication.boto3,
    'Session',
    lambda region_name: SimpleNamespace(client=lambda service: storage),
  )
  monkeypatch.setattr(publication.credentials, 'get', lambda name: 'scoped-harbor-key')
  return source, storage, retained


def test_publish_derives_a_scratch_job_and_appends_its_hub_record(monkeypatch, tmp_path):
  source, storage, retained = _retained_run(monkeypatch, tmp_path)
  captured = {}

  def upload(job_directory: Path, visibility: publication.Visibility, api_key: str) -> str:
    captured['job_directory'] = job_directory
    captured['visibility'] = visibility
    captured['api_key'] = api_key
    trial = json.loads((job_directory / 'trial-one' / 'result.json').read_text())
    job = json.loads((job_directory / 'result.json').read_text())
    captured['trial_cost'] = trial['agent_result']['cost_usd']
    captured['job_cost'] = job['stats']['cost_usd']
    captured['trajectory'] = json.loads(
      (job_directory / 'trial-one' / 'agent' / 'trajectory.json').read_text()
    )
    return f'https://hub.harborframework.com/jobs/{JOB_ID}'

  monkeypatch.setattr(publication, '_upload', upload)
  monkeypatch.setattr(publication, '_publication_time', lambda: '2026-09-11T02:30:00Z')

  published = publication.publish_run(retained.prefix + '/', 'private')

  assert captured['visibility'] == 'private'
  assert captured['api_key'] == 'scoped-harbor-key'
  assert captured['trial_cost'] == pytest.approx(0.0000636)
  assert captured['job_cost'] == pytest.approx(0.0000636)
  assert captured['trajectory']['steps'][1]['metrics']['cost_usd'] == pytest.approx(0.0000636)
  assert not captured['job_directory'].exists()
  assert not (source / 'trial-one' / 'agent' / 'trajectory.json').exists()
  raw_trial = json.loads((source / 'trial-one' / 'result.json').read_text())
  assert raw_trial.get('agent_result') is None
  assert published.url == f'https://hub.harborframework.com/jobs/{JOB_ID}'
  assert published.record_url == (
    f's3://benchmark-runs/{retained.prefix}/publications/2026-09-11T02:30:00Z.json'
  )
  record, _ = storage.objects[
    ('benchmark-runs', f'{retained.prefix}/publications/2026-09-11T02:30:00Z.json')
  ]
  assert json.loads(record) == {
    'published_at': '2026-09-11T02:30:00Z',
    'url': f'https://hub.harborframework.com/jobs/{JOB_ID}',
    'visibility': 'private',
  }
  assert storage.puts[-1]['IfNoneMatch'] == '*'


def test_harbor_upload_receives_the_scoped_key_and_explicit_visibility(monkeypatch, tmp_path):
  captured = {}
  monkeypatch.setattr(publication.shutil, 'which', lambda command: '/venv/bin/harbor')

  def run(command, **kwargs):
    captured['command'] = command
    captured['environment'] = kwargs['env']
    assert kwargs['capture_output'] is True
    assert kwargs['text'] is True
    return SimpleNamespace(
      returncode=0,
      stdout='Uploaded 1 trial(s)\nView at https://hub.harborframework.com/jobs/job-id\n',
      stderr='',
    )

  monkeypatch.setattr(publication.subprocess, 'run', run)

  url = publication._upload(tmp_path, 'public', 'scoped-key')

  assert captured['command'] == ['/venv/bin/harbor', 'upload', str(tmp_path), '--public']
  assert captured['environment']['HARBOR_API_KEY'] == 'scoped-key'
  assert url == 'https://hub.harborframework.com/jobs/job-id'


def test_a_tampered_retained_file_is_refused_before_upload(monkeypatch, tmp_path):
  _, storage, retained = _retained_run(monkeypatch, tmp_path)
  key = ('benchmark-runs', f'{retained.prefix}/result.json')
  _, checksum = storage.objects[key]
  storage.objects[key] = (b'{}', checksum)
  monkeypatch.setattr(
    publication,
    '_upload',
    lambda *args: pytest.fail('tampered input must not reach Harbor'),
  )

  with pytest.raises(publication.PublicationError, match='size differs'):
    publication.publish_run(retained.prefix, 'public')


def test_an_upload_failure_writes_no_publication_record(monkeypatch, tmp_path):
  _, storage, retained = _retained_run(monkeypatch, tmp_path)
  monkeypatch.setattr(
    publication,
    '_upload',
    lambda *args: (_ for _ in ()).throw(publication.PublicationError('upload failed')),
  )

  with pytest.raises(publication.PublicationError, match='upload failed'):
    publication.publish_run(retained.prefix, 'public')

  assert not any('/publications/' in key for _, key in storage.objects)


@pytest.mark.parametrize(
  'prefix',
  [
    'other/2026-09-11/id',
    'runs/id',
    '/runs/2026-09-11/id',
    'runs/../id',
  ],
)
def test_a_run_prefix_has_one_strict_shape(prefix):
  with pytest.raises(ValueError, match='run prefix'):
    publication._run_prefix(prefix)
