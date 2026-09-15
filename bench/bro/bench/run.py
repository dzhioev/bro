#!/usr/bin/env python
"""benchmark-run — score a run composed from committed presets and report what came back.

The whole operator loop around one job: compose the presets into the job
config, rebuild the bundle the trials run the framework from, run the job,
and turn its result into a short report. Inside a managed session the job goes
through the session broker and comes back as an artifact; outside one it runs
as a host process into a jobs directory. `--retain` then copies the finished
job to retention storage with the credentials this process holds.

Nothing is cleaned up behind it: the composed config stays in the workspace and
the run stays where it landed, so what the trials wrote is there to read
afterwards.
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import bro.base.args as base_args
from bro.artifact import ArtifactError, get_artifact
from bro.base import log, spawn
from bro.base.lulid import lulid
from bro.bench.job import JobError, run_job
from bro.bench.presets import PresetError, compose
from bro.broker.job import OUTPUT_DIRECTORY, TERM_GRACE
from bro.launch.broker_environment import CHANNEL_ENV, UPSTREAM_ENV
from bro.workspace.git import git_out

__cli_name__ = 'benchmark-run'

# a run's own files, in the gitignored scratch the bundle already lives in
RUNS_DIRECTORY = Path('var') / 'benchmark' / 'runs'
JOBS_DIRECTORY = Path('jobs')


def _benchmark_command(tree: Path, *arguments: str) -> list[str]:
  """a command of the benchmark project, run in its own environment."""
  return ['uv', 'run', '--project', str(tree / 'benchmark'), *arguments]


def build_bundle(tree: Path) -> None:
  """rebuild the relocatable bundle, so the trials run this checkout's framework
  rather than whatever an older build left in the tree."""
  log.info('building the bundle the trials run from')
  spawn.run(_benchmark_command(tree, 'benchmark', 'bundle'), cwd=tree, check=True)


def run_host_job(
  tree: Path, config: Path, jobs: Path, job_name: str, timeout: Optional[float]
) -> Path:
  """run the job as a host process, the way the broker's benchmark kind runs it,
  answering with its job directory. Past `timeout` the whole process group goes,
  Harbor and its trials included, after a grace to end on SIGTERM."""
  command = _benchmark_command(
    tree, 'bro.benchmark.job', '-c', str(config), '--jobs-dir', str(jobs), '--job-name', job_name
  )
  try:
    spawn.run(command, cwd=tree, check=True, timeout=timeout, grace=TERM_GRACE)
  except subprocess.TimeoutExpired:
    raise JobError(f'no result within {timeout:.0f}s; the job was killed') from None
  except subprocess.CalledProcessError as error:
    raise JobError(f'benchmark job failed with exit code {error.returncode}') from None
  return jobs / job_name


def retain_run(tree: Path, source: str) -> str:
  """copy the finished run to retention storage through `benchmark retain`,
  answering with the retained location it prints."""
  completed = spawn.run(
    _benchmark_command(tree, 'benchmark', 'retain', source),
    cwd=tree,
    check=True,
    stdout=subprocess.PIPE,
    text=True,
  )
  return completed.stdout.strip()


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


def one_job(jobs: Path) -> Path:
  """the job directory a collected run holds, exactly one."""
  results = sorted(jobs.glob('*/result.json'))
  if len(results) != 1:
    raise JobError(f'{jobs} holds {len(results)} job results, expected one')
  return results[0].parent


def report(job: Path) -> list[str]:
  """the short account of a finished job: what each agent scored, what the job
  spent, and every trial with its reward and the trail it recorded."""
  run = json.loads((job / 'result.json').read_text())
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


def _checkout_root() -> Path:
  return Path(git_out('rev-parse', '--show-toplevel')).resolve()


def _in_session() -> bool:
  """whether a session broker is there to run the job: its channel, or the
  mark a failed proxy launch leaves behind."""
  return CHANNEL_ENV in os.environ or UPSTREAM_ENV in os.environ


def _through_broker(config: str, timeout: Optional[float]) -> tuple[Path, str]:
  """the job started through the session broker: its job directory, and the
  artifact ref retention resolves it from."""
  try:
    ref = run_job(config, timeout)
  except JobError as error:
    run = _resolved(error.ref) if error.ref is not None else None
    if run is not None:
      print(f'results {run / OUTPUT_DIRECTORY}  artifact {error.ref}')
    raise
  run = Path(get_artifact(ref))
  # printed before the report is rendered, so a run this cannot summarize is
  # still a run the operator can open
  print(f'results {run / OUTPUT_DIRECTORY}  artifact {ref}')
  return one_job(run / OUTPUT_DIRECTORY), ref


def _on_host(tree: Path, config: Path, jobs: Path, timeout: Optional[float]) -> tuple[Path, str]:
  """the job run as a host process: its job directory, which is also what
  retention resolves it from."""
  job = jobs / config.stem
  try:
    run_host_job(tree, config, jobs, config.stem, timeout)
  except JobError:
    if job.is_dir():
      print(f'results {job}')
    raise
  print(f'results {job}')
  return job, str(job)


def _run(
  task: list[str],
  agents: str,
  settings: str,
  tasks: Optional[str],
  dataset: Optional[str],
  timeout: Optional[float],
  keep_bundle: bool,
  jobs_dir: Optional[str],
  retain: bool,
) -> int:
  tree = _checkout_root()
  in_session = _in_session()
  if in_session and jobs_dir is not None:
    log.error("--jobs-dir places a host process's job; a session's job lands in the artifact store")
    return 1
  try:
    config = compose(
      tree, agents=agents, settings=settings, tasks=tasks, dataset=dataset, task_names=task
    )
  except PresetError as error:
    log.error('%s', error)
    return 1
  runs = tree / RUNS_DIRECTORY
  runs.mkdir(parents=True, exist_ok=True)
  composed = runs / f'{lulid()}.json'
  composed.write_text(json.dumps(config, indent=2) + '\n')
  log.info('running %s', composed.relative_to(tree))

  if not keep_bundle:
    build_bundle(tree)
  try:
    if in_session:
      job, source = _through_broker(composed.relative_to(tree).as_posix(), timeout)
    else:
      jobs = tree / JOBS_DIRECTORY if jobs_dir is None else Path(jobs_dir).resolve()
      job, source = _on_host(tree, composed, jobs, timeout)
  except JobError as error:
    log.error('%s', error)
    return 1

  print(f'config  {composed}')
  for line in report(job):
    print(line)
  if retain:
    try:
      print(f'retained {retain_run(tree, source)}')
    except subprocess.CalledProcessError as error:
      log.error(
        'retention failed with exit code %s; the run stays at %s, and `benchmark retain %s` '
        'finishes it',
        error.returncode,
        job,
        source,
      )
      return 1
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog=__cli_name__,
    description='score a benchmark run composed from the presets under benchmark/: in a '
    'managed session the host runs harbor with its own docker access and the finished '
    "run stays in the ride's artifact store; outside one harbor runs right here into a "
    'jobs directory',
  )
  parser.add_argument(
    'task',
    nargs='*',
    help='with --dataset: the tasks to run, bare or Harbor-qualified (default: the whole dataset)',
  )
  parser.add_argument(
    '--agents', required=True, metavar='NAME', help='agent preset, benchmark/agents/NAME.json'
  )
  parser.add_argument(
    '--settings', required=True, metavar='NAME', help='run settings, benchmark/settings/NAME.json'
  )
  selection = parser.add_mutually_exclusive_group(required=True)
  selection.add_argument('--tasks', metavar='NAME', help='task set, benchmark/tasks/NAME.json')
  selection.add_argument(
    '--dataset',
    metavar='NAME',
    help='dataset pin, benchmark/datasets/NAME.json, for an ad hoc task selection',
  )
  parser.add_argument(
    '--timeout', type=float, metavar='SECONDS', help='seconds before the job is killed'
  )
  parser.add_argument(
    '--keep-bundle',
    action='store_true',
    help='run the bundle already in var/benchmark instead of rebuilding it',
  )
  parser.add_argument(
    '--jobs-dir',
    metavar='DIRECTORY',
    help=f'where a host process leaves the job (default: {JOBS_DIRECTORY}/ in the checkout); '
    "a session's job lands in the artifact store",
  )
  parser.add_argument(
    '--retain',
    action='store_true',
    help='copy the finished run to retention storage with `benchmark retain`',
  )
  return _run(**parser.parse(argv))
