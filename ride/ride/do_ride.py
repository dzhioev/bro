"""Run one session in a prepared workspace."""

import contextlib
import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.base import credentials
from bro.base.args import Parser
from bro.launch.broxy import session_broxy
from bro.launch.hold import HOLD_VARIABLE
from bro.llm.llm import LLMSpec
from bro.monitor import CLAUDE_CONFIG_DIR_ENV, SESSION_DIR_ENV, session_dir
from bro.registry import create_bro
from bro.summon import party_member
from bro.workspace.paths import ISOLATION_ENV, workspace_dir
from bro.workspace.session import clear_requested_exit_status, requested_exit_status
from ride.errors import reports_runtime_errors
from ride.identity import bro_git_identity_env

if TYPE_CHECKING:
  from ride.harness import Harness
  from ride.session import SessionSpec

__cli_name__ = 'do-ride'

INSTALL_DIRECTORY_ENV = 'BRO_INSTALL_DIR'
RESOLVED_LLM_ENV = 'RIDE_RESOLVED_LLM'
CONTAINER_INSTALL_DIRECTORY = '/home/ride/.bro-environment'
PROCESS_FILENAME = 'runner.pid'


def _boxed() -> bool:
  isolation = os.environ.get(ISOLATION_ENV)
  if isolation not in ('boxed', 'unboxed'):
    raise RuntimeError(f'{ISOLATION_ENV} must be set to boxed or unboxed')
  return isolation == 'boxed'


@dataclass(frozen=True)
class SessionRun:
  """The session fields consumed inside a prepared workspace."""

  name: str
  repo: Optional[str]
  harness: str
  hold: str
  llm: Optional[str]
  resolved_llm: dict
  solo: bool
  resume: bool
  bro: str
  prompt: Optional[str]
  arguments: list[str]
  harness_options: dict
  into: Optional[str] = None

  @property
  def llm_spec(self) -> LLMSpec:
    return LLMSpec.from_dict(self.resolved_llm)


def command(spec: 'SessionSpec', *, harness_flags: Sequence[str]) -> list[str]:
  """Build the do-ride argv for a launcher's session spec."""
  verb = 'solo' if spec.solo else 'along'
  parts = [
    'do-ride',
    verb,
    '--workspace',
    spec.name,
    '--harness',
    spec.harness,
    *(['--resume'] if spec.resume else []),
    *harness_flags,
  ]
  if spec.repo is not None:
    parts.extend(['--repo', spec.repo])
  parts.extend(['--hold', spec.hold])
  if spec.llm is not None:
    parts.extend(['--llm', spec.llm])
  parts.append(spec.bro)
  if spec.prompt is not None:
    parts.append(spec.prompt)
  if len(spec.arguments) > 0:
    parts.extend(['--', *spec.arguments])
  return parts


def _configure_mode_parser(parser: Parser, *, solo: bool) -> None:
  from bro.mcp import HOLDS
  from ride.harness import HARNESS_NAMES, get_harness

  parser.add_argument('--workspace', required=True, help='prepared workspace name')
  parser.add_argument('--harness', required=True, choices=HARNESS_NAMES, help='driving harness')
  parser.add_argument(
    '--resume', action='store_true', env=False, help='resume the recorded session'
  )
  parser.add_argument('--repo', default=None, metavar='PATH|URL', help='repository attachment')
  parser.add_argument('--hold', required=True, choices=HOLDS, help='session user-involvement level')
  parser.add_argument('--llm', default=None, help='session LLM recipe')
  for harness_name in HARNESS_NAMES:
    get_harness(harness_name).add_flags(parser)
  parser.add_argument('bro', help='bro personality to run the harness as')
  parser.add_argument(
    'prompt',
    **({} if solo else {'nargs': '?', 'default': None}),
    help='prompt to answer' if solo else 'initial prompt',
  )


def build_parser() -> Parser:
  parser = Parser(description='run one session in a prepared workspace')
  subparsers = parser.add_subparsers(dest='mode', required=True)
  _configure_mode_parser(subparsers.add_parser('solo', help='run a one-shot session'), solo=True)
  _configure_mode_parser(
    subparsers.add_parser('along', help='run an interactive session'), solo=False
  )
  return parser


def _parse(argv: list[str]) -> tuple[dict, list[str]]:
  parser = build_parser()
  try:
    separator = argv.index('--')
  except ValueError:
    return parser.parse(argv), []
  return parser.parse(argv[:separator]), argv[separator + 1 :]


def encode_resolved_llm(resolved_llm: dict) -> str:
  return json.dumps(resolved_llm, separators=(',', ':'))


def _resolved_llm(harness: 'Harness', llm: Optional[str], bro: str) -> dict:
  encoded = os.environ.get(RESOLVED_LLM_ENV)
  if encoded is None:
    return harness.resolve_llm(llm, bro).dump()
  data = json.loads(encoded)
  if not isinstance(data, dict):
    raise TypeError(f'{RESOLVED_LLM_ENV} must encode an object')
  return LLMSpec.from_dict(data).dump()


