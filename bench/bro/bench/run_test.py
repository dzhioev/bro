import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from bro.base.liveness_test_helper import Liveness
from bro.bench import run
from bro.bench.job import JobError
from bro.bench.presets import AGENTS, DATASETS, PRESETS_KEY, SETTINGS, TASKS
from bro.bench.run import main, one_job, report
from bro.launch.broker_environment import CHANNEL_ENV, UPSTREAM_ENV

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
ARTIFACT_REF = 'sha256:' + 'a' * 64
RUN = ['benchmark-run', '--agents', 'one', '--settings', 'quick', '--keep-bundle']


def _write_job(job: Path) -> Path:
  """a finished job directory, laid out the way harbor leaves one: the run's own
  result beside a trial directory holding its result and its collected trail."""
  trial = job / TRIAL
  trails = trial / 'agent' / 'ride' / 'trails' / 'trails' / TRAIL
  trails.mkdir(parents=True)
  (trails / 'header.json').write_text('{}')
  (trial / 'result.json').write_text(json.dumps(TRIAL_RESULT))
  (job / 'result.json').write_text(json.dumps(JOB_RESULT))
  return job


@pytest.fixture
def job(tmp_path):
  return _write_job(tmp_path / '2026-08-24__20-36-50')


@pytest.fixture
def tree(tmp_path, monkeypatch):
  """a checkout holding the presets a run composes, outside any session."""
  presets = {
    (AGENTS, 'one'): {'agents': [{'kwargs': {'bro': 'terminal', 'llm_credential': 'openai'}}]},
    (SETTINGS, 'quick'): {'n_attempts': 1},
    (DATASETS, 'pinned'): {
      'name': 'org/dataset',
      'ref': 'sha256:' + 'd' * 64,
      'task_namespace': 'org',
    },
    (TASKS, 'few'): {'dataset': 'pinned', 'tasks': ['alpha']},
  }
  for (kind, name), value in presets.items():
    path = tmp_path / 'benchmark' / kind / f'{name}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
  monkeypatch.setattr('bro.bench.run._checkout_root', lambda: tmp_path)
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  monkeypatch.delenv(UPSTREAM_ENV, raising=False)
  return tmp_path


def _composed(tree: Path) -> Path:
  [config] = (tree / 'var' / 'benchmark' / 'runs').glob('*.json')
  return config


class TestReport:
  def test_it_names_the_job_the_score_the_spend_and_every_trial(self, job):
    lines = report(job)

    assert lines[0] == 'job 2026-08-24__20-36-50, 5m51s'
    assert lines[1] == (
      '  bro:terminal__terminal-bench/terminal-bench-2-1: trials 1, errors 0, mean 1.0'
    )
    assert lines[2] == '  183973 input tokens (158632 cached), 13407 output'
    assert lines[3] == f'  {TRIAL}: reward 1.0, 5m51s, trail {TRAIL}'

  def test_pass_at_k_is_reported_where_the_run_computed_one(self, job):
    result = job / 'result.json'
    scored = json.loads(result.read_text())
    evaluated = scored['stats']['evals']['bro:terminal__terminal-bench/terminal-bench-2-1']
    evaluated['pass_at_k'] = {'1': 0.5}
    result.write_text(json.dumps(scored))

    assert report(job)[1].endswith('pass@k 1 0.5')

  def test_a_trial_that_recorded_no_trail_says_so(self, job):
    (job / TRIAL / 'agent/ride/trails/trails' / TRAIL / 'header.json').unlink()

    assert report(job)[3].endswith('no trail')

  def test_a_trial_that_raised_reports_the_failure_instead_of_a_reward(self, job):
    result = job / TRIAL / 'result.json'
    result.write_text(json.dumps({**TRIAL_RESULT, 'exception_info': {'name': 'ApiRateLimitError'}}))

    assert 'failed: {"name": "ApiRateLimitError"}' in report(job)[3]

  def test_a_collected_run_holds_exactly_one_job(self, job, tmp_path):
    assert one_job(tmp_path) == job

    with pytest.raises(JobError, match='0 job results'):
      one_job(tmp_path / 'nothing')


