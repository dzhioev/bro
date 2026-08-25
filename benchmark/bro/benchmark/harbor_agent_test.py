import asyncio
import json
import shlex
import subprocess
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
  kill_command,
  run_command,
  run_timeout,
  scoped_store,
)

CREDENTIAL = 'openai'
INSTANCE = 'openai+benchmark'
KEY = '{"api_key": "sk-test"}'


@pytest.fixture(autouse=True)
def _compose(monkeypatch):
  """answer the host probe every construction runs, without a subprocess and
  without carrying one test's answer into the next."""
  monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: _completed(command, 0))
  harbor_agent.docker_compose_missing.cache_clear()
  yield
  harbor_agent.docker_compose_missing.cache_clear()


def _completed(command, returncode: int) -> subprocess.CompletedProcess:
  return subprocess.CompletedProcess(command, returncode)


@pytest.fixture
def store(monkeypatch, tmp_path: Path) -> Path:
  """a resolver bounded to a generated registry holding one instance of one kind."""
  directory = tmp_path / 'configs'
  directory.mkdir()
  (directory / 'benchmark.json').write_text(KEY)
  (directory / 'credentials.json').write_text(
    json.dumps({INSTANCE: {'sources': [{'file': 'benchmark.json'}]}})
  )
  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(directory))
  monkeypatch.setattr(credentials, 'BRO_DIR', str(tmp_path / 'absent'))
  monkeypatch.setattr(credentials, '_default_store', None)
  return directory


class FakeEnvironment:
  """records what an agent asked of the environment, answering every exec.

  The bro's own command never returns, the way a run harbor has to cancel
  behaves; every other command answers at once, `results` deciding how.
  """

  def __init__(self):
    self.session_id = 'task__trial__env'
    self.default_user = None
    self.commands: list[str] = []
    self.envs: list[Optional[dict[str, str]]] = []
    self.uploads: list[tuple[Path, str]] = []
    self.results: dict[str, ExecResult] = {}
    self.bro_started = asyncio.Event()

  def as_environment(self) -> BaseEnvironment:
    return cast(BaseEnvironment, self)

  async def exec(self, command: str, **kwargs: Any) -> ExecResult:
    self.commands.append(command)
    self.envs.append(kwargs.get('env'))
    for needle, result in self.results.items():
      if needle in command:
        return result
    if 'setsid' in command:
      self.bro_started.set()
      await asyncio.Event().wait()
    return ExecResult(stdout='', stderr='', return_code=0)

  async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
    self.uploads.append((Path(source_dir), target_dir))


def agent(tmp_path: Path, **kwargs: Any) -> BroAgent:
  kwargs.setdefault('llm_credential', 'openai')
  return BroAgent(logs_dir=tmp_path, **kwargs)


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


def test_a_credential_is_never_implied(tmp_path):
  with pytest.raises(TypeError, match='llm_credential'):
    # kwargs arrive dynamically from a job config, so the runtime refusal is
    # the contract under test
    BroAgent(logs_dir=tmp_path, bro='terminal')  # pyright: ignore[reportCallIssue]


def test_the_error_patterns_are_the_roster_providers_signatures():
  patterns = {pattern.pattern for pattern in BroAgent.ERROR_PATTERNS}
  assert r'openai\.RateLimitError' in patterns  # the openai declaration reaches harbor


def test_the_recipe_reaches_the_run_with_its_provider_slot_empty():
  command = run_command(
    bro='terminal', instruction='do it', llm='gpt-5.6-terra:high', timeout_sec=None
  )

  assert '--llm :gpt-5.6-terra:high' in command


def test_no_recipe_leaves_the_bros_own():
  command = run_command(bro='terminal', instruction='do it', llm=None, timeout_sec=None)

  assert '--llm' not in command


def test_the_run_drives_the_bundle_shim_in_this_process():
  command = run_command(bro='terminal', instruction='do it', llm=None, timeout_sec=None)

  assert f'{BUNDLE.shim} run terminal' in command


def test_the_instruction_is_one_argument_however_it_is_written():
  instruction = "rm -rf / ; echo 'the task instruction is third-party text'"
  command = run_command(bro='terminal', instruction=instruction, llm=None, timeout_sec=None)

  launch = next(line for line in command.splitlines() if line.startswith('setsid'))
  session = shlex.split(launch)[shlex.split(launch).index('-c') + 1]

  assert instruction in shlex.split(session)


def test_the_activity_log_lands_in_the_trial_directory():
  command = run_command(bro='terminal', instruction='do it', llm=None, timeout_sec=None)

  assert '2> /logs/agent/bro.log' in command
  assert 'tail -c' in command


def test_the_bro_runs_in_its_own_process_group():
  command = run_command(bro='terminal', instruction='do it', llm=None, timeout_sec=None)

  assert 'setsid --wait' in command
  assert f'echo $$ > {harbor_agent.PGID_FILE}' in command


def test_an_in_container_ceiling_is_opt_in():
  without = run_command(bro='terminal', instruction='do it', llm=None, timeout_sec=None)
  with_ceiling = run_command(bro='terminal', instruction='do it', llm=None, timeout_sec=900)

  assert 'timeout ' not in without
  assert '--kill-after=5 900' in with_ceiling


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


