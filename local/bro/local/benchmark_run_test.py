import json

import pytest

from bro.local.benchmark_job import JobError
from bro.local.benchmark_run import main, narrowed, report

CONFIG = {
  'datasets': [{'name': 'terminal-bench/terminal-bench-2-1', 'ref': '6'}],
  'agents': [
    {'model_name': 'openai/model', 'kwargs': {'bro': 'dev', 'llm_credential': 'openai+benchmark'}},
    {
      'model_name': 'openai/model',
      'kwargs': {'bro': 'terminal', 'llm_credential': 'openai+benchmark'},
    },
  ],
  'n_attempts': 1,
}

JOB_RESULT = {
  'started_at': '2026-08-24T20:36:54.446642',
  'finished_at': '2026-08-24T20:42:46.280062',
  'stats': {
    'evals': {
      'bro:terminal__terminal-bench/terminal-bench-2-1': {
        'n_trials': 1,
        'n_errors': 0,
        'metrics': [{'mean': 1.0}],
        'pass_at_k': {},
      }
    },
    'n_input_tokens': 183973,
    'n_cache_tokens': 158632,
    'n_output_tokens': 13407,
  },
}

TRIAL_RESULT = {
  'started_at': '2026-08-24T20:36:55.026541Z',
  'finished_at': '2026-08-24T20:42:46.276576Z',
  'exception_info': None,
  'verifier_result': {'rewards': {'reward': 1.0}},
}

TRIAL = 'adaptive-rejection-sampler__q8e2woY'
TRAIL = '01m0tqvd37-br85bbm9-7hpya8cq'


class TestNarrowed:
  def test_tasks_reach_every_dataset(self):
    selected = narrowed(CONFIG, ['a', 'b'], [], None)

    assert [dataset['task_names'] for dataset in selected['datasets']] == [['a', 'b']]
    assert selected['datasets'][0]['ref'] == '6'

  def test_an_empty_selection_leaves_the_config_alone(self):
    assert narrowed(CONFIG, [], [], None) == CONFIG

  def test_bros_select_the_agents_that_drive_them(self):
    selected = narrowed(CONFIG, [], ['terminal'], None)

    assert [agent['kwargs']['bro'] for agent in selected['agents']] == ['terminal']

  def test_a_bro_no_agent_drives_is_refused(self):
    with pytest.raises(ValueError, match='nobody'):
      narrowed(CONFIG, [], ['nobody'], None)

  def test_attempts_override_the_config(self):
    assert narrowed(CONFIG, [], [], 3)['n_attempts'] == 3

  def test_the_source_config_is_never_mutated(self):
    narrowed(CONFIG, ['a'], ['terminal'], 3)

    assert 'task_names' not in CONFIG['datasets'][0]
    assert len(CONFIG['agents']) == 2
    assert CONFIG['n_attempts'] == 1


@pytest.fixture
def jobs(tmp_path):
  """a finished job directory, laid out the way harbor leaves one: the run's own
  result beside a trial directory holding its result and its collected trail."""
  trial = tmp_path / '2026-08-24__20-36-50' / TRIAL
  trails = trial / 'agent' / 'ride' / 'trails' / 'trails' / TRAIL
  trails.mkdir(parents=True)
  (trails / 'header.json').write_text('{}')
  (trial / 'result.json').write_text(json.dumps(TRIAL_RESULT))
  (trial.parent / 'result.json').write_text(json.dumps(JOB_RESULT))
  return tmp_path


class TestReport:
  def test_it_names_the_job_the_score_the_spend_and_every_trial(self, jobs):
    lines = report(jobs)

    assert lines[0] == 'job 2026-08-24__20-36-50, 5m51s'
    assert lines[1] == (
      '  bro:terminal__terminal-bench/terminal-bench-2-1: trials 1, errors 0, mean 1.0'
    )
    assert lines[2] == '  183973 input tokens (158632 cached), 13407 output'
    assert lines[3] == f'  {TRIAL}: reward 1.0, 5m51s, trail {TRAIL}'

  def test_pass_at_k_is_reported_where_the_run_computed_one(self, jobs):
    result = jobs / '2026-08-24__20-36-50' / 'result.json'
    scored = json.loads(result.read_text())
    evaluated = scored['stats']['evals']['bro:terminal__terminal-bench/terminal-bench-2-1']
    evaluated['pass_at_k'] = {'1': 0.5}
    result.write_text(json.dumps(scored))

    assert report(jobs)[1].endswith('pass@k 1 0.5')

  def test_a_trial_that_recorded_no_trail_says_so(self, jobs):
    (
      jobs / '2026-08-24__20-36-50' / TRIAL / 'agent/ride/trails/trails' / TRAIL / 'header.json'
    ).unlink()

    assert report(jobs)[3].endswith('no trail')

  def test_a_trial_that_raised_reports_the_failure_instead_of_a_reward(self, jobs):
    result = jobs / '2026-08-24__20-36-50' / TRIAL / 'result.json'
    result.write_text(json.dumps({**TRIAL_RESULT, 'exception_info': {'name': 'ApiRateLimitError'}}))

    assert 'failed: {"name": "ApiRateLimitError"}' in report(jobs)[3]

  def test_a_directory_holding_no_single_run_is_refused(self, tmp_path):
    with pytest.raises(JobError, match='0 job results'):
      report(tmp_path)


def test_an_absent_config_fails_before_anything_runs(tmp_path, monkeypatch, caplog):
  monkeypatch.setattr('bro.local.benchmark_run.project_root', lambda: tmp_path)

  assert main(['benchmark-run', '-c', 'benchmark/nothing.yaml']) == 1
  assert any('no job config' in record.getMessage() for record in caplog.records)
  assert not (tmp_path / 'var').exists()


def test_upload_visibility_reaches_the_host_job_and_its_link_is_reported(
  tmp_path, monkeypatch, capsys
):
  config = tmp_path / 'job.yaml'
  config.write_text(json.dumps(CONFIG))
  artifact = tmp_path / 'artifact'
  upload = artifact / 'output' / 'job' / 'upload.json'
  upload.parent.mkdir(parents=True)
  upload.write_text(
    json.dumps({'visibility': 'public', 'url': 'https://hub.harborframework.com/jobs/1'})
  )
  captured = {}

  def run_job(config, timeout, visibility):
    captured.update(config=config, timeout=timeout, visibility=visibility)
    return 'sha256:' + 'a' * 64

  monkeypatch.setattr('bro.local.benchmark_run.project_root', lambda: tmp_path)
  monkeypatch.setattr('bro.local.benchmark_run.run_job', run_job)
  monkeypatch.setattr('bro.local.benchmark_run.get_artifact', lambda ref: str(artifact))
  monkeypatch.setattr('bro.local.benchmark_run.report', lambda jobs: [])

  assert main(['benchmark-run', '-c', 'job.yaml', '--upload', 'public', '--keep-bundle']) == 0
  assert captured['visibility'] == 'public'
  assert 'upload  https://hub.harborframework.com/jobs/1' in capsys.readouterr().out
