import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from bro.benchmark import compare

FRAMEWORK_REVISION = 'sha256:' + 'a' * 64
SCORE_CONFIG_SHA256 = 'sha256:' + 'b' * 64
ROSTER_SHA256 = 'sha256:' + 'c' * 64
JOB_ID = '12345678-1234-5678-1234-567812345678'


class FakeS3:
  def __init__(self, objects):
    self.objects = dict(objects)
    self.puts = []

  def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
    assert Bucket == 'benchmark-runs'
    assert ContinuationToken is None
    return {
      'Contents': [{'Key': key} for key in sorted(self.objects) if key.startswith(Prefix)],
      'IsTruncated': False,
    }

  def get_object(self, Bucket, Key):
    assert Bucket == 'benchmark-runs'
    if Key not in self.objects:
      raise ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'missing'}}, 'GetObject')
    return {'Body': io.BytesIO(self.objects[Key])}

  def put_object(self, Bucket, Key, Body):
    assert Bucket == 'benchmark-runs'
    self.objects[Key] = Body
    self.puts.append((Key, Body))


def _job_result():
  now = datetime(2026, 8, 25, tzinfo=UTC).isoformat()
  return {
    'id': JOB_ID,
    'started_at': now,
    'finished_at': now,
    'n_total_trials': 4,
    'stats': {'n_completed_trials': 4},
  }


def _trial_result(
  trial_id,
  task_name,
  reward,
  *,
  agent='bro:terminal',
  model='gpt-5.6-terra',
  provider='openai',
):
  return {
    'id': trial_id,
    'task_name': task_name,
    'trial_name': f'{task_name}-{trial_id[-4:]}',
    'trial_uri': f'{task_name}-{trial_id[-4:]}',
    'task_id': {'path': task_name},
    'task_checksum': 'checksum',
    'config': {'task': {'path': task_name}},
    'agent_info': {
      'name': agent,
      'version': FRAMEWORK_REVISION,
      'model_info': {'name': model, 'provider': provider},
    },
    'verifier_result': {'rewards': {'reward': reward}},
  }


def _write_local_job(path: Path):
  path.mkdir()
  (path / 'result.json').write_text(json.dumps(_job_result()))
  trials = [
    _trial_result('23456781-2345-6781-2345-678123456781', 'task-a', 1),
    _trial_result('23456781-2345-6781-2345-678123456782', 'task-a', 0),
    _trial_result('23456781-2345-6781-2345-678123456783', 'task-b', 0),
    _trial_result(
      '23456781-2345-6781-2345-678123456784',
      'task-a',
      1,
      agent='bro:dev',
    ),
  ]
  for result in trials:
    trial = path / result['trial_name']
    trial.mkdir()
    (trial / 'result.json').write_text(json.dumps(result))


def _submission():
  return {
    'source_jobs': [f'https://hub.harborframework.com/jobs/{JOB_ID}'],
    'source_filter': {
      'agent': 'claude-code',
      'agent_version': '2.1.205',
      'model_name': 'anthropic/claude-opus-4-8',
      'reasoning_effort': 'high',
    },
    'metadata': {'agent_display': {'label': 'Claude Code'}},
    'disqualified_trials': [
      {
        'trial_id': '33456781-2345-6781-2345-678123456782',
        'reason': 'reward_hacking',
      }
    ],
    'trials': [
      '33456781-2345-6781-2345-678123456781',
      '33456781-2345-6781-2345-678123456782',
      '33456781-2345-6781-2345-678123456783',
    ],
  }


def _reference_rows():
  base = {
    'source': 'terminal-bench/terminal-bench-2-1',
    'agent_name': 'claude-code',
    'agent_version': '2.1.205',
    'model_provider': 'anthropic',
    'model_name': 'claude-opus-4-8',
    'error_type': None,
  }
  return [
    base
    | {
      'id': '33456781-2345-6781-2345-678123456781',
      'task_name': 'task-a',
      'reward': 1,
    },
    base
    | {
      'id': '33456781-2345-6781-2345-678123456782',
      'task_name': 'task-b',
      'reward': 1,
    },
    base
    | {
      'id': '33456781-2345-6781-2345-678123456783',
      'task_name': 'task-c',
      'reward': None,
      'error_type': 'AgentError',
    },
    base
    | {
      'id': '33456781-2345-6781-2345-678123456799',
      'task_name': 'task-a',
      'reward': 0,
      'agent_name': 'another-agent',
    },
  ]


def test_local_report_selects_one_agent_marks_deltas_and_caches_reference(
  tmp_path, monkeypatch, capsys
):
  job_directory = tmp_path / 'job'
  _write_local_job(job_directory)
  source = tmp_path / 'submission.json'
  source.write_text(json.dumps(_submission()))
  calls = []

  async def fetch(job_ids):
    calls.append(job_ids)
    return _reference_rows()

  monkeypatch.setattr(compare, '_fetch_hub_trial_rows', fetch)

  arguments = [
    'bro.benchmark.compare',
    str(job_directory),
    '--agent',
    'bro:terminal',
    '--reference',
    str(source),
  ]
  assert compare.main(arguments) == 0
  output = capsys.readouterr().out

  assert calls == [[JOB_ID]]
  assert 'ours: bro:terminal / openai/gpt-5.6-terra' in output
  assert 'reference: Claude Code / anthropic/claude-opus-4-8' in output
  assert '* task-a' in output
  assert '  task-b' in output
  assert 'task-c' not in output
  assert '2 tasks; ours 3 trials / mean 0.250' in output
  assert 'reference 2 matching trials / mean 0.500' in output
  assert '1 task from reference outside this run omitted' in output
  cache_files = list((tmp_path / compare.REFERENCE_DIRECTORY).glob('*.json'))
  assert len(cache_files) == 1

  monkeypatch.setattr(
    compare,
    '_fetch_hub_trial_rows',
    lambda job_ids: pytest.fail('the second report must read the cached reference'),
  )
  assert compare.main(arguments) == 0