def test_the_run_environment_points_at_the_store_bundle_and_record_root(tmp_path):
  environment = agent(tmp_path, bro='terminal').run_env()

  assert environment == {
    'BRO_CONFIGS_DIR': str(STORE_DIR),
    'BRO_USAGE_FILE': '/logs/agent/usage.json',
    'SSL_CERT_FILE': str(BUNDLE.ca_bundle),
    'XDG_DATA_HOME': '/logs/agent',
  }


def test_the_store_carries_the_named_instance_under_its_kind(store):
  with scoped_store(INSTANCE) as directory:
    registry = json.loads((directory / 'credentials.json').read_text())
    assert set(registry) == {CREDENTIAL}
    assert (directory / f'{CREDENTIAL}.cred').read_text() == KEY


def test_the_store_is_private_while_it_exists_and_gone_after(store):
  with scoped_store(INSTANCE) as directory:
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / f'{CREDENTIAL}.cred').stat().st_mode & 0o777 == 0o600
    scratch = directory.parent

  assert not scratch.exists()


def test_a_credential_the_host_cannot_resolve_fails_before_the_container(store):
  with pytest.raises(ValueError, match='trails'):
    with scoped_store('trails'):
      pass


def test_the_recorded_identity_names_the_bro_and_bundle_under_test(monkeypatch, tmp_path):
  identity = f'sha256:{"1" * 64}'
  monkeypatch.setattr(harbor_agent, 'benchmark_bundle', lambda: SimpleNamespace(identity=identity))
  dev = agent(tmp_path, bro='dev', model_name='openai/gpt-5.6-terra').to_agent_info()
  terminal_agent = agent(tmp_path, bro='terminal', model_name='openai/gpt-5.6-terra')
  terminal = terminal_agent.to_agent_info()

  assert dev.name != terminal.name
  assert terminal.name == 'bro:terminal'
  assert terminal.version == identity
  assert terminal_agent.version() == identity
  assert terminal.model_info is not None
  assert terminal.model_info.name == 'gpt-5.6-terra'


def test_a_provider_failure_is_classified_from_the_frameworks_own_output(tmp_path):
  bro_agent = agent(tmp_path, bro='terminal')

  classified = bro_agent._classify_exec_error(
    'bro run', ExecResult(stdout='', stderr='openai.RateLimitError: 429', return_code=1)
  )

  assert isinstance(classified, ApiRateLimitError)


def test_task_prose_no_longer_classifies_a_failure(tmp_path):
  bro_agent = agent(tmp_path, bro='terminal')

  classified = bro_agent._classify_exec_error(
    'bro run',
    ExecResult(stdout='the service under test answers with Connection refused', return_code=1),
  )

  assert type(classified) is NonZeroAgentExitCodeError


def test_a_transport_failure_still_classifies(tmp_path):
  bro_agent = agent(tmp_path, bro='terminal')

  classified = bro_agent._classify_exec_error(
    'bro run', ExecResult(stderr='openai.APIConnectionError: [Errno -2]', return_code=1)
  )

  assert isinstance(classified, NetworkConnectionError)


def _usage(tmp_path: Path, **counts: int) -> None:
  (tmp_path / 'usage.json').write_text(
    json.dumps({'agent': 'bro//terminal', 'models': {'gpt-5.6-terra': counts}})
  )


def test_the_whole_prompt_is_reported_as_input(tmp_path):
  _usage(tmp_path, input=10, cache_write=3, cache_read=100, output=7)
  context = AgentContext()

  agent(tmp_path, bro='terminal').populate_context_post_run(context)

  assert context.n_input_tokens == 113
  assert context.n_cache_tokens == 100
  assert context.n_output_tokens == 7
  assert context.cost_usd is None


def test_a_run_that_reached_no_model_reports_nothing(tmp_path):
  context = AgentContext()

  agent(tmp_path, bro='terminal').populate_context_post_run(context)

  assert context.is_empty()


def test_an_unreadable_usage_file_does_not_cost_the_trial_its_grade(tmp_path):
  (tmp_path / 'usage.json').write_text('{ truncated')
  context = AgentContext()

  agent(tmp_path, bro='terminal').populate_context_post_run(context)

  assert context.is_empty()


async def test_the_install_uploads_both_trees_and_proves_the_bro(monkeypatch, tmp_path, store):
  bundle = tmp_path / 'bundle'
  bundle.mkdir()
  monkeypatch.setattr(harbor_agent, 'workspace_root', lambda: tmp_path)
  monkeypatch.setattr(harbor_agent, 'default_root', lambda root: bundle)
  monkeypatch.setattr(harbor_agent, 'built', lambda root: harbor_agent.Bundle(root))
  environment = FakeEnvironment()

  await agent(tmp_path, bro='terminal', llm_credential=INSTANCE).install(
    environment.as_environment()
  )

  assert [target for _, target in environment.uploads] == [str(BUNDLE.root), str(STORE_DIR)]
  assert f'{BUNDLE.shim} show terminal' in environment.commands[-1]


async def test_a_cancelled_phase_reaps_the_bro_and_stays_cancelled(tmp_path):
  environment = FakeEnvironment()
  context = AgentContext()
  run = asyncio.ensure_future(
    agent(tmp_path, bro='terminal').run('do it', environment.as_environment(), context)
  )
  await environment.bro_started.wait()

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
  await environment.bro_started.wait()

  run.cancel()
  with pytest.raises(asyncio.CancelledError):
    await run