def _session_run(args: dict, arguments: list[str]) -> tuple['Harness', SessionRun]:
  from ride.flags import pop_harness_options
  from ride.harness import get_harness
  from ride.workspace.metadata import Isolation

  mode = args.pop('mode')
  harness_name = args.pop('harness')
  harness = get_harness(harness_name)
  parser = build_parser()
  harness_options = pop_harness_options(
    parser,
    args,
    harness_name,
    solo=mode == 'solo',
    isolation=Isolation.BOXED,
  )
  name = args.pop('workspace')
  bro = args.pop('bro')
  llm = args.pop('llm')
  run = SessionRun(
    name=name,
    harness=harness_name,
    solo=mode == 'solo',
    bro=bro,
    llm=llm,
    resolved_llm=_resolved_llm(harness, llm, bro),
    arguments=arguments,
    harness_options=harness_options,
    **args,
  )
  return harness, run


def _process_start_time(process_id: int) -> str:
  stat = Path(f'/proc/{process_id}/stat')
  if stat.is_file():
    fields = stat.read_text().rsplit(')', 1)[1].split()
    return f'linux-ticks:{fields[19]}'
  result = subprocess.run(
    ['ps', '-o', 'lstart=', '-p', str(process_id)],
    check=True,
    capture_output=True,
    text=True,
  )
  value = result.stdout.strip()
  if len(value) == 0:
    raise RuntimeError(f'cannot read start time for process {process_id}')
  return f'ps:{value}'


@contextlib.contextmanager
def _process_record() -> Generator[None]:
  state = session_dir()
  if state is None:
    raise RuntimeError(f'{SESSION_DIR_ENV} is unset: do-ride requires a session state directory')
  state.mkdir(parents=True, exist_ok=True)
  process_id = os.getpid()
  content = json.dumps({'pid': process_id, 'start_time': _process_start_time(process_id)})
  path = state / PROCESS_FILENAME
  temporary = path.with_suffix('.tmp')
  temporary.write_text(content)
  temporary.replace(path)
  try:
    yield
  finally:
    try:
      owned = path.read_text() == content
    except OSError:
      owned = False
    if owned:
      path.unlink()


def _install_credential_hooks() -> None:
  store_path = os.environ.get('BRO_STORE')
  kinds_value = os.environ.get('BRO_INSTALL_KINDS')
  if store_path is None and kinds_value is None:
    return
  if store_path is None or kinds_value is None:
    raise RuntimeError('BRO_STORE and BRO_INSTALL_KINDS must be set together')
  directory_value = os.environ.get(INSTALL_DIRECTORY_ENV)
  if directory_value is None:
    directory = (
      Path(CONTAINER_INSTALL_DIRECTORY)
      if _boxed()
      else workspace_dir(os.environ['RIDE_WORKSPACE']) / 'environment'
    )
    os.environ[INSTALL_DIRECTORY_ENV] = str(directory)
  else:
    directory = Path(directory_value)
  store = credentials.default_store()
  os.environ.update(
    credentials.install_hooks(
      store.registry,
      kinds_value.split(),
      store,
      directory,
      os.environ,
    )
  )


def _prepare_claude_state(run: SessionRun) -> None:
  if run.harness != 'claude':
    return
  from ride.claude.claude_config import provision_unboxed_claude_dir, seed_session_plugins

  boxed = _boxed()
  config_value = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
  if config_value is None:
    if boxed:
      config_directory = Path.home() / '.claude'
      config_directory.mkdir(parents=True, exist_ok=True)
    else:
      config_directory = provision_unboxed_claude_dir(workspace_dir(run.name), Path.cwd())
    os.environ[CLAUDE_CONFIG_DIR_ENV] = str(config_directory)
  else:
    config_directory = Path(config_value)
  seed_session_plugins(config_directory, container=boxed)


@contextlib.contextmanager
def stopped_on_sigterm(stop: Callable[[], None]) -> Generator[threading.Event]:
  """Run stop once when SIGTERM arrives during the block."""
  stopped = threading.Event()

  def _stop(signum, frame):
    del signum, frame
    if stopped.is_set():
      return
    stopped.set()
    threading.Thread(target=stop, daemon=True).start()

  previous = signal.signal(signal.SIGTERM, _stop)
  try:
    yield stopped
  finally:
    signal.signal(signal.SIGTERM, previous)


def run_agent(argv: list[str], env: Optional[dict[str, str]] = None) -> int:
  """Spawn an agent process and terminate it when the session runner is stopped."""
  process = subprocess.Popen(argv, env=env)
  with stopped_on_sigterm(process.terminate):
    return process.wait()


def run_session(harness: 'Harness', run: SessionRun) -> int:
  """Run the session in this process's prepared working directory."""
  os.environ.update(bro_git_identity_env(run.bro))
  os.environ['RIDE_WORKSPACE'] = run.name
  if run.repo is None:
    os.environ.pop('RIDE_REPO', None)
  else:
    os.environ['RIDE_REPO'] = run.repo
  os.environ['RIDE_BRO'] = run.bro
  os.environ[HOLD_VARIABLE] = run.hold
  os.environ['RIDE_RUNNER_PID'] = str(os.getpid())
  if run.repo is not None and party_member() is None:
    create_bro(run.bro).provision_workspace(Path.cwd())
  clear_requested_exit_status()
  _install_credential_hooks()
  _prepare_claude_state(run)
  with _process_record(), session_broxy():
    code = harness.run_session(run)
  requested = requested_exit_status()
  return code if requested is None else requested


@reports_runtime_errors
def main(argv: list[str]) -> Optional[int]:
  args, arguments = _parse(argv)
  harness, run = _session_run(args, arguments)
  return run_session(harness, run)
