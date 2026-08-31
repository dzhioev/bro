#!/usr/bin/env python
import contextlib
import functools
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from bro.base.args import Parser
from bro.dev.affected_tests import (
  changed_paths,
  import_graph,
  module_name,
  module_names,
  reachable,
)
from bro.dev.packaging_policy import TEST_MODULE_SUFFIXES, distribution_roots

__cli_name__ = 'run-tests'

DIR = Path(__file__).resolve().parents[3]

TEST_MODULE_PATTERN = f'.*({"|".join(TEST_MODULE_SUFFIXES)})\\.py$'


# this checkout's one project outside the workspace, which the root metadata
# names nowhere, so every tool that walks the members is told about it here
BENCHMARK = 'benchmark'


@dataclass(frozen=True)
class Distribution:
  directory: str
  deptry_exclude: tuple[str, ...]
  deptry_known_first_party: tuple[str, ...]


DISTRIBUTIONS = [
  Distribution(
    directory='.',
    deptry_exclude=(
      TEST_MODULE_PATTERN,
      'conftest\\.py$',
      'bro/base/yesno\\.py$',
      'bro/setup/',
      '^dev/',
      '^local/',
      '^native/',
      '^oops/',
      '^ride/',
      f'^{BENCHMARK}/',
      '.venv/',
      '.claude/',
    ),
    # the root ships both packages; `bros` is a namespace another member also
    # contributes to, so nothing infers it from the directory alone
    deptry_known_first_party=('bro', 'bros'),
  ),
  Distribution(
    directory='dev',
    deptry_exclude=(TEST_MODULE_PATTERN,),
    deptry_known_first_party=('bro',),
  ),
  Distribution(
    directory='local',
    deptry_exclude=(TEST_MODULE_PATTERN,),
    deptry_known_first_party=('bro', 'bros'),
  ),
  Distribution(
    directory='native',
    deptry_exclude=(TEST_MODULE_PATTERN,),
    deptry_known_first_party=('bro',),
  ),
  Distribution(
    directory='oops',
    deptry_exclude=(TEST_MODULE_PATTERN,),
    deptry_known_first_party=('bro',),
  ),
  Distribution(
    directory='ride',
    deptry_exclude=(TEST_MODULE_PATTERN,),
    deptry_known_first_party=('bro',),
  ),
  Distribution(
    directory=BENCHMARK,
    deptry_exclude=(TEST_MODULE_PATTERN, '.venv/'),
    deptry_known_first_party=('bro',),
  ),
]

