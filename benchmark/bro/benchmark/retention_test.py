import base64
import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from bro.benchmark import retention
from bro.benchmark.bundle import Bundle
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest
from bro.trails.record.spine import Recording

JOB_ID = UUID('12345678-1234-5678-1234-567812345678')
SOURCE_COMMIT = 'b' * 40
DATASET = 'terminal-bench/terminal-bench-2-1'
DATASET_REF = 'sha256:' + 'd' * 64
MODEL = 'gpt-5.6-terra'


class FakeS3:
  def __init__(self):
    self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
    self.puts: list[dict] = []

  def put_object(self, **kwargs):
    key = (kwargs['Bucket'], kwargs['Key'])
    body = kwargs['Body']
    content = body.read() if hasattr(body, 'read') else body
    checksum = base64.b64encode(hashlib.sha256(content).digest()).decode()
    assert kwargs['ChecksumAlgorithm'] == 'SHA256'
    assert kwargs['ChecksumSHA256'] == checksum
    assert kwargs['IfNoneMatch'] == '*'
    if key in self.objects:
      raise ClientError(
        {
          'Error': {'Code': 'PreconditionFailed', 'Message': 'exists'},
          'ResponseMetadata': {'HTTPStatusCode': 412},
        },
        'PutObject',
      )
    self.objects[key] = (content, checksum)
    self.puts.append(dict(kwargs))

  def head_object(self, **kwargs):
    assert kwargs['ChecksumMode'] == 'ENABLED'
    return {'ChecksumSHA256': self.objects[(kwargs['Bucket'], kwargs['Key'])][1]}

  def get_object(self, **kwargs):
    content, _ = self.objects[(kwargs['Bucket'], kwargs['Key'])]
    return {'Body': BytesIO(content)}

  def download_file(self, bucket, key, filename):
    content, _ = self.objects[(bucket, key)]
    Path(filename).write_bytes(content)


def _bundle_manifest(job_directory: Path) -> str:
  manifest = {
    'format': 2,
    'cpython': '3.12.14',
    'requirements': 'bro==0.1\n',
    'shim': '1' * 64,
    'source_commit': SOURCE_COMMIT,
    'target': ['linux', 'x86_64', 'glibc'],
    'wheels': {'bro.whl': '2' * 64},
  }
  (job_directory / 'bundle.json').write_text(json.dumps(manifest) + '\n')
  return Bundle(job_directory).identity


def _trial_config(task: str, bro: str = 'dev') -> dict:
  return {
    'task': {'path': task, 'source': DATASET},
    'trial_name': task,
    'agent': {
      'import_path': 'bro.benchmark.harbor_agent:BroAgent',
      'model_name': f'openai/{MODEL}:high',
      'kwargs': {'bro': bro, 'llm_credential': 'openai+benchmark'},
    },
  }


def _trial_result(
  task: str,
  trial: str,
  framework_revision: str,
  *,
  source: str = DATASET,
  error: bool = False,
) -> dict:
  timestamp = datetime(2026, 8, 24, 20, 37, tzinfo=UTC).isoformat()
  result = {
    'task_name': task,
    'trial_name': trial,
    'trial_uri': f'file:///jobs/{trial}',
    'task_id': {'path': task},
    'source': source,
    'task_checksum': 'checksum',
    'config': _trial_config(task),
    'agent_info': {'name': 'bro:dev', 'version': framework_revision},
    'started_at': timestamp,
    'finished_at': timestamp,
  }
  if error:
    result['exception_info'] = {
      'exception_type': 'RuntimeError',
      'exception_message': 'install failed',
      'exception_traceback': 'traceback',
      'occurred_at': timestamp,
    }
  else:
    result['agent_setup'] = {'started_at': timestamp, 'finished_at': timestamp}
    result['verifier_result'] = {'rewards': {'reward': 1, 'auxiliary': 0.5}}
  return result


