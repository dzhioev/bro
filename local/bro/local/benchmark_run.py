#!/usr/bin/env python
"""benchmark-run — score a chosen set of benchmark tasks and report what came back.

The whole operator loop around one job: narrow the pinned config to the tasks
under test, rebuild the bundle the trials run the framework from, block until
the job ends, and turn its run into a short report and a path.

Nothing is cleaned up behind it: the narrowed config stays in the workspace and
the run stays in the session's artifact store, so what the trials wrote is there
to read afterwards.
"""

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

import bro.base.args as base_args
from bro.artifact import ArtifactError, get_artifact
from bro.base import log
from bro.base.lulid import lulid
from bro.broker.job import OUTPUT_DIRECTORY
from bro.local.benchmark_job import (
  UPLOAD_VISIBILITIES,
  JobError,
  run_job,
  uploaded_job_url,
)
from bro.workspace.paths import project_root

__cli_name__ = 'benchmark-run'

# the pinned job config a run narrows unless another is named
DEFAULT_CONFIG = 'benchmark/bro/benchmark/terminal_bench_2_1.yaml'
# a run's own files, in the gitignored scratch the bundle already lives in
RUNS_DIRECTORY = Path('var') / 'benchmark' / 'runs'


def narrowed(
  config: dict[str, Any], tasks: Sequence[str], bros: Sequence[str], attempts: Optional[int]
) -> dict[str, Any]:
  """the job config restricted to what this run measures. An empty selection
  leaves that dimension as the config pinned it."""
  narrowed = dict(config)
  if len(tasks) > 0:
    narrowed['datasets'] = [
      {**dataset, 'task_names': list(tasks)} for dataset in config['datasets']
    ]
  if len(bros) > 0:
    agents = [agent for agent in config['agents'] if agent['kwargs']['bro'] in bros]
    if len(agents) == 0:
      raise ValueError(f'no agent in the config drives any of: {", ".join(bros)}')
    narrowed['agents'] = agents
  if attempts is not None:
    narrowed['n_attempts'] = attempts
  return narrowed


def build_bundle(tree: Path) -> None:
  """rebuild the relocatable bundle, so the trials run this checkout's framework
  rather than whatever an older build left in the tree."""
  log.info('building the bundle the trials run from')
  subprocess.run(
    ['uv', 'run', '--project', str(tree / 'benchmark'), 'benchmark-bundle'], cwd=tree, check=True
  )


def _elapsed(record: dict[str, Any]) -> str:
  span = datetime.fromisoformat(record['finished_at']) - datetime.fromisoformat(
    record['started_at']
  )
  seconds = int(span.total_seconds())
  return f'{seconds // 60}m{seconds % 60:02d}s'


def _pairs(values: dict[str, Any]) -> str:
  return ', '.join(f'{key} {value}' for key, value in sorted(values.items()))


def _trails(trial: Path) -> list[str]:
  """the ids of the trails the trial recorded, under the data home its run
  rooted at `agent/`."""
  return sorted(
    header.parent.name for header in (trial / 'agent' / 'ride').rglob('trails/*/header.json')
  )


def _trial_line(trial: Path) -> str:
  record = json.loads((trial / 'result.json').read_text())
  if record['exception_info'] is None:
    outcome = _pairs(record['verifier_result']['rewards'])
  else:
    outcome = f'failed: {json.dumps(record["exception_info"], sort_keys=True)}'
  trails = _trails(trial)
  found = f'trail {", ".join(trails)}' if len(trails) > 0 else 'no trail'
  return f'  {trial.name}: {outcome}, {_elapsed(record)}, {found}'


