import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast

import pytest
from harbor.agents.installed.base import (
  ApiRateLimitError,
  NetworkConnectionError,
  NonZeroAgentExitCodeError,
)
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.agent.context import AgentContext

from bro.base import credentials
from bro.benchmark import harbor_agent
from bro.benchmark.harbor_agent import (
  BUNDLE,
  STORE_DIR,
  BroAgent,
  bare_recipe,
  harness_name,
  kill_command,
  reported_agent_name,
  ride_command,
  run_command,
  run_timeout,
  scoped_store,
)
from bro.benchmark.trial_store import TRAILS_DIRECTORY
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest
from bro.trails.record.spine import Recording
from ride.workspace.spawn import PROCESS_TERM_GRACE

CREDENTIAL = 'openai'
INSTANCE = 'openai+benchmark'
KEY = '{"api_key": "sk-test"}'
_REAL_SUBPROCESS_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _compose(monkeypatch):
  """answer the host probe every construction runs, without a subprocess and
  without carrying one test's answer into the next."""

  def run(command, **kwargs):
    if tuple(command) == harbor_agent.COMPOSE_PROBE:
      return _completed(command, 0)
    return _REAL_SUBPROCESS_RUN(command, **kwargs)

  monkeypatch.setattr(subprocess, 'run', run)
  harbor_agent.docker_compose_missing.cache_clear()
  yield
  harbor_agent.docker_compose_missing.cache_clear()


def _completed(command, returncode: int) -> subprocess.CompletedProcess:
  return subprocess.CompletedProcess(command, returncode)


def _write_interpreter(path: Path) -> None:
  path.parent.mkdir(parents=True)
  path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
  path.chmod(0o700)


@pytest.fixture
def bundle_interpreter(monkeypatch):
  monkeypatch.setattr(
    harbor_agent,
    'benchmark_bundle',
    lambda: SimpleNamespace(interpreter=Path(sys.executable)),
  )


@pytest.fixture
def store(monkeypatch, tmp_path: Path) -> Path:
  """An exclusive store holding one instance of one kind."""
  directory = tmp_path / 'store'
  material = directory / credentials.MATERIAL_DIR / f'{INSTANCE}.cred'
  material.parent.mkdir(parents=True)
  material.write_text(KEY)
  (directory / credentials.STORE_FILE).write_text(json.dumps({'defaults': [INSTANCE]}))
  monkeypatch.setattr(credentials, 'STORE_DIR', str(directory))
  monkeypatch.setattr(credentials, '_default_store', None)
  return directory


class FakeEnvironment:
  """records what an agent asked of the environment, answering every exec.

  The ride's own command never returns, the way a run harbor has to cancel
  behaves; every other command answers at once, `results` deciding how.
  """

  def __init__(self):
    self.session_id = 'task__trial__env'
    self.default_user = None
    self.commands: list[str] = []
    self.envs: list[Optional[dict[str, str]]] = []
    self.uploads: list[tuple[Path, str]] = []
    self.results: dict[str, ExecResult] = {}
    self.ride_started = asyncio.Event()

  def as_environment(self) -> BaseEnvironment:
    return cast(BaseEnvironment, self)

  async def exec(self, command: str, **kwargs: Any) -> ExecResult:
    self.commands.append(command)
    self.envs.append(kwargs.get('env'))
    for needle, result in self.results.items():
      if needle in command:
        return result
    if 'setsid' in command:
      self.ride_started.set()
      await asyncio.Event().wait()
    return ExecResult(stdout='', stderr='', return_code=0)

  async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
    self.uploads.append((Path(source_dir), target_dir))


def agent(tmp_path: Path, **kwargs: Any) -> BroAgent:
  kwargs.setdefault('llm_credential', 'openai')
  return BroAgent(logs_dir=tmp_path, **kwargs)