PYTEST_FILES = [
  'bro/base/args_test.py',
  'bro/base/condition_test.py',
  'bro/base/configs_test.py',
  'bro/base/credentials_test.py',
  'bro/base/git_url_test.py',
  'bro/base/host_config_test.py',
  'bro/base/log_test.py',
  'bro/base/lulid_test.py',
  'bro/base/name_map_test.py',
  'bro/base/spawn_test.py',
  'bro/base/suite_environment_test.py',
  'bro/base/template_test.py',
  'bro/base/text_window_test.py',
  'bro/base/time_util_test.py',
  'bro/brog/system_test.py',
  'bro/brog/github_test.py',
  'bro/brog/mcp_test.py',
  'bro/llm/cli_tool_test.py',
  'bro/llm/llms/claude_code_test.py',
  'bro/llm/llms/openai_test.py',
  'bro/llm/mcp_test.py',
  'bro/llm/providers_test.py',
  'bro/llm/mu_test.py',
  'bro/llm/observer_test.py',
  'bro/llm/tracker_test.py',
  'bro/mcp_test.py',
  'bro/harness/claude_test.py',
  'bro/bro_test.py',
  'bro/artifact_test.py',
  'bro/run_lifecycle_test.py',
  'native/bro/fork_test.py',
  'native/bro/native/llms/openai_test.py',
  'native/bro/native/runner_test.py',
  'bro/roster_test.py',
  'bro/spells_test.py',
  'bro/datasources/current_time_test.py',
  'bro/datasources/file_test.py',
  'bro/datasources/man_test.py',
  'bro/datasources/searchable_test.py',
  'bro/datasources/web_search_test.py',
  'bro/datasources/wikipedia_test.py',
  'bro/registry_test.py',
  'native/bro/run_test.py',
  'bro/packaging_test.py',
  'native/bro/native/distribution_test.py',
  'bro/trails/network_test.py',
  'bro/trails/contract_test.py',
  'bro/trails/model_test.py',
  'bro/trails/store_test.py',
  'bro/trails/local_test.py',
  'bro/trails/display/config_test.py',
  'bro/trails/display/core_test.py',
  'bro/trails/display/live_test.py',
  'bro/trails/display/recorded_test.py',
  'bro/trails/display/records_test.py',
  'bro/trails/display/terminal_test.py',
  'bro/trails/display/textual_test.py',
  'bro/trails/display/_reflow_test.py',
  'bro/trails/lineage_test.py',
  'bro/trails/claude_lineage_test.py',
  'bro/trails/rewind_test.py',
  'bro/trails/admin_test.py',
  'bro/trails/server/server_test.py',
  'bro/trails/server/dynamo_test.py',
  'bro/show_test.py',
  'bro/shell_test.py',
  'oops/bro/oops/assets_test.py',
  'oops/bro/oops/cdk/config_test.py',
  'oops/bro/oops/trails_deployment_test.py',
  'oops/bro/oops/deploy_lib_test.py',
  'oops/bro/oops/distribution_test.py',
  'oops/bro/oops/monitor_ecs_test.py',
  'oops/bro/oops/mcp_test.py',
  'bro/launch/hold_test.py',
  'bro/launch/llm_flags_test.py',
  'native/bro/launch/call_test.py',
  'native/bro/launch/resume_test.py',
  'native/bro/launch/run_test.py',
  'dev/bros/dev/dev_test.py',
  'dev/bros/dev/mcp_test.py',
  'dev/bros/dev/jobs_test.py',
  'dev/bros/eyebro/eyebro_test.py',
  'dev/bros/lead/lead_test.py',
  'dev/bros/terminal/terminal_test.py',
  'dev/bros/analyst/scripts/trails_usage_test.py',
  'bro/launch/broxy_test.py',
  'ride/ride/bro_test.py',
  'ride/ride/identity_test.py',
  'ride/ride/root_test.py',
  'ride/ride/clean_test.py',
  'ride/ride/repository_test.py',
  'ride/ride/runtime_bundle_test.py',
  'ride/ride/runtime_state_test.py',
  'ride/ride/scope_test.py',
  'ride/ride/trails_test.py',
  'ride/ride/kinds_test.py',
  'ride/ride/spawn_test.py',
  'ride/ride/peer_facts_test.py',
  'ride/ride/artifacts_test.py',
  'ride/ride/summon_control_test.py',
  'ride/ride/pending_summon_test.py',
  'ride/ride/claude/claude_argv_test.py',
  'ride/ride/claude/claude_config_test.py',
  'ride/ride/alias_test.py',
  'ride/ride/cli_test.py',
  'ride/ride/inner_test.py',
  'ride/ride/flags_test.py',
  'ride/ride/scope_report_test.py',
  'ride/ride/claude/assembly_test.py',
  'ride/ride/claude/mcp_test.py',
  'ride/ride/claude/runner_test.py',
  'ride/ride/claude/claude_auth_test.py',
  'ride/ride/claude/session_context_test.py',
  'ride/ride/claude/recorder_test.py',
  'ride/ride/session_test.py',
  'ride/ride/claude/system_prompt_test.py',
  'ride/ride/packaging_test.py',
  'bro/workspace/banner_test.py',
  'ride/ride/workspace/build_context_test.py',
  'ride/ride/workspace/containers_test.py',
  'ride/ride/workspace/docker_test.py',
  'bro/workspace/git_test.py',
  'ride/ride/workspace/model_test.py',
  'bro/workspace/paths_test.py',
  'bro/workspace/project_test.py',
  'bro/workspace/session_test.py',
  'ride/ride/workspace/spawn_test.py',
  'ride/ride/workspace/store_test.py',
  'ride/ride/workspace/worktrees_test.py',
  'bro/broker/brotocol_test.py',
  'bro/broker/spawn_test.py',
  'bro/broker/job_test.py',
  'bro/broker/runtime_test.py',
  'bro/broker/worker_test.py',
  'bro/broker/journal_test.py',
  'bro/broker/dispatcher_test.py',
  'bro/broker/client_test.py',
  'bro/broker/cli_test.py',
  'bro/broker/broxy_test.py',
  'bro/broker/transports/tcp_test.py',
  'bro/summon_test.py',
  'native/bro/trails/record/bro_test.py',
  'ride/ride/claude/trail_recorder_test.py',
  'bro/monitor/monitor_test.py',
  'bro/monitor/health_test.py',
  'bro/monitor/trail_pointer_test.py',
  'ride/ride/claude/statusline_test.py',
  'ride/ride/dive_in_test.py',
  'bro/extra/github/api_test.py',
  'bro/extra/github/app_test.py',
  'bro/extra/github/pulls_test.py',
  'dev/bro/extra/github/poll_pr_test.py',
  'dev/bro/extra/github/pr_state_test.py',
  'dev/bro/workflow/co_author_test.py',
  'dev/bro/workflow/commit_footer_test.py',
  'dev/bro/workflow/fold_branch_test.py',
  'dev/bro/workflow/land_pr_test.py',
  'bro/runtime/mcp_server_test.py',
  'bro/prompts/prompts_test.py',
  'ride/ride/setup/container/git_test.py',
  'bro/setup/docker_smoke_test_test.py',
  'bro/llm/usage_test.py',
  'local/bro/local/benchmark_job_test.py',
  'local/bro/local/benchmark_run_test.py',
  'local/bro/local/run_tests_test.py',
  'local/bro/local/shell_policy_test.py',
  'local/bro/local/markdown_policy_test.py',
  'local/bro/local/setup_test.py',
  'local/bro/local/packaging_policy_test.py',
  'local/bro/local/environment_policy_test.py',
  'dev/bro/dev/affected_tests_test.py',
  'dev/bro/dev/packaging_policy_test.py',
  'dev/bro/dev/distribution_test.py',
  'dev/bro/dev/sync_scripts_test.py',
  'local/bros/bro_dev/bro_dev_test.py',
  'local/bros/bro_eyebro/bro_eyebro_test.py',
  'dev/bro/dev/git_golc_test.py',
  'dev/bro/dev/usage_report_test.py',
  'dev/bro/dev/install_test.py',
  'dev/bro/dev/shell_policy_test.py',
]
# collected by one process rather than by every worker in the pool: an xdist
# worker collects the whole roster, not the share it runs, so a module importing
# `aws_cdk` starts a jsii Node kernel in each of them
SINGLE_PROCESS_PYTEST_FILES = ['oops/bro/oops/cdk/stacks_test.py']
# outside both rosters above: they drive the host docker daemon, which the
# suite's in-container leg has none of
DOCKER_PYTEST_FILE = 'ride/ride/workspace/launch_smoke_test.py'
BROKER_E2E_PYTEST_FILE = 'ride/ride/e2e_test.py'
# run from the benchmark project's own environment, the only one that can import
# it. The e2e modules stay out of every stage: two of them spend real tokens
BENCHMARK_PYTEST_FILES = [
  'bro/benchmark/bundle_test.py',
  'bro/benchmark/compare_test.py',
  'bro/benchmark/harbor_agent_test.py',
  'bro/benchmark/harbor_environment_test.py',
  'bro/benchmark/job_test.py',
  'bro/benchmark/pricing_test.py',
  'bro/benchmark/retention_test.py',
  'bro/benchmark/trajectory_test.py',
]


