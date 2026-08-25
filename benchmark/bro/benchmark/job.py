#!/usr/bin/env python
"""Run a Harbor job and finish its host-side post-run pipeline."""

import json
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Optional

from harbor.constants import HARBOR_VIEWER_JOBS_URL
from harbor.models.job.result import JobResult

import bro.base.args as base_args
from bro.base import log
from bro.base.lulid import lulid
from bro.benchmark.retention import RetainedRun, RetentionError, retain_job
from bro.benchmark.trajectory import convert_job_trajectories

__cli_name__ = 'benchmark-harbor-job'

UPLOAD_RECORD = 'upload.json'


class UploadVisibility(StrEnum):
  NONE = 'none'
  PRIVATE = 'private'
  PUBLIC = 'public'


@dataclass(frozen=True)
class UploadResult:
  visibility: UploadVisibility
  url: str


@dataclass(frozen=True)
class PostRunResult:
  job_directory: Path
  trajectory_paths: tuple[Path, ...]
  upload: Optional[UploadResult]
  retained: Optional[RetainedRun]


def _upload_job(job_directory: Path, visibility: UploadVisibility) -> Optional[UploadResult]:
  if visibility == UploadVisibility.NONE:
    return None
  subprocess.run(
    ['harbor', 'upload', str(job_directory), f'--{visibility.value}'],
    check=True,
  )
  job = JobResult.model_validate_json((job_directory / 'result.json').read_text())
  upload = UploadResult(
    visibility=visibility,
    url=f'{HARBOR_VIEWER_JOBS_URL}/{job.id}',
  )
  (job_directory / UPLOAD_RECORD).write_text(
    json.dumps({'visibility': upload.visibility, 'url': upload.url}, indent=2) + '\n'
  )
  return upload


def finish_job(
  job_directory: Path, visibility: UploadVisibility = UploadVisibility.NONE
) -> PostRunResult:
  """Run every ordered post-run operation against one finished Harbor job."""
  trajectory_paths = tuple(convert_job_trajectories(job_directory))
  upload = _upload_job(job_directory, visibility)
  retained = retain_job(job_directory)
  return PostRunResult(job_directory, trajectory_paths, upload, retained)


def _job_directory(jobs_directory: Path, job_name: str) -> Path:
  if job_name in {'', '.', '..'} or Path(job_name).name != job_name:
    raise ValueError(f'job name must be one path component: {job_name!r}')
  return jobs_directory / job_name


def run_job(
  config: Path,
  jobs_directory: Path,
  visibility: UploadVisibility = UploadVisibility.NONE,
  job_name: Optional[str] = None,
) -> PostRunResult:
  """Run Harbor, then finish the concrete job directory it produced."""
  resolved_jobs_directory = jobs_directory.resolve()
  selected_job_name = job_name if job_name is not None else lulid()
  job_directory = _job_directory(resolved_jobs_directory, selected_job_name)
  subprocess.run(
    [
      'harbor',
      'job',
      'start',
      '-c',
      str(config),
      '--jobs-dir',
      str(resolved_jobs_directory),
      '--job-name',
      selected_job_name,
    ],
    check=True,
  )
  return finish_job(job_directory, visibility)


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog='bro.benchmark.job',
    description='run a Harbor job, convert its finished trails to ATIF, optionally upload it '
    'to the Harbor Hub, and retain it when bucket storage is configured',
  )
  parser.add_argument('-c', '--config', type=Path, required=True, help='Harbor job config')
  parser.add_argument(
    '-o',
    '--jobs-dir',
    type=Path,
    default=Path('jobs'),
    help='directory to store job results (default: jobs)',
  )
  parser.add_argument('--job-name', help='job directory name (default: a generated id)')
  parser.add_argument(
    '--upload',
    choices=tuple(UploadVisibility),
    default=UploadVisibility.NONE,
    type=UploadVisibility,
    help='Harbor Hub visibility, or none to skip upload (default: none)',
  )
  args = parser.parse(argv)
  try:
    result = run_job(
      config=args['config'],
      jobs_directory=args['jobs_dir'],
      visibility=args['upload'],
      job_name=args['job_name'],
    )
  except (OSError, RetentionError, subprocess.CalledProcessError, ValueError) as error:
    log.error('benchmark job pipeline failed: %s', error)
    return 1
  log.info('converted %d trial trajectories', len(result.trajectory_paths))
  if result.upload is not None:
    print(f'uploaded {result.upload.url}')
  if result.retained is not None:
    print(f'retained {result.retained.url}')
  return 0