def test_the_checkout_root_stays_in_a_linked_worktree(tmp_path, monkeypatch):
  repository = tmp_path / 'repository'
  worktree = tmp_path / 'worktree'
  subprocess.run(['git', 'init', '-q', str(repository)], check=True)
  subprocess.run(
    ['git', '-C', str(repository), 'config', 'user.email', 'test@example.com'], check=True
  )
  subprocess.run(['git', '-C', str(repository), 'config', 'user.name', 'Test'], check=True)
  (repository / 'tracked').write_text('content\n')
  subprocess.run(['git', '-C', str(repository), 'add', 'tracked'], check=True)
  subprocess.run(['git', '-C', str(repository), 'commit', '-qm', 'initial'], check=True)
  subprocess.run(
    ['git', '-C', str(repository), 'worktree', 'add', '-q', '-b', 'linked', str(worktree)],
    check=True,
  )
  monkeypatch.chdir(worktree)

  assert run._checkout_root() == worktree.resolve()


def test_a_missing_preset_fails_before_anything_runs(tree, caplog):
  assert main([*RUN, '--tasks', 'none']) == 1
  assert any("no tasks preset 'none'" in record.getMessage() for record in caplog.records)
  assert not (tree / 'var').exists()


def test_a_session_run_starts_the_composed_config_over_the_broker(tree, monkeypatch, capsys):
  monkeypatch.setenv(CHANNEL_ENV, 'tcp://127.0.0.1:1')
  artifact = tree / 'artifact'
  job = _write_job(artifact / 'output' / 'job')
  captured = {}

  def run_job(config, timeout):
    captured.update(config=config, timeout=timeout)
    return ARTIFACT_REF

  monkeypatch.setattr('bro.bench.run.run_job', run_job)
  monkeypatch.setattr('bro.bench.run.get_artifact', lambda artifact_ref: str(artifact))

  assert main([*RUN, '--tasks', 'few', '--timeout', '60']) == 0
  composed = _composed(tree)
  assert captured == {'config': composed.relative_to(tree).as_posix(), 'timeout': 60}
  config = json.loads(composed.read_text())
  assert config[PRESETS_KEY] == {'agents': 'one', 'settings': 'quick', 'tasks': 'few'}
  assert config['datasets'][0]['task_names'] == ['org/alpha']
  out = capsys.readouterr().out
  assert f'results {artifact / "output"}  artifact {ARTIFACT_REF}' in out
  assert f'config  {composed}' in out
  assert report(job)[0] in out


def test_a_session_run_names_the_run_a_failed_job_left(tree, monkeypatch, capsys, caplog):
  monkeypatch.setenv(CHANNEL_ENV, 'tcp://127.0.0.1:1')
  artifact = tree / 'artifact'
  artifact.mkdir()

  def run_job(config, timeout):
    raise JobError('benchmark job failed (exit)', ref=ARTIFACT_REF)

  monkeypatch.setattr('bro.bench.run.run_job', run_job)
  monkeypatch.setattr('bro.bench.run.get_artifact', lambda artifact_ref: str(artifact))

  assert main([*RUN, '--tasks', 'few']) == 1
  assert f'results {artifact / "output"}  artifact {ARTIFACT_REF}' in capsys.readouterr().out
  assert 'benchmark job failed (exit)' in caplog.text


def test_jobs_dir_is_refused_in_a_session(tree, monkeypatch, caplog):
  monkeypatch.setenv(CHANNEL_ENV, 'tcp://127.0.0.1:1')

  assert main([*RUN, '--tasks', 'few', '--jobs-dir', 'elsewhere']) == 1
  assert '--jobs-dir' in caplog.text
  assert not (tree / 'var').exists()


