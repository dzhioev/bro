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
  harness_name,
  kill_command,
  llm_recipe,
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
  monkeypatch.setattr(
    harbor_agent,
    'benchmark_bundle',
    lambda: SimpleNamespace(
      harnesses=('bro', 'claude'), identity='sha256:bundle', interpreter=Path(sys.executable)
    ),
  )
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
  kwargs.setdefault('credentials', ['openai'])
  kwargs.setdefault('model_name', 'openai/gpt-5.6-terra')
  return BroAgent(logs_dir=tmp_path, **kwargs)


def _run_command(**overrides: Any) -> str:
  arguments: dict[str, Any] = {
    'bro': 'terminal',
    'instruction': 'do it',
    'timeout_sec': None,
  }
  arguments.update(overrides)
  return run_command(**arguments)


def _session(command: str) -> str:
  """the script `setsid` runs, out of the launch line's quoting."""
  launch = next(line for line in command.splitlines() if line.startswith('setsid'))
  return shlex.split(launch)[shlex.split(launch).index('-c') + 1]


@pytest.mark.parametrize(
  'text',
  [
    'openai/gpt-5.6-terra,reasoning_effort=high,compact_threshold=200000',
    'anthropic/claude-opus-5,effort=high,fast_mode=true',
    'foreign/model+passthrough',
  ],
)
def test_harbor_model_text_preserves_recipe_fields(text):
  provider, rest = text.split('/', 1)
  assert llm_recipe(text) == provider + ':' + rest


@pytest.mark.parametrize(
  'text', ['missing-slash', 'openai/', 'openai/model+unknown', 'openai/model,a=b,a=c']
)
def test_bad_recipe_grammar_refuses_agent_construction(tmp_path, text):
  with pytest.raises(ValueError):
    agent(tmp_path, bro='terminal', model_name=text)


def test_a_preset_without_model_name_is_refused(tmp_path):
  with pytest.raises(ValueError, match='model_name'):
    agent(tmp_path, bro='terminal', model_name=None)


def test_a_harness_is_registered_in_the_bundle(monkeypatch):
  monkeypatch.setattr(
    harbor_agent, 'benchmark_bundle', lambda: SimpleNamespace(harnesses=('claude',))
  )

  assert harness_name('claude') == 'claude'
  with pytest.raises(ValueError, match='installed harnesses: claude'):
    harness_name('bro')


def test_a_bad_harness_fails_before_any_trial_runs(tmp_path):
  with pytest.raises(ValueError, match='harness'):
    agent(tmp_path, bro='terminal', harness='codex')


def test_a_credential_is_never_implied(tmp_path):
  with pytest.raises(TypeError, match='credentials'):
    # kwargs arrive dynamically from a job config, so the runtime refusal is
    # the contract under test
    BroAgent(logs_dir=tmp_path, bro='terminal')  # pyright: ignore[reportCallIssue]


def test_the_error_patterns_are_the_roster_providers_signatures():
  patterns = {pattern.pattern for pattern in BroAgent.ERROR_PATTERNS}
  assert r'openai\.RateLimitError' in patterns  # the openai declaration reaches harbor


def test_the_trial_rides_the_bundle_as_an_unboxed_root_in_the_tasks_directory():
  command = ride_command(bro='terminal', instruction='do it')

  assert command.startswith(f'{BUNDLE.script("ride")} solo --unboxed --tree "$PWD" ')
  assert f'--runtime-bundle {BUNDLE.root} ' in command
  assert '--keep' in command
  assert command.endswith(" terminal 'do it'")


def test_the_rides_summons_may_join_its_party_and_start_nothing():
  command = ride_command(bro='terminal', instruction='do it')

  assert '--revoke bro.placement=boxed --grant bro.placement=join' in command


def test_the_harness_reaches_the_launch():
  command = ride_command(bro='terminal', instruction='do it')

  assert '--harness' not in command and '--llm' not in command


def test_the_trial_declares_the_task_container_a_sandbox():
  command = ride_command(bro='terminal', instruction='do it')

  # pins the variable claude consults before running as root with permissions skipped
  assert '--env IS_SANDBOX=1' in command


def test_launch_leaves_harness_and_recipe_to_trial_configuration():
  command = _run_command()
  assert '--llm' not in command and '--harness' not in command


def test_the_instruction_is_one_argument_however_it_is_written():
  instruction = "rm -rf / ; echo 'the task instruction is third-party text'"

  session = _session(_run_command(instruction=instruction))

  assert instruction in shlex.split(session)


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
  with scoped_store([CREDENTIAL]) as directory:
    config = json.loads((directory / 'creds.json').read_text())
    assert config['defaults'] == [INSTANCE]
    assert config['sources'] == {}
    assert config['material'][INSTANCE] == f'creds/{INSTANCE}.cred'
    assert (directory / 'creds' / f'{INSTANCE}.cred').read_text() == KEY