def _record_trail(trial_directory: Path) -> str:
  store = LocalStore(trial_directory / retention.TRAILS_DIRECTORY)
  recording = Recording.create(
    store,
    BlazeRequest(
      harness='bro',
      bro='dev',
      version='test',
      native={'llm': {'type': 'openai', 'model': MODEL, 'effort': 'high'}},
      body={'records': [{'kind': 'system_prompt', 'body': 'prompt'}]},
      interactive=False,
      surface='benchmark',
    ),
  )
  recording.append(
    [
      {
        'kind': 'llm_call',
        'body': {
          'request': {'model': MODEL},
          'response': {
            'model': MODEL,
            'usage': {
              'input_tokens': 10,
              'input_tokens_details': {'cached_tokens': 3, 'cache_write_tokens': 2},
              'output_tokens': 4,
            },
            'output': [],
          },
        },
      }
    ]
  )
  recording.end('ok')
  store.close()
  return recording.trail_id


def _write_job(job_directory: Path, *, error_trial: bool = False) -> tuple[dict, str, str]:
  job_directory.mkdir(parents=True)
  framework_revision = _bundle_manifest(job_directory)
  started_at = datetime(2026, 8, 24, 20, 36, 50, 123456, tzinfo=UTC)
  result = {
    'id': str(JOB_ID),
    'started_at': started_at.isoformat(),
    'finished_at': started_at.isoformat(),
    'n_total_trials': 1 + int(error_trial),
    'stats': {'n_completed_trials': 1, 'n_errored_trials': int(error_trial), 'n_retries': 2},
  }
  config = {
    'job_name': 'ephemeral-name',
    'jobs_dir': '/tmp/jobs',
    'n_attempts': 1,
    'agents': [_trial_config('task-one')['agent']],
    'datasets': [{'name': DATASET, 'ref': DATASET_REF}],
  }
  (job_directory / 'result.json').write_text(json.dumps(result))
  (job_directory / 'config.json').write_text(json.dumps(config))

  trial = job_directory / 'trial-one'
  trial.mkdir()
  (trial / 'config.json').write_text(json.dumps(_trial_config('task-one')))
  (trial / 'result.json').write_text(
    json.dumps(_trial_result('task-one', 'trial-one', framework_revision))
  )
  (trial / 'agent').mkdir()
  (trial / 'agent' / 'bro.log').write_text('finished\n')
  root_trail_id = _record_trail(trial)

  if error_trial:
    failed = job_directory / 'trial-two'
    failed.mkdir()
    (failed / 'config.json').write_text(json.dumps(_trial_config('task-two')))
    (failed / 'result.json').write_text(
      json.dumps(
        _trial_result(
          'task-two',
          'trial-two',
          'not-the-bundle',
          error=True,
        )
      )
    )
  return config, framework_revision, root_trail_id


def _configure(monkeypatch, s3: FakeS3) -> None:
  monkeypatch.setattr(
    retention,
    'configured_retention',
    lambda: retention.RetentionConfig(bucket='benchmark-runs', region='us-east-1'),
  )
  monkeypatch.setattr(
    retention.boto3,
    'Session',
    lambda region_name: SimpleNamespace(client=lambda service: s3),
  )


def _retained_manifest(s3: FakeS3, retained: retention.RetainedRun) -> dict:
  content, _ = s3.objects[(retained.bucket, f'{retained.prefix}/{retention.MANIFEST_FILENAME}')]
  return json.loads(content)


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
  monkeypatch.setattr(retention.credentials, 'get', lambda name: value)

  with pytest.raises(ValueError, match='benchmark_retention'):
    retention.configured_retention()