FAILURE_REPLAY_LINES = 40
DEFAULT_BASE = 'origin/master'

_recording: Optional[list[str]] = None


@contextlib.contextmanager
def _record() -> Iterator[list[str]]:
  """collect what a stage's commands write, so its failure can be replayed last."""
  global _recording
  lines: list[str] = []
  _recording = lines
  try:
    yield lines
  finally:
    _recording = None


def run(*args: str, extra_env: Optional[dict[str, str]] = None, cwd: Optional[Path] = None) -> None:
  env = None if extra_env is None else {**os.environ, **extra_env}
  with subprocess.Popen(
    args,
    cwd=DIR if cwd is None else cwd,
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  ) as process:
    assert process.stdout is not None
    for line in process.stdout:
      sys.stderr.write(line)
      if _recording is not None:
        _recording.append(line)
  if process.returncode:
    raise subprocess.CalledProcessError(process.returncode, args)


def node_env() -> dict[str, str]:
  return {'NODE_OPTIONS': '--max-old-space-size=4096'}


def lint_stage(distributions: Sequence[Distribution] = DISTRIBUTIONS) -> None:
  scope = (
    'every distribution'
    if len(distributions) == len(DISTRIBUTIONS)
    else f'{len(distributions)} of {len(DISTRIBUTIONS)} distributions'
  )
  print(f'sync-scripts, deptry: {scope}', file=sys.stderr)
  for distribution in distributions:
    directory = DIR / distribution.directory
    print(f'sync-scripts: verifying {directory} console-script metadata', file=sys.stderr)
    run(sys.executable, '-m', 'bro.dev.sync_scripts', '--check', '--project', str(directory))
    deptry_args = [sys.executable, '-m', 'deptry', '.']
    for pattern in distribution.deptry_exclude:
      deptry_args += ['-ee', pattern]
    for module in distribution.deptry_known_first_party:
      deptry_args += ['-kf', module]
    run(*deptry_args, cwd=directory)
  print('ruff: lint check', file=sys.stderr)
  run(sys.executable, '-m', 'ruff', 'check', '.')
  print('ruff: format check', file=sys.stderr)
  run(sys.executable, '-m', 'ruff', 'format', '--check', '.')