def _run_command(**overrides: Any) -> str:
  arguments: dict[str, Any] = {
    'bro': 'terminal',
    'instruction': 'do it',
    'harness': 'bro',
    'llm': None,
    'timeout_sec': None,
  }
  arguments.update(overrides)
  return run_command(**arguments)


def _session(command: str) -> str:
  """the script `setsid` runs, out of the launch line's quoting."""
  launch = next(line for line in command.splitlines() if line.startswith('setsid'))
  return shlex.split(launch)[shlex.split(launch).index('-c') + 1]


def test_a_recipe_is_named_within_a_registered_provider():
  assert bare_recipe('openai/gpt-5.6-terra') == 'gpt-5.6-terra'
  assert bare_recipe('openai/gpt-5.6-terra:high') == 'gpt-5.6-terra:high'
  assert bare_recipe('openai/gpt-5.6-terra:high+fast') == 'gpt-5.6-terra:high+fast'
  assert bare_recipe('claude-code/opus5') == 'opus5'
  assert bare_recipe(None) is None


@pytest.mark.parametrize('model_name', ['anthropic/opus', 'gpt-5.6-terra', 'openai/'])
def test_a_recipe_outside_that_form_is_refused(model_name):
  with pytest.raises(ValueError, match='registered provider'):
    bare_recipe(model_name)


@pytest.mark.parametrize('model_name', ['openai/a:b:c:d', 'openai/gpt+turbo', 'openai/gpt:sprint'])
def test_a_recipe_off_the_llm_grammar_is_refused(model_name):
  with pytest.raises(ValueError):
    bare_recipe(model_name)


def test_a_bad_model_fails_before_any_trial_runs(tmp_path):
  with pytest.raises(ValueError, match='registered provider'):
    agent(tmp_path, bro='terminal', model_name='anthropic/opus')


def test_a_harness_is_one_ride_knows():
  assert harness_name('claude') == 'claude'
  with pytest.raises(ValueError, match='codex'):
    harness_name('codex')


def test_a_bad_harness_fails_before_any_trial_runs(tmp_path):
  with pytest.raises(ValueError, match='harness'):
    agent(tmp_path, bro='terminal', harness='codex')


def test_a_credential_is_never_implied(tmp_path):
  with pytest.raises(TypeError, match='llm_credential'):
    # kwargs arrive dynamically from a job config, so the runtime refusal is
    # the contract under test
    BroAgent(logs_dir=tmp_path, bro='terminal')  # pyright: ignore[reportCallIssue]


def test_the_error_patterns_are_the_roster_providers_signatures():
  patterns = {pattern.pattern for pattern in BroAgent.ERROR_PATTERNS}
  assert r'openai\.RateLimitError' in patterns  # the openai declaration reaches harbor


def test_the_trial_rides_the_bundle_as_an_unboxed_root_in_the_tasks_directory():
  command = ride_command(bro='terminal', instruction='do it', harness='bro', llm=None)

  assert command.startswith(f'{BUNDLE.script("ride")} solo --unboxed --tree "$PWD" ')
  assert f'--runtime-bundle {BUNDLE.root} ' in command
  assert '--keep' in command
  assert command.endswith(" --harness bro terminal 'do it'")


def test_the_rides_summons_may_join_its_party_and_start_nothing():
  command = ride_command(bro='terminal', instruction='do it', harness='bro', llm=None)

  assert '--revoke :launch.bro.party.boxed --grant :launch.bro.party.join' in command


def test_the_harness_reaches_the_launch():
  command = ride_command(bro='terminal', instruction='do it', harness='claude', llm=None)

  assert '--harness claude terminal' in command


def test_the_trial_declares_the_task_container_a_sandbox():
  command = ride_command(bro='terminal', instruction='do it', harness='claude', llm=None)

  # pins the variable claude consults before running as root with permissions skipped
  assert '--env IS_SANDBOX=1' in command


def test_the_recipe_reaches_the_run_with_its_provider_slot_empty():
  command = _run_command(llm='gpt-5.6-terra:high')

  assert '--llm :gpt-5.6-terra:high' in command


