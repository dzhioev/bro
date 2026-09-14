"""the Harbor agent that rides a bro inside the task container.

Harbor constructs this class in its own process from the `import_path` a job
config names, then runs one trial through `setup()` → `run()` → the verifier.
The bro never runs here: `install()` uploads the relocatable bundle and a
credential store holding only the LLM credential, and `run()` is a single
`ride solo` executed inside the task's container — an unboxed root in the task's
own directory, on the bundle as its runtime, whose summons join its party — so
the tools under measurement are the ones the framework ships, on whichever
harness the job names.

Nothing bro-shaped is constructed or validated in this process. The environment
harbor runs in resolves `openai` 2.x through litellm while every model call
happens in the container against the bundle's `openai` 3, so the two majors
must never share an interpreter: this module imports nothing that pulls the
`openai` package in, and the bro name crosses into the container as a string
for the bundle itself to check.
"""

import asyncio
import contextlib
import shlex
import subprocess
import tempfile
from collections.abc import Generator
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Optional, override

from harbor.agents.installed.base import (
  AgentAuthenticationError,
  ApiInternalServerError,
  ApiRateLimitError,
  ApiUsageLimitError,
  BaseInstalledAgent,
  ErrorPattern,
  ModelNotFoundError,
  NetworkConnectionError,
  NonZeroAgentExitCodeError,
  UnknownApiError,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths
from harbor.models.trial.result import AgentInfo

from bro.base import credentials
from bro.benchmark.bundle import Bundle, built, default_root, workspace_root
from bro.benchmark.trial_store import TRAILS_DIRECTORY, token_totals
from bro.harness.claude import HARNESS as CLAUDE_HARNESS
from bro.llm.llm import FAILURE_CATEGORIES
from bro.llm.providers import failure_signatures, known_names, parse
from ride.harness import HARNESS_NAMES
from ride.workspace.spawn import PROCESS_TERM_GRACE
from ride.workspace.store import materialize_scoped_store

AGENT_NAME = 'bro'
DEFAULT_HARNESS = 'bro'

# the directory harbor's own `setup()` creates before it calls `install()`
INSTALL_DIR = PurePosixPath('/installed-agent')
BUNDLE = Bundle(Path(INSTALL_DIR / 'bro'))
STORE_DIR = INSTALL_DIR / 'credentials'
PGID_FILE = INSTALL_DIR / 'bro.pgid'

# harbor collects this directory into the job's results, so what a run writes
# under it is the record it leaves behind
AGENT_DIR = EnvironmentPaths.agent_dir
ACTIVITY_LOG = AGENT_DIR / 'bro.log'

COMPOSE_PROBE = ('docker', 'compose', 'version')


def benchmark_bundle() -> Bundle:
  return built(default_root(workspace_root()))


def reported_agent_name(bro: str, harness: str = DEFAULT_HARNESS) -> str:
  """the identity a trial is recorded under: the bro, and the harness where it
  is not the default one."""
  if harness == DEFAULT_HARNESS:
    return f'{AGENT_NAME}:{bro}'
  return f'{AGENT_NAME}:{bro}:{harness}'


def reported_agent_version() -> str:
  return benchmark_bundle().identity


# `FAILURE_CATEGORIES`, as harbor's retry policy speaks them
_HARBOR_EXCEPTIONS: dict[str, type[NonZeroAgentExitCodeError]] = {
  'rate-limit': ApiRateLimitError,
  'server-error': ApiInternalServerError,
  'network': NetworkConnectionError,
  'authentication': AgentAuthenticationError,
  'model-not-found': ModelNotFoundError,
  'usage-limit': ApiUsageLimitError,
  'unknown-api': UnknownApiError,
}
if set(_HARBOR_EXCEPTIONS) != FAILURE_CATEGORIES:
  raise RuntimeError(
    'the failure-category vocabulary moved; realign _HARBOR_EXCEPTIONS with '
    'bro.llm.llm.FAILURE_CATEGORIES'
  )


def _error_patterns() -> list[ErrorPattern]:
  """every registered provider's declared failure signatures, as harbor error
  classifications."""
  return [
    ErrorPattern(signature.pattern, _HARBOR_EXCEPTIONS[signature.category])
    for provider in known_names()
    for signature in failure_signatures(provider)
  ]


# seconds between the TERM and the KILL when a cancelled phase reaps the ride,
# and the same grace for the optional in-container `timeout` wrapper: a TERM ends
# the ride through its own teardown, which gives each member the process grace,
# so the KILL waits past that grace or it would orphan what the teardown was
# still ending
TERM_GRACE_SEC = int(PROCESS_TERM_GRACE) + 10

# how much of the activity log a failed run reports back to harbor: enough for
# the error classifier and the recorded failure detail, bounded because the log
# holds the bro's whole trajectory
FAILURE_LOG_TAIL_BYTES = 64 * 1024


@cache
def docker_compose_missing() -> bool:
  """whether this host lacks the `docker compose` CLI plugin, which installs
  separately from the engine.

  Only harbor's docker environment reaches containers through it, and which
  environment a job runs is harbor's choice rather than this agent's — so an
  absent plugin is reported, never refused. Probed once per process: a job
  constructs one agent per trial.
  """
  try:
    return subprocess.run(COMPOSE_PROBE, capture_output=True).returncode != 0
  except FileNotFoundError:
    return True


def bare_recipe(model_name: Optional[str]) -> Optional[str]:
  """the `--llm` recipe harbor's `model_name` carries past its provider prefix.

  `model_name` is `<provider>/<recipe>`: a registered provider's name, then the
  exact `--llm` grammar with the provider slot dropped —
  `<model>[:<effort>][+fast]` — both validated here so a bad name fails at job
  start rather than inside a graded trial. The framework receives it as
  `ride solo --llm :<recipe>`, whose empty provider slot replaces only what the
  recipe names and keeps the persona's own spec; naming the provider there
  would substitute that provider's default recipe and drop knobs such as the
  compaction threshold. So the prefix is checked and stripped rather than
  passed on.
  """
  if model_name is None:
    return None
  provider, separator, recipe = model_name.partition('/')
  if separator == '' or recipe == '' or provider not in known_names():
    raise ValueError(
      f'model {model_name!r} is not of the form <provider>/<recipe> with a '
      f'registered provider ({", ".join(known_names())})'
    )
  parse(f':{recipe}')
  return recipe


def harness_name(value: str) -> str:
  if value not in HARNESS_NAMES:
    raise ValueError(f'harness {value!r} is not one of {", ".join(HARNESS_NAMES)}')
  return value


def run_timeout(value: Any) -> Optional[int]:
  """the optional in-container ceiling, as a positive number of seconds."""
  if value is None:
    return None
  seconds = int(value)
  if seconds <= 0:
    raise ValueError(f'run_timeout_sec must be positive, got {value!r}')
  return seconds


@contextlib.contextmanager
def scoped_store(name: str) -> Generator[Path]:
  """the credential store the container resolves against, on disk for an upload.

  `name` is hydrated strictly, so an unknown or unresolvable credential fails
  here rather than inside a graded trial. The store carries a live API key, and
  the upload reads it from a file, so it lives in a private directory for no
  longer than that.
  """
  source_store = credentials.Store(credentials.default_registry(), credentials.STORE_DIR, {})
  files, _ = credentials.build_scoped_store(source_store, [name])
  with tempfile.TemporaryDirectory(prefix='bro-benchmark-store-') as scratch:
    directory = Path(scratch) / 'store'
    materialize_scoped_store(files, directory)
    yield directory


def ride_command(*, bro: str, instruction: str, harness: str, llm: Optional[str]) -> str:
  """the launch a trial's agent phase runs, for a shell in the task's directory.

  An unboxed root in that directory (`--tree "$PWD"`, the shell's own), on the
  bundle as its runtime, kept after a clean exit so its records stay for
  collection, permitted to have its summons join its party and nothing else,
  and declared a sandbox to the harness it rides.
  `llm` is the `--llm` selection as the launch takes it.
  """
  launch = [str(BUNDLE.script('ride')), 'solo', '--unboxed', '--tree']
  placement = [
    '--runtime-bundle',
    str(BUNDLE.root),
    '--revoke',
    ':party.start.boxed',
    '--grant',
    ':party.join',
    # the task container is thrown away after the verifier and its agent phase
    # runs as root: the declaration under which claude accepts
    # `--dangerously-skip-permissions` from uid 0
    '--env',
    'IS_SANDBOX=1',
    '--keep',
    '--harness',
    harness,
  ]
  if llm is not None:
    placement += ['--llm', llm]
  return f'{shlex.join(launch)} "$PWD" {shlex.join([*placement, bro, instruction])}'


def run_command(
  *, bro: str, instruction: str, harness: str, llm: Optional[str], timeout_sec: Optional[int]
) -> str:
  """the one command a trial's agent phase runs.

  The ride's activity log goes to a file rather than to harbor, which buffers the
  whole exec stream and regex-scans it: only the terminal reply travels normally,
  with a bounded tail of the log added when the run fails so the classifier and
  the recorded detail have the framework's own error text. The ride runs under
  `setsid` and publishes its process group, which is what `kill_command` reaps:
  a TERM to the group reaches the launcher, which ends the ride through its own
  teardown, joined members in their own sessions included. The session's PATH
  leads with the bundle's `claude`, the one program the runtime spawns by name
  rather than through its own scripts.
  """
  launch = ride_command(
    bro=bro, instruction=instruction, harness=harness, llm=None if llm is None else f':{llm}'
  )
  if timeout_sec is not None:
    launch = f'timeout --signal=TERM --kill-after={TERM_GRACE_SEC} {timeout_sec} {launch}'
  session = '; '.join(
    [
      f'echo $$ > {PGID_FILE}',
      f'export PATH={BUNDLE.claude_dir}:"$PATH"',
      f'exec {launch} 2> {ACTIVITY_LOG}',
    ]
  )
  return '\n'.join(
    [
      f'mkdir -p {AGENT_DIR}',
      # --wait makes setsid fork unconditionally and return the ride's own exit
      # status, so the process group the inner shell publishes is always the
      # ride's and the status is always its own
      f'setsid --wait bash -c {shlex.quote(session)} &',
      'status=0',
      'wait $! || status=$?',
      f'[ "$status" -eq 0 ] || tail -c {FAILURE_LOG_TAIL_BYTES} {ACTIVITY_LOG} >&2',
      'exit "$status"',
    ]
  )


def kill_command() -> str:
  """reap the ride's process group, TERM then KILL, always reporting success.

  The TERM is what ends the whole ride: the launcher tears its workers down,
  members outside this group among them, before it exits; the KILL is the bound
  on a launcher that does not. This runs while harbor cancels the agent phase.
  A failure raised here would replace the cancellation and abort the trial
  before the verifier grades it, so every step tolerates a group that has
  already gone.
  """
  return '\n'.join(
    [
      f'pgid=$(cat {PGID_FILE} 2>/dev/null) || exit 0',
      '[ -n "$pgid" ] || exit 0',
      'kill -TERM -"$pgid" 2>/dev/null || exit 0',
      f'for _ in $(seq {TERM_GRACE_SEC}); do',
      '  kill -0 -"$pgid" 2>/dev/null || exit 0',
      '  sleep 1',
      'done',
      'kill -KILL -"$pgid" 2>/dev/null',
      'exit 0',
    ]
  )


class BroAgent(BaseInstalledAgent):
  """a bro under test: one `ride solo …` process per trial.

  Kwargs (`--ak key=value`, or a job config's `agent.kwargs`):

  - `bro` — the registered persona to run, e.g. `terminal`
  - `llm_credential` — the credential hydrated into the container, a kind or a
    `kind+instance` name selecting a dedicated key: the LLM key on the bro
    harness, the `claude_code` setup token on the claude one
  - `harness` — the driving loop the bro rides under, `bro` unless named
  - `run_timeout_sec` — an optional in-container ceiling on the run, for an
    operator who pins the agent budget with `agent.override_timeout_sec`
  """

  # replaces the inherited list rather than extending it: harbor's defaults are
  # prose needles, and the output scanned here carries the bro's reply to a
  # third-party task instruction, which reproduces them by accident. What scans
  # instead is the registered providers' own declared failure signatures.
  ERROR_PATTERNS: ClassVar[list[ErrorPattern]] = _error_patterns()

  def __init__(
    self,
    bro: str,
    llm_credential: str,
    harness: str = DEFAULT_HARNESS,
    run_timeout_sec: Any = None,
    *args: Any,
    **kwargs: Any,
  ) -> None:
    super().__init__(*args, **kwargs)
    self._bro = bro
    self._llm_credential = llm_credential
    self._harness = harness_name(harness)
    self._run_timeout_sec = run_timeout(run_timeout_sec)
    self._llm = bare_recipe(self.model_name)
    if docker_compose_missing():
      self.logger.warning(
        'no docker compose plugin on this host: a job on the docker environment cannot '
        'start a task container without it'
      )

  @staticmethod
  @override
  def name() -> str:
    return AGENT_NAME

  @override
  def version(self) -> str:
    return reported_agent_version()

  @override
  def to_agent_info(self) -> AgentInfo:
    """the recorded identity, qualified by the bro and harness under test.

    A job keys its per-agent statistics by this name, so two agent entries
    driving different bros have to report different identities or their trials
    are summed into one.
    """
    return (
      super()
      .to_agent_info()
      .model_copy(update={'name': reported_agent_name(self._bro, self._harness)})
    )

  @override
  async def install(self, environment: BaseEnvironment) -> None:
    """upload the bundle and the credential store, then prove both in this image.

    Nothing is installed through a package manager: the task filesystem is what
    the verifier grades, and several tasks are about the Python environment the
    bundle would otherwise touch. The trailing `bro show` is the only check the
    host cannot make — it rejects an unknown bro name and smoke-tests the bundle
    in this task's own image, in the setup phase, so a misconfigured job aborts
    instead of being graded as a run of failed attempts; on the claude harness
    the bundled `claude` proves it runs there too.
    """
    bundle = benchmark_bundle()
    await self.exec_as_root(
      environment, command=f'mkdir -p {BUNDLE.root} {STORE_DIR} && chmod 700 {STORE_DIR}'
    )
    await environment.upload_dir(bundle.root, str(BUNDLE.root))
    with scoped_store(self._llm_credential) as directory:
      await environment.upload_dir(directory, str(STORE_DIR))
    # the agent phase runs as root in every task of this dataset, so the store
    # needs no chown — only the private mode the upload does not carry over
    await self.exec_as_root(environment, command=f'chmod 600 {STORE_DIR}/*')
    await self.exec_as_agent(
      environment, command=shlex.join([str(BUNDLE.script('bro')), 'show', self._bro])
    )
    if self._harness == CLAUDE_HARNESS:
      await self.exec_as_agent(environment, command=shlex.join([str(BUNDLE.claude), '--version']))

  def run_env(self) -> dict[str, str]:
    return {
      'BRO_STORE': str(STORE_DIR),
      # the ride's runtime root: its workspace records, summon audit, and trail
      # store have to stay inside the directory harbor collects, and out of the
      # filesystem the verifier grades
      'XDG_DATA_HOME': str(AGENT_DIR),
    }

  @override
  async def run(
    self, instruction: str, environment: BaseEnvironment, context: AgentContext
  ) -> None:
    command = run_command(
      bro=self._bro,
      instruction=instruction,
      harness=self._harness,
      llm=self._llm,
      timeout_sec=self._run_timeout_sec,
    )
    try:
      await self.exec_as_agent(environment, command=command, env=self.run_env())
    except asyncio.CancelledError:
      # harbor bounds the phase by cancelling this coroutine, which only kills
      # the local exec client: without this the ride keeps running inside the
      # container, spending tokens and writing to the filesystem the verifier is
      # about to grade. The cancellation is re-raised so harbor still records
      # the phase as timed out.
      await self._reap(environment)
      raise

  async def _reap(self, environment: BaseEnvironment) -> None:
    try:
      await self.exec_as_root(environment, command=kill_command())
    except Exception as error:
      # a raise here would replace the cancellation being handled and cost the
      # trial its grade; an unreapable ride is worth a line, not the result
      self.logger.warning('could not reap the ride in %s: %s', environment.session_id, error)

  @override
  def populate_context_post_run(self, context: AgentContext) -> None:
    """map the trial's recorded token classes onto harbor's three counters.

    Read from the trail store the run left under the collected agent directory,
    the one record every harness writes. Not one-to-one: harbor documents
    `n_input_tokens` as including cache, so it takes the whole prompt. Cost
    stays unset: pricing runs after the run, over the retained trails.
    """
    store_root = self.logs_dir / TRAILS_DIRECTORY
    try:
      totals = token_totals(store_root)
    except (OSError, ValueError, KeyError) as error:
      # this runs in the trial's own cleanup, where an exception would abort the
      # trial before it is graded — so an unreadable store costs the token counts
      # and nothing else
      self.logger.warning('could not read the trail store at %s: %s', store_root, error)
      return
    if totals is None:
      self.logger.debug('no trail under %s; the run recorded none', store_root)
      return
    context.n_input_tokens = totals['input'] + totals['cache_write'] + totals['cache_read']
    context.n_cache_tokens = totals['cache_read']
    context.n_output_tokens = totals['output']