def types_stage() -> None:
  print('pyright: type check', file=sys.stderr)
  run(sys.executable, '-m', 'pyright', extra_env=node_env())


def unit_stage(
  roster: Sequence[str] = PYTEST_FILES,
  single_process_roster: Sequence[str] = SINGLE_PROCESS_PYTEST_FILES,
) -> None:
  selected = len(roster) + len(single_process_roster)
  total = len(PYTEST_FILES) + len(SINGLE_PROCESS_PYTEST_FILES)
  scope = 'whole roster' if selected == total else f'{selected} of {total} modules'
  print(f'pytest: unit suite ({scope})', file=sys.stderr)
  if len(roster) > 0:
    run(sys.executable, '-m', 'pytest', '-n', 'auto', *roster)
  if len(single_process_roster) > 0:
    print('pytest: unit suite (single-process modules)', file=sys.stderr)
    run(sys.executable, '-m', 'pytest', *single_process_roster)


@dataclass(frozen=True)
class Selection:
  """the gate work a change can reach: what each narrowed stage runs, and what is dropped whole."""

  roster: Sequence[str]
  single_process_roster: Sequence[str]
  distributions: Sequence[Distribution]
  dropped: frozenset[str]


def touched_distributions(changed: Sequence[str]) -> list[Distribution]:
  """the distributions whose own files a change lands in.

  Distribution directories nest, and both `sync-scripts` and deptry stop at a
  nested project's root, so the innermost directory holding a path covers it.
  """

  def covering(path: str) -> str:
    directories = [
      distribution.directory
      for distribution in DISTRIBUTIONS
      if distribution.directory == '.' or path.startswith(f'{distribution.directory}/')
    ]
    return max(directories, key=len)

  covered = {covering(path) for path in changed}
  return [distribution for distribution in DISTRIBUTIONS if distribution.directory in covered]


def select(base: str) -> Selection:
  """the work a change against `base` can reach, stage by stage."""
  source_roots = distribution_roots(DIR, (BENCHMARK,))
  changed = changed_paths(DIR, base)
  print(f'{len(changed)} paths changed against {base}', file=sys.stderr)
  seeds = {name for path in changed if (name := module_name(source_roots, DIR / path)) is not None}
  hit = reachable(import_graph(DIR, source_roots), seeds)
  benchmark_reached = (
    not hit.isdisjoint(module_names(DIR / BENCHMARK, source_roots).values())
    or any(path.startswith(f'{BENCHMARK}/') for path in changed)
    # the stage's `uv sync --locked` reads the metadata of every project the
    # benchmark project installs from a path source, so a change to any of them
    # can leave the lock committed beside it stale
    or any(path.endswith('pyproject.toml') for path in changed)
  )

  def reached(test: str) -> bool:
    return (
      not (DIR / f'{test.removesuffix("_test.py")}.py').exists()
      or module_name(source_roots, DIR / test) in hit
    )

  return Selection(
    roster=[test for test in PYTEST_FILES if reached(test)],
    single_process_roster=[test for test in SINGLE_PROCESS_PYTEST_FILES if reached(test)],
    distributions=touched_distributions(changed),
    dropped=frozenset() if benchmark_reached else frozenset({'benchmark'}),
  )


def benchmark_stage() -> None:
  directory = DIR / BENCHMARK
  environment = directory / '.venv'
  # naming the environment uv is about to sync keeps it from reporting the
  # workspace venv this gate runs from as one it is ignoring
  in_environment = {'VIRTUAL_ENV': str(environment)}
  print(f'benchmark: syncing {environment}', file=sys.stderr)
  run('uv', 'sync', '--locked', '--all-groups', cwd=directory, extra_env=in_environment)
  python = str(environment / 'bin' / 'python')
  print('benchmark: type check', file=sys.stderr)
  run(python, '-m', 'pyright', cwd=directory, extra_env={**in_environment, **node_env()})
  print('benchmark: unit suite', file=sys.stderr)
  run(python, '-m', 'pytest', *BENCHMARK_PYTEST_FILES, cwd=directory, extra_env=in_environment)


