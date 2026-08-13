#!/usr/bin/env python
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base.args import Parser

DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Distribution:
  directory: str
  deptry_exclude: tuple[str, ...]
  deptry_known_first_party: tuple[str, ...]


DISTRIBUTIONS = [
  Distribution(
    directory='.',
    deptry_exclude=(
      '.*_test\\.py$',
      'conftest\\.py$',
      'bro/base/yesno\\.py$',
      'bro/base/log_test_helper\\.py$',
      'bro/setup/',
      'bro-dev/',
      'dev/',
      '.venv/',
      '.claude/',
    ),
    deptry_known_first_party=('bro',),
  ),
  Distribution(
    directory='bro-dev',
    deptry_exclude=('.*_test\\.py$',),
    deptry_known_first_party=('bro', 'bro_dev'),
  ),
  Distribution(
    directory='dev',
    deptry_exclude=(),
    deptry_known_first_party=('bro',),
  ),
]

PYTEST_FILES = [
  'bro/base/args_test.py',
  'bro/base/condition_test.py',
  'bro/base/configs_test.py',
  'bro/base/credentials_test.py',
  'bro/base/host_config_test.py',
  'bro/base/log_test.py',
  'bro/base/lulid_test.py',
  'bro/base/name_map_test.py',
  'bro/base/spawn_test.py',
  'bro/base/template_test.py',
  'bro/base/text_window_test.py',
  'bro/base/time_util_test.py',
  'bro/brog/system_test.py',
  'bro/brog/github_test.py',
  'bro/brog/mcp_test.py',
  'bro/llm/llms/chat_gpt_test.py',
  'bro/llm/mcp_test.py',
  'bro/llm/mu_test.py',
  'bro/llm/observer_test.py',
  'bro/llm/tracker_test.py',
  'bro/harness/claude_test.py',
  'bro/bro_test.py',
  'bro/channel_test.py',
  'bro/fork_test.py',
  'bro/roster_test.py',
  'bro/spells_test.py',
  'bro/datasources/current_time_test.py',
  'bro/datasources/file_test.py',
  'bro/datasources/searchable_test.py',
  'bro/datasources/web_search_test.py',
  'bro/datasources/wikipedia_test.py',
  'bro/registry_test.py',
  'bro/run_test.py',
  'bro/packaging_test.py',
  'bro/trails/client_test.py',
  'bro/trails/lineage_test.py',
  'bro/trails/rewind_test.py',
  'bro/trails/server/server_test.py',
  'bro/trails/server/storage_test.py',
  'bro/show_test.py',
  'bro/shell_test.py',
  'bro/launch/_cli_test.py',
  'bro/launch/bro_run_test.py',
  'bro/launch/identity_test.py',
  'bro/launch/root_test.py',
  'bro/launch/scope_test.py',
  'bro/launch/spawn_test.py',
  'bro/launch/summon_control_test.py',
  'bro/launch/_reflow_test.py',
  'bro/launch/call_test.py',
  'bro/launch/resume_test.py',
  'bro/launch/ask_test.py',
  'bro/bros/dev/mcp_test.py',
  'bro/bros/dev/jobs_test.py',
  'bro/cw/broxy_test.py',
  'bro/cw/claude_argv_test.py',
  'bro/cw/claude_config_test.py',
  'bro/cw/cli_test.py',
  'bro/cw/flags_test.py',
  'bro/cw/scope_report_test.py',
  'bro/cw/mcp_test.py',
  'bro/cw/runner_test.py',
  'bro/cw/claude_auth_test.py',
  'bro/cw/session_context_test.py',
  'bro/cw/recorder_test.py',
  'bro/cw/session_test.py',
  'bro/cw/system_prompt_test.py',
  'bro/workspace/banner_test.py',
  'bro/workspace/build_context_test.py',
  'bro/workspace/containers_test.py',
  'bro/workspace/docker_test.py',
  'bro/workspace/git_test.py',
  'bro/workspace/model_test.py',
  'bro/workspace/paths_test.py',
  'bro/workspace/project_test.py',
  'bro/workspace/session_test.py',
  'bro/workspace/spawn_test.py',
  'bro/workspace/store_test.py',
  'bro/workspace/worktrees_test.py',
  'bro/broker/brotocol_test.py',
  'bro/broker/runtime_test.py',
  'bro/broker/dispatcher_test.py',
  'bro/broker/client_test.py',
  'bro/broker/cli_test.py',
  'bro/broker/broxy_test.py',
  'bro/broker/transports/unix_test.py',
  'bro/summon_test.py',
  'bro/trails/record/bro_test.py',
  'bro/trails/record/claude_test.py',
  'bro/monitor/monitor_test.py',
  'bro/monitor/health_test.py',
  'bro/monitor/trail_pointer_test.py',
  'bro/cw/statusline_test.py',
  'bro/workflow/dive_in_test.py',
  'bro/extra/github/api_test.py',
  'bro/extra/github/app_test.py',
  'bro/extra/github/poll_pr_test.py',
  'bro/workflow/commit_footer_test.py',
  'bro/workflow/land_pr_test.py',
  'bro/runtime/mcp_server_test.py',
  'bro/prompts/prompts_test.py',
  'bro/setup/container/git_test.py',
  'bro/llm/usage_test.py',
  'shell_policy_test.py',
  'bro-dev/bro_dev/sync_scripts_test.py',
  'bro-dev/bro_dev/bro_test.py',
  'bro-dev/bro_dev/git_golc_test.py',
  'bro-dev/bro_dev/usage_report_test.py',
  'bro-dev/bro_dev/install_test.py',
  'bro-dev/bro_dev/shell_policy_test.py',
]
# outside the roster above: it drives the host docker daemon, which the suite's
# in-container leg has none of
DOCKER_PYTEST_FILE = 'bro/workspace/launch_smoke_test.py'