def test_a_local_job_must_contain_every_recorded_trial(tmp_path):
  job_directory = tmp_path / 'job'
  _write_local_job(job_directory)
  next(job_directory.glob('*/result.json')).unlink()

  with pytest.raises(compare.ComparisonError, match='records 4 total trials but contains 3'):
    compare.load_run(str(job_directory))


def test_a_multi_agent_run_requires_a_selector(tmp_path, monkeypatch, caplog):
  job_directory = tmp_path / 'job'
  _write_local_job(job_directory)
  monkeypatch.setattr(
    compare,
    'load_reference',
    lambda *args, **kwargs: pytest.fail('selection must fail before the reference is loaded'),
  )

  assert compare.main(['bro.benchmark.compare', str(job_directory)]) == 1

  assert 'multiple agent/model groups' in caplog.text
  assert 'bro:dev / openai/gpt-5.6-terra' in caplog.text
  assert 'bro:terminal / openai/gpt-5.6-terra' in caplog.text


def _retained_objects(prefix, trial):
  trial_name = trial['trial_name']
  manifest = {
    'format': 2,
    'score_config_sha256': SCORE_CONFIG_SHA256,
    'roster_sha256': ROSTER_SHA256,
    'files': {
      'config.json': {'sha256': FRAMEWORK_REVISION, 'size': 1},
      'result.json': {'sha256': FRAMEWORK_REVISION, 'size': 1},
      f'{trial_name}/result.json': {'sha256': FRAMEWORK_REVISION, 'size': 1},
    },
  }
  return {
    f'{prefix}/{compare.MANIFEST_FILENAME}': json.dumps(manifest).encode(),
    f'{prefix}/{trial_name}/result.json': json.dumps(trial).encode(),
  }


def test_retained_runs_in_one_cohort_are_aggregated_and_share_an_s3_reference_cache(
  monkeypatch,
):
  first = _trial_result('23456781-2345-6781-2345-678123456781', 'task-a', 1)
  second = _trial_result('23456781-2345-6781-2345-678123456782', 'task-b', 0)
  objects = _retained_objects('runs/2026-08-24/first', first)
  objects.update(_retained_objects('runs/2026-08-25/second', second))
  s3 = FakeS3(objects)
  monkeypatch.setattr(
    compare.boto3,
    'Session',
    lambda region_name: SimpleNamespace(client=lambda service: s3),
  )

  run = compare.load_run('s3://benchmark-runs', region='us-east-1')

  assert run.run_count == 2
  assert run.cohort == (SCORE_CONFIG_SHA256, ROSTER_SHA256)
  assert [(trial.task_name, trial.reward) for trial in run.trials] == [
    ('task-a', 1),
    ('task-b', 0),
  ]
  assert isinstance(run.cache, compare.S3ReferenceCache)
  assert run.cache.read('not-there') is None
  run.cache.write('reference-id', b'cached\n')
  assert s3.puts == [('references/reference-id.json', b'cached\n')]


def test_retention_bucket_requires_an_unambiguous_cohort(monkeypatch):
  first = _trial_result('23456781-2345-6781-2345-678123456781', 'task-a', 1)
  objects = _retained_objects('runs/2026-08-24/first', first)
  other = _retained_objects('runs/2026-08-25/second', first)
  manifest_key = 'runs/2026-08-25/second/retention.json'
  manifest = json.loads(other[manifest_key])
  manifest['score_config_sha256'] = 'sha256:' + 'd' * 64
  other[manifest_key] = json.dumps(manifest).encode()
  objects.update(other)
  s3 = FakeS3(objects)
  monkeypatch.setattr(
    compare.boto3,
    'Session',
    lambda region_name: SimpleNamespace(client=lambda service: s3),
  )

  with pytest.raises(compare.ComparisonError, match='multiple cohorts'):
    compare.load_run('s3://benchmark-runs')

  selected = compare.load_run('s3://benchmark-runs', score_config_sha256=SCORE_CONFIG_SHA256)
  assert selected.run_count == 1


def test_materialized_reference_trials_and_disqualifications_define_the_reference():
  trials = compare._reference_trials(_submission(), _reference_rows())

  assert [(trial.task_name, trial.reward) for trial in trials] == [
    ('task-a', 1),
    ('task-b', 0),
    ('task-c', 0),
  ]


def test_reference_rejects_a_materialized_trial_missing_from_the_public_jobs():
  rows = _reference_rows()[:1]

  with pytest.raises(compare.ComparisonError, match='missing 2 materialized'):
    compare._reference_trials(_submission(), rows)


def test_missing_tasks_and_threshold_are_divergences():
  ours = (
    compare.TrialReward('same', 'ours', None, 0.6),
    compare.TrialReward('ours-only', 'ours', None, 1),
  )
  reference = (
    compare.TrialReward('same', 'reference', None, 0.5),
    compare.TrialReward('reference-only', 'reference', None, 0),
  )

  rows = compare.compare_rewards(ours, reference, divergence=0.1)

  assert [(row.task_name, row.diverges) for row in rows] == [
    ('ours-only', True),
    ('same', False),
  ]


def test_the_native_claude_code_reference_is_content_pinned():
  source = compare.REFERENCE_SOURCES[compare.DEFAULT_REFERENCE]

  assert '7131e4375048a0e408a8fb404b5f499d726b695b' in source
  assert source.endswith('anthropic-claude-opus-4-8-high-claude-code.json')