class TestHostRun:
  @pytest.fixture
  def commands(self, tree, monkeypatch):
    """the benchmark project's commands a host run spawns, each finishing the way
    its real one does: the job runner leaves a finished job, retention prints
    where it put the run."""
    spawned = []

    def spawn(command, cwd, check, **kwargs):
      assert cwd == tree and check is True
      spawned.append(command)
      if command[4] == 'bro.benchmark.job':
        _write_job(Path(command[8]) / command[10])
        return subprocess.CompletedProcess(command, 0)
      assert command[4:6] == ['benchmark', 'retain']
      return subprocess.CompletedProcess(command, 0, stdout='s3://bucket/runs/2026-08-24/id/\n')

    monkeypatch.setattr(run.spawn, 'run', spawn)
    return spawned

  def test_it_drives_the_job_runner_into_the_jobs_directory(self, tree, commands, capsys):
    assert main([*RUN, '--dataset', 'pinned', 'alpha', 'org/beta']) == 0
    composed = _composed(tree)
    job = tree / 'jobs' / composed.stem
    assert commands == [
      [
        'uv',
        'run',
        '--project',
        str(tree / 'benchmark'),
        'bro.benchmark.job',
        '-c',
        str(composed),
        '--jobs-dir',
        str(tree / 'jobs'),
        '--job-name',
        composed.stem,
      ]
    ]
    config = json.loads(composed.read_text())
    assert config[PRESETS_KEY] == {'agents': 'one', 'settings': 'quick', 'tasks': None}
    assert config['datasets'][0]['task_names'] == ['org/alpha', 'org/beta']
    out = capsys.readouterr().out
    assert f'results {job}' in out
    assert report(job)[0] in out

  def test_jobs_dir_places_the_job(self, tree, commands, tmp_path):
    elsewhere = tmp_path / 'elsewhere'

    assert main([*RUN, '--tasks', 'few', '--jobs-dir', str(elsewhere)]) == 0
    assert commands[0][8] == str(elsewhere)
    assert (elsewhere / _composed(tree).stem / 'result.json').is_file()

  def test_retain_finishes_with_the_retention_verb(self, tree, commands, capsys):
    assert main([*RUN, '--tasks', 'few', '--retain']) == 0
    job = tree / 'jobs' / _composed(tree).stem
    assert commands[1][4:] == ['benchmark', 'retain', str(job)]
    assert 'retained s3://bucket/runs/2026-08-24/id/\n' in capsys.readouterr().out

  def test_a_failed_retention_fails_the_run_and_keeps_the_job(
    self, tree, commands, monkeypatch, caplog
  ):
    spawn = run.spawn.run

    def failing_retention(command, **kwargs):
      if command[4] == 'benchmark':
        raise subprocess.CalledProcessError(1, command)
      return spawn(command, **kwargs)

    monkeypatch.setattr(run.spawn, 'run', failing_retention)

    assert main([*RUN, '--tasks', 'few', '--retain']) == 1
    job = tree / 'jobs' / _composed(tree).stem
    assert (job / 'result.json').is_file()
    assert f'`benchmark retain {job}` finishes it' in caplog.text

  def test_a_failed_job_names_the_directory_it_left(self, tree, monkeypatch, capsys, caplog):
    def fail(command, cwd, check, **kwargs):
      _write_job(Path(command[8]) / command[10])
      raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(run.spawn, 'run', fail)

    assert main([*RUN, '--tasks', 'few']) == 1
    assert f'results {tree / "jobs" / _composed(tree).stem}' in capsys.readouterr().out
    assert 'exit code 2' in caplog.text

  def test_a_timed_out_job_takes_its_whole_process_tree_along(
    self, tree, tmp_path, monkeypatch, caplog
  ):
    # what stands in for the job runner is a process tree whose leaf shrugs off
    # SIGTERM, so only a kill of the whole group ends it
    with contextlib.closing(Liveness(tmp_path / 'liveness')) as grandchild:
      monkeypatch.setattr(
        run,
        '_benchmark_command',
        lambda tree, *arguments: [
          'bash',
          '-c',
          f'(trap "" TERM; {grandchild.holding("sleep 60")}) | cat',
        ],
      )
      monkeypatch.setattr(run, 'TERM_GRACE', 0.5)

      assert main([*RUN, '--tasks', 'few', '--timeout', '1']) == 1
      grandchild.assert_reaped()
    assert 'no result within 1s; the job was killed' in caplog.text