def test_no_recipe_leaves_the_bros_own():
  assert '--llm' not in _run_command()


def test_the_instruction_is_one_argument_however_it_is_written():
  instruction = "rm -rf / ; echo 'the task instruction is third-party text'"

  session = _session(_run_command(instruction=instruction))

  assert instruction in shlex.split(session)


def test_the_bundled_claude_leads_the_sessions_path():
  session = _session(_run_command())

  assert f'export PATH={BUNDLE.claude_dir}:"$PATH"' in session


def test_the_activity_log_lands_in_the_trial_directory():
  command = _run_command()

  assert '2> /logs/agent/bro.log' in command
  assert 'tail -c' in command


def test_the_ride_runs_in_its_own_process_group():
  command = _run_command()

  assert 'setsid --wait' in command
  assert f'echo $$ > {harbor_agent.PGID_FILE}' in command


def test_an_in_container_ceiling_is_opt_in():
  without = _run_command()
  with_ceiling = _run_command(timeout_sec=900)

  assert 'timeout ' not in without
  assert f'--kill-after={harbor_agent.TERM_GRACE_SEC} 900' in with_ceiling


def test_the_kill_waits_past_the_rides_own_teardown_grace():
  assert harbor_agent.TERM_GRACE_SEC > PROCESS_TERM_GRACE
  assert f'seq {harbor_agent.TERM_GRACE_SEC}' in kill_command()


@pytest.mark.parametrize('value', [0, -1, 'soon'])
def test_a_ceiling_that_is_no_duration_is_refused(value):
  with pytest.raises(ValueError):
    run_timeout(value)


def test_a_ceiling_survives_the_string_a_command_line_kwarg_carries():
  assert run_timeout('900') == 900


@pytest.mark.parametrize(('returncode', 'missing'), [(0, False), (1, True)])
def test_the_compose_plugin_is_probed_through_the_docker_cli(monkeypatch, returncode, missing):
  monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: _completed(command, returncode))

  assert harbor_agent.docker_compose_missing() is missing


def test_a_host_without_docker_at_all_reads_as_missing(monkeypatch):
  def absent(*args, **kwargs):
    raise FileNotFoundError('docker')

  monkeypatch.setattr(subprocess, 'run', absent)

  assert harbor_agent.docker_compose_missing() is True


def test_the_kill_reaps_the_group_and_reports_success():
  command = kill_command()

  assert 'kill -TERM -"$pgid"' in command
  assert 'kill -KILL -"$pgid"' in command
  assert command.endswith('exit 0')


def test_the_run_environment_points_at_the_store_and_the_record_root(tmp_path):
  environment = agent(tmp_path, bro='terminal').run_env()

  assert environment == {'BRO_STORE': str(STORE_DIR), 'XDG_DATA_HOME': '/logs/agent'}


def test_the_bundle_follows_the_host_store_default(bundle_interpreter, store):
  with scoped_store(CREDENTIAL) as directory:
    config = json.loads((directory / 'creds.json').read_text())
    assert config == {'defaults': [INSTANCE], 'sources': {}}
    assert (directory / 'creds' / f'{INSTANCE}.cred').read_text() == KEY


def test_the_store_is_private_while_it_exists_and_gone_after(bundle_interpreter, store):
  with scoped_store(CREDENTIAL) as directory:
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / 'creds' / f'{INSTANCE}.cred').stat().st_mode & 0o777 == 0o600
    scratch = directory.parent

  assert not scratch.exists()


def test_a_credential_the_bundle_cannot_resolve_fails_before_the_container(
  bundle_interpreter, store
):
  with pytest.raises(RuntimeError, match="secret 'trails' not found"):
    with scoped_store('trails'):
      pass


