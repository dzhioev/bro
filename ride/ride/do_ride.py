"""Run one session in a prepared workspace."""

import contextlib
import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro import watches
from bro.base import credentials, log
from bro.base.args import Parser
from bro.launch.broxy import session_broxy
from bro.launch.hold import HOLD_VARIABLE
from bro.llm.llm import Recipe
from bro.llm.resolved import RESOLVED_RECIPE_ENV, ResolvedRecipe
from bro.monitor import PROCESS_FILENAME, SESSION_DIR_ENV, session_dir
from bro.summon import RUNTIME_ENV, party_member
from bro.workspace.launch_facts import LaunchFacts
from bro.workspace.paths import ISOLATION_ENV, workspace_dir
from bro.workspace.session import clear_requested_exit_status, requested_exit_status
from ride.attachments import Attachment, get_attachment_type
from ride.errors import reports_runtime_errors
from ride.identity import bro_git_identity_env
from ride.workspace.model import ACTIVITY_FILENAME

if TYPE_CHECKING:
  from ride.harness import SessionHarness
  from ride.session import SessionSpec

__cli_name__ = 'do-ride'

INSTALL_DIRECTORY_ENV = 'BRO_INSTALL_DIR'
CONTAINER_INSTALL_DIRECTORY = '/home/ride/.bro-environment'


def _boxed() -> bool:
  isolation = os.environ.get(ISOLATION_ENV)
  if isolation not in ('boxed', 'unboxed'):
    raise RuntimeError(f'{ISOLATION_ENV} must be set to boxed or unboxed')
  return isolation == 'boxed'


@dataclass(frozen=True)
class SessionRun:
  """The session fields consumed inside a prepared workspace."""

  name: str
  attachments: tuple[Attachment, ...]
  harness: str
  hold: str
  resolved_recipe: dict
  solo: bool
  resume: bool
  bro: str
  prompt: Optional[str]
  activity_file: Path

  @property
  def recipe(self) -> Recipe:
    return ResolvedRecipe.from_dict(self.resolved_recipe).recipe


def command(spec: 'SessionSpec', attachments: tuple[Attachment, ...]) -> list[str]:
  """Build the do-ride argv for a launcher's session spec."""
  verb = 'solo' if spec.solo else 'along'
  parts = [
    'do-ride',
    verb,
    '--workspace',
    spec.name,
    '--harness',
    spec.resolved_harness,
    *(['--resume'] if spec.resume else []),
  ]
  for attachment in attachments:
    parts.extend(['--attachment', json.dumps(attachment.dump())])
  parts.extend(['--hold', spec.hold, '--', spec.bro])
  if spec.prompt is not None:
    parts.append(spec.prompt)
  return parts


def _configure_mode_parser(parser: Parser, *, solo: bool) -> None:
  from bro.mcp import HOLDS
  from ride.harness import HARNESS_NAMES

  parser.add_argument('--workspace', required=True, help='prepared workspace name')
  parser.add_argument('--harness', required=True, choices=HARNESS_NAMES, help='driving harness')
  parser.add_argument(
    '--resume', action='store_true', env=False, help='resume the recorded session'
  )
  parser.add_argument('--attachment', action='append', default=[], help='recorded attachment JSON')
  parser.add_argument('--hold', required=True, choices=HOLDS, help='session user-involvement level')
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


def _parse(argv: list[str]) -> dict:
  return build_parser().parse(argv)


def encode_resolved_recipe(resolved_recipe: dict) -> str:
  return ResolvedRecipe.from_dict(resolved_recipe).encode()


def _resolved_recipe() -> dict:
  encoded = os.environ.get(RESOLVED_RECIPE_ENV)
  if encoded is None:
    raise RuntimeError(f'{RESOLVED_RECIPE_ENV} is unset: do-ride requires the admitted recipe')
  return ResolvedRecipe.from_dict(json.loads(encoded)).dump()


def _session_run(args: dict) -> tuple['SessionHarness', SessionRun]:
  from ride.harness import get_harness

  mode = args.pop('mode')
  harness_name = args.pop('harness')
  harness = get_harness(harness_name)
  name = args.pop('workspace')
  attachments = tuple(Attachment.load(json.loads(value)) for value in args.pop('attachment'))
  if len(attachments) > 1:
    raise ValueError('a session supports at most one attachment')
  run = SessionRun(
    name=name,
    attachments=attachments,
    harness=harness_name,
    solo=mode == 'solo',
    resolved_recipe=_resolved_recipe(),
    activity_file=_session_state() / ACTIVITY_FILENAME,
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


def _session_state() -> Path:
  state = session_dir()
  if state is None:
    raise RuntimeError(f'{SESSION_DIR_ENV} is unset: do-ride requires a session state directory')
  return state


@contextlib.contextmanager
def _process_record() -> Generator[None]:
  state = _session_state()
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


@contextlib.contextmanager
def _activity_marked(activity_file: Path) -> Generator[None]:
  activity_file.touch()
  try:
    yield
  finally:
    activity_file.touch()


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


def _launch_facts(run: SessionRun) -> LaunchFacts:
  from ride.runtime_bundle import frozen_hash

  runtime = os.environ.get(RUNTIME_ENV)
  if runtime is None:
    raise RuntimeError(f'{RUNTIME_ENV} is unset: do-ride requires the runtime it runs from')
  return LaunchFacts.collect(runtime=frozen_hash(Path(runtime)), recipe=run.recipe, hold=run.hold)


def run_session(harness: 'SessionHarness', run: SessionRun) -> int:
  """Run the session in this process's prepared working directory."""
  os.environ.update(bro_git_identity_env(run.bro))
  os.environ['RIDE_WORKSPACE'] = run.name
  os.environ['RIDE_BRO'] = run.bro
  os.environ[HOLD_VARIABLE] = run.hold
  os.environ['RIDE_RUNNER_PID'] = str(os.getpid())
  clear_requested_exit_status()
  _install_credential_hooks()
  if party_member() is None:
    try:
      for attachment in run.attachments:
        get_attachment_type(attachment.type).start_session(attachment, Path.cwd(), run.bro)
    except (OSError, ValueError, RuntimeError) as error:
      log.error('%s', error)
      return 1
  harness.prepare_session(run)
  with (
    _process_record(),
    _activity_marked(run.activity_file),
    session_broxy(),
    watches.Owner.for_session() as watch_owner,
    contextlib.ExitStack() as session_scope,
  ):
    if watches.session_watch_admitted():
      from bro.authority_view import capture_initial
      from bro.harness import get_harness
      from bro.prompts import save_deferred_contract

      capture_initial()
      save_deferred_contract(harness=get_harness(run.harness), hold=run.hold)
      watch_owner.store.start(watches.SESSION_WATCH_COMMAND)
      session_scope.enter_context(watch_owner.store.kept_alive(watches.SESSION_WATCH_COMMAND))
    code = harness.run_session(run, _launch_facts(run))
  requested = requested_exit_status()
  return code if requested is None else requested


@reports_runtime_errors
def main(argv: list[str]) -> Optional[int]:
  harness, run = _session_run(_parse(argv))
  return run_session(harness, run)