def docker_stage() -> None:
  print('smoke: container entrypoint', file=sys.stderr)
  run(str(DIR / 'ride' / 'ride' / 'setup' / 'container' / 'test_smoke.sh'))
  print('smoke: container launch path', file=sys.stderr)
  run(sys.executable, '-m', 'pytest', DOCKER_PYTEST_FILE)


def broker_e2e_stage() -> None:
  print('broker_e2e: broker-supervised container launch seam', file=sys.stderr)
  run(sys.executable, '-m', 'pytest', BROKER_E2E_PYTEST_FILE)


@dataclass(frozen=True)
class Stage:
  name: str
  run: Callable[[], None]
  host_only: bool = False


STAGES = [
  Stage('lint', lint_stage),
  Stage('types', types_stage),
  Stage('unit', unit_stage),
  Stage('benchmark', benchmark_stage),
  Stage('docker', docker_stage, host_only=True),
  Stage('broker_e2e', broker_e2e_stage, host_only=True),
]


def main(argv: list[str]) -> Optional[int]:
  names = [stage.name for stage in STAGES]
  parser = Parser(description='run the repository test gate')
  parser.add_argument(
    '--only',
    action='append',
    choices=names,
    metavar='STAGE',
    help=f'run only the named stage, repeatable ({", ".join(names)})',
  )
  parser.add_argument(
    '--skip',
    action='append',
    choices=names,
    metavar='STAGE',
    help='skip the named stage, repeatable',
  )
  parser.add_argument(
    '--changed',
    action='store_true',
    help='narrow the gate to the work the diff can reach',
  )
  parser.add_argument('--base', help=f'the ref --changed diffs against (default: {DEFAULT_BASE})')
  parser.add_exclusive_groups(['only'], ['skip'])
  args = parser.parse(argv)
  only = args['only']
  skip = args['skip'] if args['skip'] is not None else []
  if args['base'] is not None and not args['changed']:
    parser.error('--base names the ref --changed diffs against; pass --changed too')
  stages = STAGES
  dropped: frozenset[str] = frozenset()
  if args['changed']:
    selected = select(args['base'] or DEFAULT_BASE)
    narrowed: dict[str, Callable[[], None]] = {
      'lint': functools.partial(lint_stage, selected.distributions),
      'unit': functools.partial(unit_stage, selected.roster, selected.single_process_roster),
    }
    stages = [replace(stage, run=narrowed.get(stage.name, stage.run)) for stage in STAGES]
    dropped = selected.dropped
  in_container = Path('/.dockerenv').is_file()

  verdicts: list[tuple[str, str]] = []
  failures: list[tuple[str, list[str]]] = []
  for stage in stages:
    if only is not None and stage.name not in only:
      continue
    if stage.name in skip:
      print(f'skipping the {stage.name} stage (--skip)', file=sys.stderr)
      continue
    # unlike --skip, this narrowing is the gate's own deduction, so it owes a
    # verdict rather than going missing from the closing line
    if stage.name in dropped:
      print(
        f'skipping the {stage.name} stage (--changed: the diff reaches nothing it runs)',
        file=sys.stderr,
      )
      verdicts.append((stage.name, 'skipped'))
      continue
    if stage.host_only and in_container:
      if only is not None:
        parser.error(f'the {stage.name} stage drives the host docker daemon; run it on the host')
      print(f'skipping the {stage.name} stage (inside container; run on host)', file=sys.stderr)
      continue
    with _record() as recorded:
      try:
        stage.run()
        passed = True
      except subprocess.CalledProcessError:
        failures.append((stage.name, recorded[-FAILURE_REPLAY_LINES:]))
        passed = False
    verdicts.append((stage.name, 'ok' if passed else 'FAILED'))

  for name, replay in failures:
    print(f'\n=== {name} failed ===', file=sys.stderr)
    sys.stderr.writelines(replay)
  print(
    '\ngate: ' + ' | '.join(f'{name} {verdict}' for name, verdict in verdicts),
    file=sys.stderr,
  )
  return 1 if failures else None


if __name__ == '__main__':
  sys.exit(main(sys.argv))