def test_the_bundle_hydrates_auxiliary_configuration_beside_the_llm_key(bundle_interpreter, store):
  material = store / 'creds' / 'mu+benchmark.cred'
  material.write_text(
    json.dumps(
      {'llm': 'openai:gpt-5.6-terra,reasoning_effort=low', 'credential': {'$cred': INSTANCE}}
    )
  )
  with scoped_store([INSTANCE, 'mu+benchmark']) as directory:
    config = json.loads((directory / 'creds.json').read_text())
    assert set(config['defaults']) == {INSTANCE, 'mu+benchmark'}
    auxiliary = json.loads((directory / 'creds' / 'mu+benchmark.cred').read_text())
    assert auxiliary['credential'] == json.loads(KEY)
    scoped = credentials.Store(credentials.default_registry(), directory, {})
    with credentials.as_default_store(scoped):
      assert credentials.available('mu')
      assert credentials.get_json('mu') == auxiliary


def test_the_store_is_private_while_it_exists_and_gone_after(bundle_interpreter, store):
  with scoped_store([CREDENTIAL]) as directory:
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / 'creds' / f'{INSTANCE}.cred').stat().st_mode & 0o777 == 0o600
    scratch = directory.parent

  assert not scratch.exists()


def test_a_credential_the_bundle_cannot_resolve_fails_before_the_container(
  bundle_interpreter, store
):
  with pytest.raises(RuntimeError, match="secret 'trails' not found"):
    with scoped_store(['trails']):
      pass