def test_every_source_file_and_a_last_manifest_land_under_the_flat_key(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  config, framework_revision, root_trail_id = _write_job(job_directory)
  s3 = FakeS3()
  _configure(monkeypatch, s3)

  retained = retention.retain_job(job_directory)

  assert retained.bucket == 'benchmark-runs'
  assert retained.prefix == f'runs/2026-08-24/{JOB_ID}'
  keys = [put['Key'] for put in s3.puts]
  assert keys[-1] == f'{retained.prefix}/{retention.MANIFEST_FILENAME}'
  assert not (job_directory / retention.MANIFEST_FILENAME).exists()
  source_names = [
    path.relative_to(job_directory).as_posix()
    for path in sorted(job_directory.rglob('*'))
    if path.is_file()
  ]
  assert [key.removeprefix(f'{retained.prefix}/') for key in keys[:-1]] == source_names

  manifest = _retained_manifest(s3, retained)
  assert manifest['format'] == 3
  assert manifest['job'] == {
    'id': str(JOB_ID),
    'started_at': '2026-08-24T20:36:50.123456+00:00',
    'finished_at': '2026-08-24T20:36:50.123456+00:00',
    'n_retries': 2,
  }
  assert manifest['config'] == config
  assert manifest['dataset'] == {'name': DATASET, 'ref': DATASET_REF}
  assert manifest['bundle'] == {
    'source_commit': SOURCE_COMMIT,
    'framework_revision': framework_revision,
  }
  assert manifest['total_cost_usd'] == pytest.approx(0.0000636)
  assert manifest['trials'] == [
    {
      'trial': 'trial-one',
      'task': 'task-one',
      'harness': 'bro',
      'bro': 'dev',
      'model': f'openai/{MODEL}:high',
      'llm': {'type': 'openai', 'model': MODEL, 'effort': 'high'},
      'rewards': {'reward': 1, 'auxiliary': 0.5},
      'reward': 1,
      'error': None,
      'started_at': '2026-08-24T20:37:00+00:00',
      'finished_at': '2026-08-24T20:37:00+00:00',
      'root_trail_id': root_trail_id,
      'llm_calls': 1,
      'tokens': {'input': 5, 'cache_write': 2, 'cache_read': 3, 'output': 4},
      'cost_usd': pytest.approx(0.0000636),
    }
  ]
  assert manifest['pricing']['openai']['source'].startswith('https://')
  assert manifest['pricing']['openai']['rates'].keys() == {MODEL}
  assert len(manifest['pricing']['openai']['sha256']) == 64
  assert manifest['files'] == [
    {'path': name, 'sha256': row['sha256'], 'size': row['size']}
    for name, row in zip(source_names, manifest['files'], strict=True)
  ]


def test_a_trial_that_failed_before_install_keeps_an_error_row(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory, error_trial=True)
  s3 = FakeS3()
  _configure(monkeypatch, s3)

  retained = retention.retain_job(job_directory)

  manifest = _retained_manifest(s3, retained)
  error = manifest['trials'][1]
  assert error['error'] == 'RuntimeError: install failed'
  assert error['rewards'] is None
  assert error['reward'] is None
  assert error['root_trail_id'] is None
  assert error['llm'] is None
  assert error['llm_calls'] == 0
  assert error['tokens'] is None
  assert error['cost_usd'] is None
  assert manifest['total_cost_usd'] is None


def test_a_trial_that_reached_install_must_report_the_job_bundle(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory)
  result_path = job_directory / 'trial-one' / 'result.json'
  result = json.loads(result_path.read_text())
  result['agent_info']['version'] = 'wrong'
  result_path.write_text(json.dumps(result))
  _configure(monkeypatch, FakeS3())

  with pytest.raises(ValueError, match='reports bundle'):
    retention.retain_job(job_directory)


def test_a_job_spanning_multiple_dataset_sources_is_refused(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory, error_trial=True)
  result_path = job_directory / 'trial-two' / 'result.json'
  result = json.loads(result_path.read_text())
  result['source'] = 'another-dataset'
  result_path.write_text(json.dumps(result))
  _configure(monkeypatch, FakeS3())

  with pytest.raises(ValueError, match='spans 2 dataset sources'):
    retention.retain_job(job_directory)


def test_a_trail_header_must_agree_with_the_trial_config(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory)
  config_path = job_directory / 'trial-one' / 'config.json'
  config = json.loads(config_path.read_text())
  config['agent']['kwargs']['bro'] = 'terminal'
  config_path.write_text(json.dumps(config))
  _configure(monkeypatch, FakeS3())

  with pytest.raises(ValueError, match="reports bro 'dev', expected 'terminal'"):
    retention.retain_job(job_directory)


def test_a_partial_retry_skips_an_identical_object(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory)
  s3 = FakeS3()
  _configure(monkeypatch, s3)
  first_file = retention._inventory(job_directory)[0]
  key = ('benchmark-runs', f'runs/2026-08-24/{JOB_ID}/{first_file.relative_path}')
  s3.objects[key] = (first_file.path.read_bytes(), first_file.checksum)

  retained = retention.retain_job(job_directory)

  assert f'{retained.prefix}/{first_file.relative_path}' not in [put['Key'] for put in s3.puts]
  assert s3.puts[-1]['Key'].endswith('/retention.json')


def test_a_partial_retry_refuses_different_content(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory)
  s3 = FakeS3()
  _configure(monkeypatch, s3)
  first_file = retention._inventory(job_directory)[0]
  key = ('benchmark-runs', f'runs/2026-08-24/{JOB_ID}/{first_file.relative_path}')
  s3.objects[key] = (b'different', base64.b64encode(hashlib.sha256(b'different').digest()).decode())

  with pytest.raises(retention.RetentionError, match='different content'):
    retention.retain_job(job_directory)


def test_an_existing_marker_refuses_a_second_retain(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory)
  s3 = FakeS3()
  _configure(monkeypatch, s3)
  retention.retain_job(job_directory)

  with pytest.raises(retention.RetentionError, match='already retained'):
    retention.retain_job(job_directory)


def test_an_s3_service_failure_names_the_run_it_could_not_retain(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory)
  s3 = FakeS3()
  _configure(monkeypatch, s3)
  error = ClientError(
    {
      'Error': {'Code': 'AccessDenied', 'Message': 'denied'},
      'ResponseMetadata': {'HTTPStatusCode': 403},
    },
    'PutObject',
  )
  s3.put_object = lambda **kwargs: (_ for _ in ()).throw(error)

  with pytest.raises(retention.RetentionError, match='s3://benchmark-runs/runs/') as raised:
    retention.retain_job(job_directory)
  assert 'AccessDenied' in str(raised.value)


def test_an_aws_failure_names_the_run_it_could_not_retain(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  _write_job(job_directory)
  s3 = FakeS3()
  _configure(monkeypatch, s3)
  error = EndpointConnectionError(endpoint_url='https://s3.example')
  s3.put_object = lambda **kwargs: (_ for _ in ()).throw(error)

  with pytest.raises(retention.RetentionError, match='s3://benchmark-runs/runs/') as raised:
    retention.retain_job(job_directory)
  assert str(error) in str(raised.value)


def test_an_artifact_ref_resolves_the_one_job_under_output(monkeypatch, tmp_path):
  artifact_root = tmp_path / 'artifact'
  job_directory = artifact_root / 'output' / 'job'
  job_directory.mkdir(parents=True)
  (job_directory / 'result.json').write_text('{}')
  ref = 'sha256:' + 'a' * 64
  monkeypatch.setattr(retention.artifact, 'get_artifact', lambda value: str(artifact_root))

  assert retention.resolve_job(ref) == job_directory


def test_a_local_job_path_never_uses_the_artifact_channel(monkeypatch, tmp_path):
  job_directory = tmp_path / 'job'
  job_directory.mkdir()
  (job_directory / 'result.json').write_text('{}')
  monkeypatch.setattr(
    retention.artifact,
    'get_artifact',
    lambda value: pytest.fail('a local path must not resolve as an artifact'),
  )

  assert retention.resolve_job(str(job_directory)) == job_directory
