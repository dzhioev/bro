#!/usr/bin/env python
"""Run Harbor and leave its completed job directory as the raw run."""

import json
import subprocess
from pathlib import Path
from typing import Any, Optional, get_args

from harbor.cli.config_sources import load_config_source
from harbor.models.job.config import JobConfig

import bro.base.args as base_args
from bro.base import log
from bro.base.lulid import lulid
from bro.bench.presets import PRESETS_KEY, presets_of
from bro.benchmark.harbor_agent import benchmark_bundle

__cli_name__ = 'benchmark-harbor-job'

BUNDLE_MANIFEST = 'bundle.json'
PRESETS_RECORD = 'presets.json'


def _job_directory(jobs_directory: Path, job_name: str) -> Path:
  if job_name in {'', '.', '..'} or Path(job_name).name != job_name:
    raise ValueError(f'job name must be one path component: {job_name!r}')
  return jobs_directory / job_name


def _models(annotation: Any) -> list[type]:
  """the pydantic models an annotation reads a nested object into."""
  if hasattr(annotation, 'model_fields'):
    return [annotation]
  return [model for argument in get_args(annotation) for model in _models(argument)]


def _dropped(value: dict[str, Any], model: type, path: str) -> list[str]:
  fields = model.model_fields
  by_alias = {field.alias: field for field in fields.values() if field.alias is not None}
  dropped: list[str] = []
  for key, item in value.items():
    field = fields.get(key, by_alias.get(key))
    if field is None:
      dropped.append(f'{path}{key}')
      continue
    nested = _models(field.annotation)
    if len(nested) != 1:
      continue
    if isinstance(item, dict):
      dropped += _dropped(item, nested[0], f'{path}{key}.')
    elif isinstance(item, list):
      for index, element in enumerate(item):
        if isinstance(element, dict):
          dropped += _dropped(element, nested[0], f'{path}{key}[{index}].')
  return dropped


def unknown_fields(config: dict[str, Any]) -> list[str]:
  """the config's fields Harbor reads no value from and would silently drop, the
  presets record excepted."""
  return _dropped(
    {key: value for key, value in config.items() if key != PRESETS_KEY}, JobConfig, ''
  )


def run_job(config: Path, jobs_directory: Path, job_name: Optional[str] = None) -> Path:
  """Run Harbor, then add the run's provenance to its job directory: the trial
  bundle's manifest and the presets the config was composed from."""
  content = load_config_source(config)
  presets = presets_of(content)
  dropped = unknown_fields(content)
  if len(dropped) > 0:
    raise ValueError(f'the job config carries fields Harbor would drop: {", ".join(dropped)}')
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
  subprocess.run(command, check=True)
  with (job_directory / BUNDLE_MANIFEST).open('xb') as manifest_file:
    manifest_file.write(bundle_manifest)
  with (job_directory / PRESETS_RECORD).open('x') as record_file:
    json.dump(presets.record(), record_file, indent=2)
    record_file.write('\n')
  return job_directory


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog='bro.benchmark.job',
    description='run Harbor and add the trial bundle manifest and the presets record '
    'to its raw job directory',
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
  args = parser.parse(argv)
  try:
    run_job(config=args['config'], jobs_directory=args['jobs_dir'], job_name=args['job_name'])
  except (OSError, subprocess.CalledProcessError, ValueError) as error:
    log.error('benchmark job failed: %s', error)
    return 1
  return None
