import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.benchmark import job


@pytest.fixture
def bundle(tmp_path):
  manifest = tmp_path / 'built-bundle.json'
  manifest.write_text('{"source_commit":"abc"}\n')
  return SimpleNamespace(manifest=manifest)


def test_run_job_starts_harbor_then_copies_the_bundle_manifest(tmp_path, monkeypatch, bundle):
  jobs_directory = tmp_path / 'jobs'
  job_directory = jobs_directory / 'chosen-name'
  events = []

  def run(command, check):
    assert check is True
    events.append(tuple(command))
    job_directory.mkdir(parents=True)

  monkeypatch.setattr(job, 'benchmark_bundle', lambda: bundle)
  monkeypatch.setattr(job.subprocess, 'run', run)

  result = job.run_job(tmp_path / 'config.yaml', jobs_directory, job_name='chosen-name')

  assert result == job_directory
  assert events == [
    (
      'harbor',
      'job',
      'start',
      '-c',
      str(tmp_path / 'config.yaml'),
      '--jobs-dir',
      str(jobs_directory.resolve()),
      '--job-name',
      'chosen-name',
    )
  ]
  assert (job_directory / job.BUNDLE_MANIFEST).read_bytes() == bundle.manifest.read_bytes()


def test_an_attempt_depth_overrides_the_config_for_one_run(tmp_path, monkeypatch, bundle):
  jobs_directory = tmp_path / 'jobs'
  commands = []

  def run(command, check):
    commands.append(tuple(command))
    (jobs_directory / 'chosen-name').mkdir(parents=True)

  monkeypatch.setattr(job, 'benchmark_bundle', lambda: bundle)
  monkeypatch.setattr(job.subprocess, 'run', run)

  job.run_job(tmp_path / 'config.yaml', jobs_directory, job_name='chosen-name', attempts=2)

  assert commands[0][-2:] == ('--n-attempts', '2')


def test_a_job_name_cannot_escape_the_jobs_directory(tmp_path, monkeypatch):
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('an invalid job name must fail before Harbor runs'),
  )

  with pytest.raises(ValueError, match='one path component'):
    job.run_job(Path('config.yaml'), tmp_path, job_name='../outside')


def test_an_empty_attempt_depth_fails_before_harbor_runs(tmp_path, monkeypatch):
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('an invalid attempt depth must fail before Harbor runs'),
  )

  with pytest.raises(ValueError, match='at least one attempt'):
    job.run_job(Path('config.yaml'), tmp_path, attempts=0)


def test_cli_runs_harbor_without_post_run_options(tmp_path, monkeypatch, capsys):
  captured = {}

  def run_job(config, jobs_directory, job_name, attempts):
    captured.update(
      config=config,
      jobs_directory=jobs_directory,
      job_name=job_name,
      attempts=attempts,
    )
    return tmp_path / 'job'

  monkeypatch.setattr(job, 'run_job', run_job)

  assert job.main(['bro.benchmark.job', '-c', 'config.yaml', '-o', str(tmp_path)]) is None
  assert captured == {
    'config': Path('config.yaml'),
    'jobs_directory': tmp_path,
    'job_name': None,
    'attempts': None,
  }
  assert capsys.readouterr().out == ''


def test_cli_refuses_the_removed_upload_option(capsys):
  with pytest.raises(SystemExit):
    job.main(['bro.benchmark.job', '-c', 'config.yaml', '--upload', 'private'])

  assert 'unrecognized arguments: --upload private' in capsys.readouterr().err


def test_harbor_failure_is_reported_without_post_processing(tmp_path, monkeypatch, bundle, caplog):
  monkeypatch.setattr(job, 'benchmark_bundle', lambda: bundle)

  def fail(command, check):
    raise subprocess.CalledProcessError(1, command)

  monkeypatch.setattr(job.subprocess, 'run', fail)

  assert job.main(['bro.benchmark.job', '-c', 'config.yaml', '-o', str(tmp_path)]) == 1
  assert 'benchmark job failed' in caplog.text
  assert not list(tmp_path.rglob(job.BUNDLE_MANIFEST))
