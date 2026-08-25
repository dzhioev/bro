import importlib.metadata
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from bro.benchmark import retention

JOB_ID = UUID('12345678-1234-5678-1234-567812345678')
FRAMEWORK_REVISION = 'sha256:' + 'a' * 64
SOURCE_COMMIT = 'b' * 40


class FakeS3:
  def __init__(self):
    self.uploads = []

  def upload_file(self, path, bucket, key):
    self.uploads.append((Path(path), bucket, key))


def _write_job(job_directory: Path, *, uploaded: bool = True) -> dict:
  job_directory.mkdir(parents=True)
  started_at = datetime(2026, 8, 24, 20, 36, 50, 123456, tzinfo=UTC)
  result = {
    'id': str(JOB_ID),
    'started_at': started_at.isoformat(),
    'finished_at': started_at.isoformat(),
    'n_total_trials': 1,
    'stats': {'n_completed_trials': 1},
  }
  trial_result = {
    'id': '23456781-2345-6781-2345-678123456781',
    'task_name': 'task-one',
    'trial_name': 'trial-one',
    'trial_uri': 'trial-one',
    'task_id': {'path': 'task-one'},
    'task_checksum': 'checksum',
    'config': {'task': {'path': 'task-one'}},
    'agent_info': {'name': 'bro:dev', 'version': FRAMEWORK_REVISION},
  }
  config = {
    'job_name': 'ephemeral-name',
    'jobs_dir': '/tmp/jobs',
    'n_attempts': 1,
    'agents': [
      {
        'import_path': 'bro.benchmark.harbor_agent:BroAgent',
        'model_name': 'openai/model',
        'kwargs': {'bro': 'dev', 'llm_credential': 'openai+benchmark'},
      }
    ],
    'datasets': [{'name': 'terminal-bench/terminal-bench-2-1', 'ref': 'sha256:data'}],
  }
  (job_directory / 'result.json').write_text(json.dumps(result))
  (job_directory / 'config.json').write_text(json.dumps(config))
  trial = job_directory / 'trial-one' / 'agent'
  trial.mkdir(parents=True)
  (trial.parent / 'result.json').write_text(json.dumps(trial_result))
  (trial / 'bro.log').write_text('finished\n')
  if uploaded:
    (job_directory / 'upload.json').write_text(
      json.dumps({'visibility': 'public', 'url': 'https://hub.example/jobs/1'})
    )
  return config


def _configure(monkeypatch, s3: FakeS3) -> None:
  monkeypatch.setattr(
    retention,
    'configured_retention',
    lambda: retention.RetentionConfig(bucket='benchmark-runs', region='us-east-1'),
  )
  monkeypatch.setattr(
    retention,
    'benchmark_bundle',
    lambda: SimpleNamespace(identity=FRAMEWORK_REVISION, source_commit=SOURCE_COMMIT),
  )
  monkeypatch.setattr(
    retention.boto3,
    'Session',
    lambda region_name: SimpleNamespace(client=lambda service: s3),
  )


def test_the_benchmark_distribution_contributes_the_retention_credential_kind():
  entries = importlib.metadata.entry_points(group='bro.credentials', name='benchmark_retention')

  [entry] = entries
  assert entry.value == 'bro.benchmark.credentials:RETENTION'
  assert entry.load() == {'sources': [{'file': 'benchmark_retention.json'}]}


def test_an_absent_retention_credential_skips_the_bucket(monkeypatch, tmp_path):
  monkeypatch.setattr(retention.credentials, 'try_get', lambda name: None)
  monkeypatch.setattr(
    retention.boto3,
    'Session',
    lambda **kwargs: pytest.fail('an absent credential must not construct an AWS client'),
  )

  assert retention.retain_job(tmp_path / 'job') is None


@pytest.mark.parametrize(
  'value',
  [
    'not json',
    '[]',
    '{}',
    '{"bucket": "runs", "region": "us-east-1", "extra": true}',
    '{"bucket": "", "region": "us-east-1"}',
    '{"bucket": "runs", "region": 1}',
  ],
)
def test_the_retention_credential_is_strict(monkeypatch, value):
  monkeypatch.setattr(retention.credentials, 'try_get', lambda name: value)

  with pytest.raises(ValueError, match='benchmark_retention'):
    retention.configured_retention()


def test_every_job_file_and_a_last_manifest_land_under_the_stable_key(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  config = _write_job(job_directory)
  s3 = FakeS3()
  _configure(monkeypatch, s3)

  retained = retention.retain_job(job_directory)

  assert retained is not None
  assert retained.bucket == 'benchmark-runs'
  assert retained.prefix.startswith('runs/2026-08-24/20260824T203650.123456Z/')
  assert f'/commit-{SOURCE_COMMIT}/' in retained.prefix
  assert f'/revision-{"a" * 64}/' in retained.prefix
  assert retained.prefix.endswith(f'/job-{JOB_ID}')
  uploaded_names = [key.removeprefix(f'{retained.prefix}/') for _, _, key in s3.uploads]
  assert uploaded_names == [
    'config.json',
    'result.json',
    'trial-one/agent/bro.log',
    'trial-one/result.json',
    'upload.json',
    retention.MANIFEST_FILENAME,
  ]
  assert all(bucket == retained.bucket for _, bucket, _ in s3.uploads)

  manifest = json.loads((job_directory / retention.MANIFEST_FILENAME).read_text())
  assert manifest['job_config'] == config
  assert manifest['bundle'] == {
    'source_commit': SOURCE_COMMIT,
    'framework_revision': FRAMEWORK_REVISION,
  }
  assert manifest['hub_url'] == 'https://hub.example/jobs/1'
  assert set(manifest['files']) == set(uploaded_names) - {retention.MANIFEST_FILENAME}
  assert manifest['files']['trial-one/agent/bro.log'] == {
    'sha256': 'sha256:' + '161069badc0cc23058b13d7c068e45205f4333aa15fd78db9d68f5c9f1ae8983',
    'size': 9,
  }


def test_output_routing_does_not_change_the_score_config_identity(monkeypatch, tmp_path):
  first = tmp_path / 'first'
  second = tmp_path / 'second'
  _write_job(first, uploaded=False)
  second_config = _write_job(second, uploaded=False)
  second_config['job_name'] = 'another-name'
  second_config['jobs_dir'] = '/elsewhere'
  (second / 'config.json').write_text(json.dumps(second_config))
  s3 = FakeS3()
  _configure(monkeypatch, s3)

  first_manifest, _ = retention._manifest(first)
  second_manifest, _ = retention._manifest(second)

  assert first_manifest['score_config_sha256'] == second_manifest['score_config_sha256']
  assert 'hub_url' not in first_manifest


def test_a_reported_revision_must_match_the_bundle(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory)
  monkeypatch.setattr(
    retention,
    'benchmark_bundle',
    lambda: SimpleNamespace(identity='sha256:' + 'c' * 64, source_commit=SOURCE_COMMIT),
  )

  with pytest.raises(ValueError, match='host bundle'):
    retention._manifest(job_directory)