def test_the_recorded_identity_names_the_bro_harness_and_bundle_under_test(monkeypatch, tmp_path):
  identity = f'sha256:{"1" * 64}'
  monkeypatch.setattr(harbor_agent, 'benchmark_bundle', lambda: SimpleNamespace(identity=identity))
  dev = agent(tmp_path, bro='dev', model_name='openai/gpt-5.6-terra').to_agent_info()
  terminal_agent = agent(tmp_path, bro='terminal', model_name='openai/gpt-5.6-terra')
  terminal = terminal_agent.to_agent_info()
  ridden = agent(
    tmp_path, bro='terminal', harness='claude', model_name='claude-code/opus5'
  ).to_agent_info()

  assert len({dev.name, terminal.name, ridden.name}) == 3
  assert terminal.name == reported_agent_name('terminal')
  assert ridden.name == reported_agent_name('terminal', 'claude')
  assert terminal.version == identity
  assert terminal_agent.version() == identity
  assert terminal.model_info is not None
  assert terminal.model_info.name == 'gpt-5.6-terra'


def test_the_default_harness_leaves_the_identity_unqualified():
  # the name retained runs and the Hub key a trial's statistics by
  assert reported_agent_name('terminal') == 'bro:terminal'
  assert reported_agent_name('terminal', 'bro') == 'bro:terminal'
  assert reported_agent_name('terminal', 'claude') == 'bro:terminal:claude'


def test_a_provider_failure_is_classified_from_the_frameworks_own_output(tmp_path):
  bro_agent = agent(tmp_path, bro='terminal')

  classified = bro_agent._classify_exec_error(
    'ride solo', ExecResult(stdout='', stderr='openai.RateLimitError: 429', return_code=1)
  )

  assert isinstance(classified, ApiRateLimitError)


def test_task_prose_no_longer_classifies_a_failure(tmp_path):
  bro_agent = agent(tmp_path, bro='terminal')

  classified = bro_agent._classify_exec_error(
    'ride solo',
    ExecResult(stdout='the service under test answers with Connection refused', return_code=1),
  )

  assert type(classified) is NonZeroAgentExitCodeError


def test_a_transport_failure_still_classifies(tmp_path):
  bro_agent = agent(tmp_path, bro='terminal')

  classified = bro_agent._classify_exec_error(
    'ride solo', ExecResult(stderr='openai.APIConnectionError: [Errno -2]', return_code=1)
  )

  assert isinstance(classified, NetworkConnectionError)


def _record_trail(agent_directory: Path, calls: list[dict[str, Any]]) -> None:
  """one trail under the agent directory's store, an `llm_call` per usage record."""
  store = LocalStore(agent_directory / TRAILS_DIRECTORY)
  recording = Recording.create(
    store,
    BlazeRequest(
      harness='bro',
      bro='terminal',
      version='test',
      native={'llm': {'type': 'openai', 'model': 'gpt-5.6-terra', 'effort': 'high'}},
      body={'records': [{'kind': 'system_prompt', 'body': 'prompt'}]},
      interactive=False,
      surface='benchmark',
    ),
  )
  recording.append(
    [
      {
        'kind': 'llm_call',
        'body': {
          'request': {'model': 'gpt-5.6-terra'},
          'response': {'model': 'gpt-5.6-terra', 'usage': usage, 'output': []},
        },
      }
      for usage in calls
    ]
  )
  recording.end('ok')
  store.close()


def test_the_whole_prompt_is_reported_as_input(tmp_path):
  _record_trail(
    tmp_path,
    [
      {
        'input_tokens': 10,
        'input_tokens_details': {'cached_tokens': 3, 'cache_write_tokens': 2},
        'output_tokens': 4,
      },
      {'input_tokens': 5, 'output_tokens': 1},
    ],
  )
  context = AgentContext()

  agent(tmp_path, bro='terminal').populate_context_post_run(context)

  assert context.n_input_tokens == 15
  assert context.n_cache_tokens == 3
  assert context.n_output_tokens == 5
  assert context.cost_usd is None