def test_the_recorded_identity_names_the_bro_harness_and_bundle_under_test(monkeypatch, tmp_path):
  identity = f'sha256:{"1" * 64}'
  monkeypatch.setattr(
    harbor_agent,
    'benchmark_bundle',
    lambda: SimpleNamespace(identity=identity, harnesses=('bro', 'claude')),
  )
  dev = agent(tmp_path, bro='dev', model_name='openai/gpt-5.6-terra').to_agent_info()
  terminal_agent = agent(tmp_path, bro='terminal', model_name='openai/gpt-5.6-terra')
  terminal = terminal_agent.to_agent_info()
  ridden = agent(
    tmp_path, bro='terminal', harness='claude', model_name='anthropic/opus5'
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
  monkeypatch.setattr(
    harbor_agent,
    'benchmark_bundle',
    lambda: SimpleNamespace(root=bundle, interpreter=interpreter, harnesses=('bro', 'claude')),
  )
  environment = FakeEnvironment()

  await agent(tmp_path, bro='terminal', credentials=[INSTANCE]).install(
    environment.as_environment()
  )

  assert [target for _, target in environment.uploads] == [str(BUNDLE.root), str(STORE_DIR)]
  assert environment.commands[-2].endswith(f'{BUNDLE.script("bro")} show terminal')
  assert environment.commands[-1].endswith(f'{BUNDLE.script("ride")} check-harness bro')


async def test_an_incompatible_bundle_fails_before_the_environment_is_touched(
  monkeypatch, tmp_path, store
):
  interpreter = tmp_path / 'incompatible-python'
  interpreter.write_text('#!/bin/sh\necho incompatible store >&2\nexit 1\n')
  interpreter.chmod(0o700)
  monkeypatch.setattr(
    harbor_agent,
    'benchmark_bundle',
    lambda: SimpleNamespace(
      root=tmp_path / 'bundle', interpreter=interpreter, harnesses=('bro', 'claude')
    ),
  )
  environment = FakeEnvironment()

  with pytest.raises(RuntimeError, match='incompatible store'):
    await agent(tmp_path, bro='terminal', credentials=[CREDENTIAL]).install(
      environment.as_environment()
    )

  assert environment.commands == []
  assert environment.uploads == []


async def test_the_install_checks_the_selected_harness_runtime(monkeypatch, tmp_path, store):
  interpreter = tmp_path / 'venv' / 'bin' / 'python3'
  _write_interpreter(interpreter)
  monkeypatch.setattr(
    harbor_agent,
    'benchmark_bundle',
    lambda: SimpleNamespace(root=tmp_path, interpreter=interpreter, harnesses=('bro', 'claude')),
  )
  environment = FakeEnvironment()

  await agent(tmp_path, bro='terminal', harness='claude', credentials=[INSTANCE]).install(
    environment.as_environment()
  )

  assert environment.commands[-2].endswith(f'{BUNDLE.script("bro")} show terminal')
  assert environment.commands[-1].endswith(f'{BUNDLE.script("ride")} check-harness claude')


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


def test_harbor_import_skips_a_broken_provider_but_explicit_selection_fails(tmp_path, monkeypatch):
  metadata = tmp_path / 'faulty_provider-1.dist-info'
  metadata.mkdir()
  (metadata / 'METADATA').write_text('Name: faulty-provider\nVersion: 1\n')
  (metadata / 'entry_points.txt').write_text(
    '[bro.llm.providers]\nfaulty = missing_faulty_provider.declaration\n'
  )
  monkeypatch.setenv('PYTHONPATH', str(tmp_path))
  result = subprocess.run(
    [
      sys.executable,
      '-c',
      """
from bro.benchmark.harbor_agent import BroAgent, llm_recipe
from bro.llm import providers

assert 'faulty' in providers.known_names()
healthy = providers.declaration('openai')
patterns = {pattern.pattern for pattern in BroAgent.ERROR_PATTERNS}
assert {signature.pattern for signature in healthy.FAILURE_SIGNATURES} <= patterns
assert llm_recipe('openai/gpt-5.6-terra') == 'openai:gpt-5.6-terra'
try:
  providers.declaration('faulty')
except providers.RecipeError as error:
  assert 'faulty' in str(error) and 'unavailable' in str(error)
else:
  raise AssertionError('explicit selection accepted a broken provider')
""",
    ],
    capture_output=True,
    text=True,
    check=True,
  )
  assert 'faulty' in result.stderr


def _agent_presets():
  from bro.benchmark.bundle import workspace_root

  directory = workspace_root() / 'benchmark' / 'agents'
  presets = sorted(path.stem for path in directory.glob('*.json'))
  assert presets, f'no agent presets found in {directory}'
  return presets


@pytest.mark.parametrize('preset_name', _agent_presets())
def test_trial_configuration_resolves_the_preset_for_root_and_summon(preset_name):
  from bro.authority.authority import Authority
  from bro.authority.rules import Request
  from bro.bench.presets import compose
  from bro.benchmark.bundle import workspace_root
  from bro.benchmark.harbor_agent import trial_host_config
  from bro.bro import BaseBro
  from bro.harness import Harness
  from bro.llm.providers import bind_recipe
  from bro.worker_types import installed_types
  from ride.authority_reading import Configuration, HarnessReading, Installation, Reading
  from ride.authority_scope import checked_recipe, resolve_recipe

  preset = compose(workspace_root(), agents=preset_name, settings='smoke', tasks='smoke')['agents'][
    0
  ]
  harness = preset['kwargs']['harness']
  text = llm_recipe(preset['model_name'])
  expected = bind_recipe(text)
  driver = Harness(harness)
  driver._llm_providers = (expected.TYPE,)
  kinds = frozenset(credentials.default_registry())
  bro = type('TrialBro', (BaseBro,), {'name': preset['kwargs']['bro']})()
  installation = Installation(
    {bro.name: bro},
    {harness: HarnessReading(driver)},
    kinds,
    {expected.TYPE: frozenset(expected.needed_secrets())},
    {name: worker.authority_declaration(kinds) for name, worker in installed_types().items()},
  )
  configuration = trial_host_config(harness, text)
  reading = Reading(
    Configuration.parse(configuration, home='benchmark'), {}, installation, home='benchmark'
  )
  root_harness = reading.resolve_harness(target=bro.name)
  root = resolve_recipe(reading, bro.name, root_harness)
  requested = Request('bro', {'target': bro.name})
  child = reading.rules.start(requested, Authority.top(), reading)
  assert checked_recipe(child.descriptor, reading) == root.recipe == expected
  assert (
    resolve_recipe(reading, bro.name, cast(str, child.descriptor.values['harness'])).source
    == root.source
    == 'defaults'
  )


async def test_install_writes_trial_configuration_in_the_task_home(monkeypatch, tmp_path, store):
  interpreter = tmp_path / 'venv' / 'bin' / 'python3'
  _write_interpreter(interpreter)
  monkeypatch.setattr(
    harbor_agent,
    'benchmark_bundle',
    lambda: SimpleNamespace(root=tmp_path, interpreter=interpreter, harnesses=('bro',)),
  )
  environment = FakeEnvironment()
  trial = agent(tmp_path, bro='terminal', credentials=[INSTANCE])
  await trial.install(environment.as_environment())
  command = next(command for command in environment.commands if '.bro.json' in command)
  home = tmp_path / 'task-home'
  home.mkdir()
  subprocess.run(['bash', '-c', command], check=True, env={'HOME': str(home)})
  config = json.loads((home / '.bro.json').read_text())
  assert config == harbor_agent.trial_host_config(trial._harness, trial._llm)
