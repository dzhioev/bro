import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from harbor.models.job.result import JobResult, JobStats

from bro.benchmark import job

JOB_ID = UUID('12345678-1234-5678-1234-567812345678')


def _write_job_result(job_directory: Path) -> None:
  job_directory.mkdir(parents=True)
  now = datetime.now(UTC)
  result = JobResult(
    id=JOB_ID,
    started_at=now,
    finished_at=now,
    n_total_trials=1,
    stats=JobStats(n_completed_trials=1),
  )
  (job_directory / 'result.json').write_text(result.model_dump_json())


@pytest.mark.parametrize('visibility', [job.UploadVisibility.PRIVATE, job.UploadVisibility.PUBLIC])
def test_run_job_converts_before_uploading_with_explicit_visibility(
  tmp_path, monkeypatch, visibility
):
  jobs_directory = tmp_path / 'jobs'
  job_directory = jobs_directory / 'chosen-name'
  trajectory = job_directory / 'trial' / 'agent' / 'trajectory.json'
  events = []

  def run(command, check):
    assert check is True
    events.append(tuple(command))
    if command[1:3] == ['job', 'start']:
      _write_job_result(job_directory)

  def convert(directory):
    assert directory == job_directory
    events.append(('convert', str(directory)))
    return [trajectory]

  monkeypatch.setattr(job.subprocess, 'run', run)
  monkeypatch.setattr(job, 'convert_job_trajectories', convert)
  monkeypatch.setattr(
    job,
    'retain_job',
    lambda directory: events.append(('retain', str(directory))),
  )

  result = job.run_job(tmp_path / 'config.yaml', jobs_directory, visibility, job_name='chosen-name')

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
    ),
    ('convert', str(job_directory)),
    ('harbor', 'upload', str(job_directory), f'--{visibility.value}'),
    ('retain', str(job_directory)),
  ]
  assert result.trajectory_paths == (trajectory,)
  assert result.upload is not None
  assert result.upload.visibility == visibility
  assert result.upload.url.endswith(str(JOB_ID))
  assert json.loads((job_directory / job.UPLOAD_RECORD).read_text()) == {
    'visibility': visibility.value,
    'url': result.upload.url,
  }


def test_no_upload_still_converts_and_retains_the_finished_job(tmp_path, monkeypatch):
  job_directory = tmp_path / 'job'
  converted = job_directory / 'trial/agent/trajectory.json'
  retained = job.RetainedRun('bucket', 'runs/job')
  monkeypatch.setattr(job, 'convert_job_trajectories', lambda directory: [converted])
  monkeypatch.setattr(job, 'retain_job', lambda directory: retained)
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('no upload command should run'),
  )

  result = job.finish_job(job_directory)

  assert result.trajectory_paths == (converted,)
  assert result.upload is None
  assert result.retained == retained
  assert not (job_directory / job.UPLOAD_RECORD).exists()


def test_a_conversion_failure_prevents_upload(tmp_path, monkeypatch):
  def fail_conversion(directory):
    raise ValueError(f'malformed store in {directory}')

  monkeypatch.setattr(job, 'convert_job_trajectories', fail_conversion)
  monkeypatch.setattr(
    job,
    'retain_job',
    lambda directory: pytest.fail('retention must not run after failed conversion'),
  )
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('upload must not run after failed conversion'),
  )

  with pytest.raises(ValueError, match='malformed store'):
    job.finish_job(tmp_path / 'job', job.UploadVisibility.PUBLIC)


def test_a_failed_upload_leaves_no_success_record(tmp_path, monkeypatch):
  job_directory = tmp_path / 'job'
  monkeypatch.setattr(job, 'convert_job_trajectories', lambda directory: [])
  monkeypatch.setattr(
    job,
    'retain_job',
    lambda directory: pytest.fail('retention must not run after failed upload'),
  )

  def fail_upload(command, check):
    raise subprocess.CalledProcessError(1, command)

  monkeypatch.setattr(job.subprocess, 'run', fail_upload)

  with pytest.raises(subprocess.CalledProcessError):
    job.finish_job(job_directory, job.UploadVisibility.PRIVATE)
  assert not (job_directory / job.UPLOAD_RECORD).exists()


def test_a_job_name_cannot_escape_the_jobs_directory(tmp_path, monkeypatch):
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('an invalid job name must fail before Harbor runs'),
  )

  with pytest.raises(ValueError, match='one path component'):
    job.run_job(Path('config.yaml'), tmp_path, job_name='../outside')


def test_cli_defaults_to_no_upload(tmp_path, monkeypatch, capsys):
  captured = {}

  def run_job(config, jobs_directory, visibility, job_name, attempts):
    captured.update(
      config=config,
      jobs_directory=jobs_directory,
      visibility=visibility,
      job_name=job_name,
      attempts=attempts,
    )
    return job.PostRunResult(tmp_path / 'job', (), None, None)

  monkeypatch.setattr(job, 'run_job', run_job)

  assert job.main(['bro.benchmark.job', '-c', 'config.yaml', '-o', str(tmp_path)]) == 0
  assert captured == {
    'config': Path('config.yaml'),
    'jobs_directory': tmp_path,
    'visibility': job.UploadVisibility.NONE,
    'job_name': None,
    'attempts': None,
  }
  assert capsys.readouterr().out == ''


def test_an_attempt_depth_overrides_the_config_for_one_run(tmp_path, monkeypatch):
  jobs_directory = tmp_path / 'jobs'
  commands = []

  def run(command, check):
    commands.append(tuple(command))
    _write_job_result(jobs_directory / 'chosen-name')

  monkeypatch.setattr(job.subprocess, 'run', run)
  monkeypatch.setattr(job, 'convert_job_trajectories', lambda directory: [])
  monkeypatch.setattr(job, 'retain_job', lambda directory: None)

  job.run_job(tmp_path / 'config.yaml', jobs_directory, job_name='chosen-name', attempts=2)

  assert commands[0][-2:] == ('--n-attempts', '2')


def test_an_empty_attempt_depth_fails_before_harbor_runs(tmp_path, monkeypatch):
  monkeypatch.setattr(
    job.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('an invalid attempt depth must fail before Harbor runs'),
  )

  with pytest.raises(ValueError, match='at least one attempt'):
    job.run_job(Path('config.yaml'), tmp_path, attempts=0)
