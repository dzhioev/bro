#!/usr/bin/env python
"""Run Harbor and leave its completed job directory as the raw run."""

import subprocess
from pathlib import Path
from typing import Optional

import bro.base.args as base_args
from bro.base import log
from bro.base.lulid import lulid
from bro.benchmark.harbor_agent import benchmark_bundle

__cli_name__ = 'benchmark-harbor-job'

BUNDLE_MANIFEST = 'bundle.json'


def _job_directory(jobs_directory: Path, job_name: str) -> Path:
  if job_name in {'', '.', '..'} or Path(job_name).name != job_name:
    raise ValueError(f'job name must be one path component: {job_name!r}')
  return jobs_directory / job_name


def run_job(
  config: Path,
  jobs_directory: Path,
  job_name: Optional[str] = None,
  attempts: Optional[int] = None,
) -> Path:
  """Run Harbor, then add the trial bundle's provenance to its job directory."""
  if attempts is not None and attempts < 1:
    raise ValueError(f'attempt depth must be at least one attempt: {attempts}')
  resolved_jobs_directory = jobs_directory.resolve()
  selected_job_name = job_name if job_name is not None else lulid()
  job_directory = _job_directory(resolved_jobs_directory, selected_job_name)
  bundle_manifest = benchmark_bundle().manifest.read_bytes()
  command = [
    'harbor',
    'job',
    'start',
    '-c',
    str(config),
    '--jobs-dir',
    str(resolved_jobs_directory),
    '--job-name',
    selected_job_name,
  ]
  if attempts is not None:
    command += ['--n-attempts', str(attempts)]
  subprocess.run(command, check=True)
  with (job_directory / BUNDLE_MANIFEST).open('xb') as manifest_file:
    manifest_file.write(bundle_manifest)
  return job_directory


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog='bro.benchmark.job',
    description='run Harbor and add the trial bundle manifest to its raw job directory',
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
    '-k',
    '--n-attempts',
    type=int,
    help="override the config's attempt depth for this run",
  )
  args = parser.parse(argv)
  try:
    run_job(
      config=args['config'],
      jobs_directory=args['jobs_dir'],
      job_name=args['job_name'],
      attempts=args['n_attempts'],
    )
  except (OSError, subprocess.CalledProcessError, ValueError) as error:
    log.error('benchmark job failed: %s', error)
    return 1
  return None
