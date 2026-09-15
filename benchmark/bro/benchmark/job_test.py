import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.benchmark import job

PRESETS = {'agents': 'one', 'settings': 'quick', 'tasks': 'few'}


@pytest.fixture
def bundle(tmp_path):
  manifest = tmp_path / 'built-bundle.json'
  manifest.write_text('{"source_commit":"abc"}\n')
  return SimpleNamespace(manifest=manifest)


@pytest.fixture
def config(tmp_path):
  path = tmp_path / 'config.json'
  path.write_text(json.dumps({'presets': PRESETS, 'agents': [{'kwargs': {'bro': 'dev'}}]}))
  return path


def test_run_job_starts_harbor_then_records_the_bundle_and_presets(
  tmp_path, monkeypatch, bundle, config
):
  jobs_directory = tmp_path / 'jobs'
  job_directory = jobs_directory / 'chosen-name'
  events = []

  def run(command, check):
    assert check is True
    events.append(tuple(command))
    job_directory.mkdir(parents=True)

  monkeypatch.setattr(job, 'benchmark_bundle', lambda: bundle)
  monkeypatch.setattr(job.subprocess, 'run', run)

  result = job.run_job(config, jobs_directory, job_name='chosen-name')

  assert result == job_directory
  assert events == [
    (
      'harbor',
      'job',
      'start',
      '-c',
      str(config),
      '--jobs-dir',
      str(jobs_directory.resolve()),
      '--job-name',
      'chosen-name',
    )
  ]
  assert (job_directory / job.BUNDLE_MANIFEST).read_bytes() == bundle.manifest.read_bytes()
  assert json.loads((job_directory / job.PRESETS_RECORD).read_text()) == PRESETS


def test_a_config_without_a_presets_record_fails_before_harbor_runs(tmp_path, monkeypatch):
  config = tmp_path / 'config.json'
  config.write_text(json.dumps({'agents': [{'kwargs': {'bro': 'dev'}}]}))
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('a config without presets must fail before Harbor runs'),
  )

  with pytest.raises(ValueError, match="no 'presets' record"):
    job.run_job(config, tmp_path)


@pytest.mark.parametrize(
  ('content', 'dropped'),
  [
    ({'n_attempt': 99}, 'n_attempt'),
    ({'retry': {'max_retry': 2}}, 'retry.max_retry'),
    ({'agents': [{'kwargs': {'bro': 'dev'}, 'modle_name': 'x'}]}, 'agents[0].modle_name'),
    (
      {'datasets': [{'name': 'org/set', 'ref': 'sha256:d', 'task_name': ['a']}]},
      'datasets[0].task_name',
    ),
  ],
)
def test_a_field_harbor_would_drop_fails_before_harbor_runs(
  tmp_path, monkeypatch, content, dropped
):
  config = tmp_path / 'config.json'
  config.write_text(json.dumps({'presets': PRESETS, **content}))
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('a dropped field must fail before Harbor runs'),
  )

  with pytest.raises(ValueError, match=re.escape(f'fields Harbor would drop: {dropped}')):
    job.run_job(config, tmp_path)


def test_a_job_name_cannot_escape_the_jobs_directory(tmp_path, monkeypatch, config):
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('an invalid job name must fail before Harbor runs'),
  )

  with pytest.raises(ValueError, match='one path component'):
    job.run_job(config, tmp_path, job_name='../outside')


def test_cli_runs_harbor_without_post_run_options(tmp_path, monkeypatch, capsys):
  captured = {}

  def run_job(config, jobs_directory, job_name):
    captured.update(config=config, jobs_directory=jobs_directory, job_name=job_name)
    return tmp_path / 'job'

  monkeypatch.setattr(job, 'run_job', run_job)

  assert job.main(['bro.benchmark.job', '-c', 'config.yaml', '-o', str(tmp_path)]) is None
  assert captured == {'config': Path('config.yaml'), 'jobs_directory': tmp_path, 'job_name': None}
  assert capsys.readouterr().out == ''


def test_cli_refuses_the_removed_upload_option(capsys):
  with pytest.raises(SystemExit):
    job.main(['bro.benchmark.job', '-c', 'config.yaml', '--upload', 'private'])

  assert 'unrecognized arguments: --upload private' in capsys.readouterr().err


def test_harbor_failure_is_reported_without_post_processing(
  tmp_path, monkeypatch, bundle, config, caplog
):
  monkeypatch.setattr(job, 'benchmark_bundle', lambda: bundle)

  def fail(command, check):
    raise subprocess.CalledProcessError(1, command)

  monkeypatch.setattr(job.subprocess, 'run', fail)

  assert job.main(['bro.benchmark.job', '-c', str(config), '-o', str(tmp_path)]) == 1
  assert 'benchmark job failed' in caplog.text
  assert not list(tmp_path.rglob(job.BUNDLE_MANIFEST))
  assert not list(tmp_path.rglob(job.PRESETS_RECORD))