def run(*args: str, extra_env: Optional[dict[str, str]] = None, cwd: Optional[Path] = None) -> None:
  env = None if extra_env is None else {**os.environ, **extra_env}
  subprocess.run(args, check=True, cwd=DIR if cwd is None else cwd, env=env)


def node_env() -> dict[str, str]:
  return {'NODE_OPTIONS': '--max-old-space-size=4096'}


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='run the repository test gate')
  parser.add_argument('--no-docker', action='store_true', help='skip the docker smoke stages')
  args = parser.parse(argv)

  for distribution in DISTRIBUTIONS:
    directory = DIR / distribution.directory
    print(f'sync-scripts: verifying {directory} console-script metadata', file=sys.stderr)
    run(sys.executable, '-m', 'bro_dev.sync_scripts', '--check', '--project', str(directory))
    deptry_args = [sys.executable, '-m', 'deptry', '.']
    for pattern in distribution.deptry_exclude:
      deptry_args += ['-ee', pattern]
    for module in distribution.deptry_known_first_party:
      deptry_args += ['-kf', module]
    run(*deptry_args, cwd=directory)

  print('ruff: lint check', file=sys.stderr)
  run(sys.executable, '-m', 'ruff', 'check', '.')
  print('pyright: type check', file=sys.stderr)
  run(sys.executable, '-m', 'pyright', extra_env=node_env())
  print('pytest: unit suite', file=sys.stderr)
  run(sys.executable, '-m', 'pytest', *PYTEST_FILES)
  if args['no_docker'] is True:
    print('skipping the docker smoke stages (--no-docker)', file=sys.stderr)
  elif Path('/.dockerenv').is_file():
    print('skipping the docker smoke stages (inside container; run on host)', file=sys.stderr)
  else:
    print('smoke: container entrypoint', file=sys.stderr)
    run(str(DIR / 'bro' / 'setup' / 'container' / 'test_smoke.sh'))
    print('smoke: container launch path', file=sys.stderr)
    run(sys.executable, '-m', 'pytest', DOCKER_PYTEST_FILE)
  return None


if __name__ == '__main__':
  sys.exit(main(sys.argv))