def report(jobs: Path) -> list[str]:
  """the short account of a finished run: what each agent scored, what the job
  spent, and every trial with its reward and the trail it recorded."""
  results = sorted(jobs.glob('*/result.json'))
  if len(results) != 1:
    raise JobError(f'{jobs} holds {len(results)} job results, expected one')
  job = results[0].parent
  run = json.loads(results[0].read_text())
  stats = run['stats']
  lines = [f'job {job.name}, {_elapsed(run)}']
  for name, evaluated in sorted(stats['evals'].items()):
    scored = [f'trials {evaluated["n_trials"]}', f'errors {evaluated["n_errors"]}']
    scored += [_pairs(metric) for metric in evaluated['metrics']]
    if len(evaluated['pass_at_k']) > 0:
      scored.append(f'pass@k {_pairs(evaluated["pass_at_k"])}')
    lines.append(f'  {name}: {", ".join(scored)}')
  lines.append(
    f'  {stats["n_input_tokens"]} input tokens '
    f'({stats["n_cache_tokens"]} cached), {stats["n_output_tokens"]} output'
  )
  lines += [_trial_line(record.parent) for record in sorted(job.glob('*/result.json'))]
  return lines


def _resolved(ref: str) -> Optional[Path]:
  try:
    return Path(get_artifact(ref))
  except ArtifactError as error:
    log.warning('the run is artifact %s, which did not resolve: %s', ref, error)
    return None


def _run(
  task: list[str],
  config: str,
  bro: list[str],
  attempts: Optional[int],
  timeout: Optional[float],
  upload: str,
  keep_bundle: bool,
) -> int:
  tree = project_root()
  source = tree / config
  if not source.is_file():
    log.error('no job config at %s', source)
    return 1
  try:
    selected = narrowed(yaml.safe_load(source.read_text()), task, bro, attempts)
  except ValueError as error:
    log.error('%s', error)
    return 1
  runs = tree / RUNS_DIRECTORY
  runs.mkdir(parents=True, exist_ok=True)
  narrowed_config = runs / f'{lulid()}.json'
  narrowed_config.write_text(json.dumps(selected, indent=2) + '\n')
  log.info('running %s', narrowed_config.relative_to(tree))

  if not keep_bundle:
    build_bundle(tree)
  try:
    ref = run_job(str(narrowed_config.relative_to(tree)), timeout, upload)
  except JobError as error:
    log.error('%s', error)
    run = _resolved(error.ref) if error.ref is not None else None
    if run is not None:
      print(f'results {run / OUTPUT_DIRECTORY}')
    return 1

  run = Path(get_artifact(ref))
  # printed before the report is rendered, so a run this cannot summarize is
  # still a run the operator can open
  print(f'results {run / OUTPUT_DIRECTORY}')
  print(f'config  {narrowed_config}')
  url = uploaded_job_url(run)
  if url is not None:
    print(f'upload  {url}')
  for line in report(run / OUTPUT_DIRECTORY):
    print(line)
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog=__cli_name__,
    description='score a set of benchmark tasks from a managed session: the host '
    'runs harbor with its own docker access, and the finished run stays in the '
    'session artifact store to be read afterwards',
  )
  parser.add_argument(
    'task', nargs='*', help='task-name globs to run (default: every task the dataset holds)'
  )
  parser.add_argument(
    '-c',
    '--config',
    default=DEFAULT_CONFIG,
    help=f'job config to narrow, relative to the workspace root (default: {DEFAULT_CONFIG})',
  )
  parser.add_argument(
    '--bro',
    action='append',
    default=[],
    metavar='NAME',
    help='keep only the config agents driving this bro (repeatable)',
  )
  parser.add_argument('--attempts', type=int, metavar='N', help="override the config's n_attempts")
  parser.add_argument(
    '--timeout', type=float, metavar='SECONDS', help='seconds before the host kills the job'
  )
  parser.add_argument(
    '--upload',
    choices=UPLOAD_VISIBILITIES,
    default='none',
    help='Harbor Hub visibility, or none to skip upload (default: none)',
  )
  parser.add_argument(
    '--keep-bundle',
    action='store_true',
    help='run the bundle already in var/benchmark instead of rebuilding it',
  )
  return _run(**parser.parse(argv))