def test_a_run_that_recorded_no_trail_reports_nothing(tmp_path):
  context = AgentContext()

  agent(tmp_path, bro='terminal').populate_context_post_run(context)

  assert context.is_empty()


def test_an_unreadable_store_does_not_cost_the_trial_its_grade(tmp_path):
  _record_trail(tmp_path, [{'input_tokens': 5, 'output_tokens': 1}])
  for steps in (tmp_path / TRAILS_DIRECTORY).rglob('steps.jsonl'):
    steps.write_text('{ truncated')
  context = AgentContext()

  agent(tmp_path, bro='terminal').populate_context_post_run(context)

  assert context.is_empty()


async def test_the_install_uploads_both_trees_and_proves_the_bro(monkeypatch, tmp_path, store):
  bundle = tmp_path / 'bundle'
  interpreter = bundle / 'venv' / 'bin' / 'python3'
  _write_interpreter(interpreter)
  monkeypatch.setattr(harbor_agent, 'workspace_root', lambda: tmp_path)
  monkeypatch.setattr(harbor_agent, 'default_root', lambda root: bundle)
  monkeypatch.setattr(harbor_agent, 'built', lambda root: harbor_agent.Bundle(root))
  environment = FakeEnvironment()

  await agent(tmp_path, bro='terminal', llm_credential=INSTANCE).install(
    environment.as_environment()
  )

  assert [target for _, target in environment.uploads] == [str(BUNDLE.root), str(STORE_DIR)]
  assert environment.commands[-1].endswith(f'{BUNDLE.script("bro")} show terminal')


async def test_an_incompatible_bundle_fails_before_the_environment_is_touched(
  monkeypatch, tmp_path, store
):
  interpreter = tmp_path / 'incompatible-python'
  interpreter.write_text('#!/bin/sh\necho incompatible store >&2\nexit 1\n')
  interpreter.chmod(0o700)
  monkeypatch.setattr(
    harbor_agent,
    'benchmark_bundle',
    lambda: SimpleNamespace(root=tmp_path / 'bundle', interpreter=interpreter),
  )
  environment = FakeEnvironment()

  with pytest.raises(RuntimeError, match='incompatible store'):
    await agent(tmp_path, bro='terminal', llm_credential=CREDENTIAL).install(
      environment.as_environment()
    )

  assert environment.commands == []
  assert environment.uploads == []


async def test_the_install_proves_the_bundled_claude_on_its_harness(monkeypatch, tmp_path, store):
  interpreter = tmp_path / 'venv' / 'bin' / 'python3'
  _write_interpreter(interpreter)
  monkeypatch.setattr(harbor_agent, 'benchmark_bundle', lambda: harbor_agent.Bundle(tmp_path))
  environment = FakeEnvironment()

  await agent(tmp_path, bro='terminal', harness='claude', llm_credential=INSTANCE).install(
    environment.as_environment()
  )

  assert environment.commands[-2].endswith(f'{BUNDLE.script("bro")} show terminal')
  assert environment.commands[-1].endswith(f'{BUNDLE.claude} --version')


async def test_a_cancelled_phase_reaps_the_ride_and_stays_cancelled(tmp_path):
  environment = FakeEnvironment()
  context = AgentContext()
  run = asyncio.ensure_future(
    agent(tmp_path, bro='terminal').run('do it', environment.as_environment(), context)
  )
  await environment.ride_started.wait()

  run.cancel()
  with pytest.raises(asyncio.CancelledError):
    await run

  assert 'kill -KILL' in environment.commands[-1]


async def test_a_reap_that_fails_does_not_replace_the_cancellation(tmp_path):
  environment = FakeEnvironment()
  environment.results['kill -TERM'] = ExecResult(stderr='no such container', return_code=1)
  run = asyncio.ensure_future(
    agent(tmp_path, bro='terminal').run('do it', environment.as_environment(), AgentContext())
  )
  await environment.ride_started.wait()

  run.cancel()
  with pytest.raises(asyncio.CancelledError):
    await run
