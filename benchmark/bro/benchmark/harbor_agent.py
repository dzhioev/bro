"""the Harbor agent that drives a bro inside the task container.

Harbor constructs this class in its own process from the `import_path` a job
config names, then runs one trial through `setup()` → `run()` → the verifier.
The bro never runs here: `install()` uploads the relocatable bundle and a
credential store holding only the LLM key, and `run()` is a single
`bro run <bro> <instruction>` executed inside the task's container,
so the tools under measurement are the ones the framework ships.

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
  UnknownApiError,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths
from harbor.models.trial.result import AgentInfo

from bro.base import credentials
from bro.benchmark.bundle import Bundle, built, default_root, workspace_root
from bro.llm.providers import parse
from bro.llm.usage import read_usage_file
from ride.workspace.store import materialize_scoped_store

AGENT_NAME = 'bro'

# the directory harbor's own `setup()` creates before it calls `install()`
INSTALL_DIR = PurePosixPath('/installed-agent')
BUNDLE = Bundle(Path(INSTALL_DIR / 'bro'))
STORE_DIR = INSTALL_DIR / 'credentials'
PGID_FILE = INSTALL_DIR / 'bro.pgid'

# harbor collects this directory into the job's results, so what a run writes
# under it is the record it leaves behind
AGENT_DIR = EnvironmentPaths.agent_dir
ACTIVITY_LOG = AGENT_DIR / 'bro.log'
USAGE_FILE = AGENT_DIR / 'usage.json'

DEFAULT_LLM_CREDENTIAL = 'openai'
MODEL_PROVIDER = 'openai'

COMPOSE_PROBE = ('docker', 'compose', 'version')

# seconds between the TERM and the KILL when a cancelled phase reaps the bro,
# and the same grace for the optional in-container `timeout` wrapper
TERM_GRACE_SEC = 5

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

  Everything after `openai/` is the exact `--llm` grammar with the provider
  slot dropped — `<model>[:<effort>][+fast]` — validated here so a malformed
  recipe fails at job start rather than inside a graded trial. The framework
  receives it as `bro run --llm :<recipe>`, whose empty provider slot replaces
  only what the recipe names and keeps the persona's own spec; naming the
  provider there would substitute that provider's default recipe and drop
  knobs such as the compaction threshold. So the prefix is checked and
  stripped rather than passed on.
  """
  if model_name is None:
    return None
  prefix = f'{MODEL_PROVIDER}/'
  if not model_name.startswith(prefix) or model_name == prefix:
    raise ValueError(f'model {model_name!r} is not of the form {prefix}<recipe>')
  recipe = model_name[len(prefix) :]
  parse(f':{recipe}')
  return recipe


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
  files = credentials.build_scoped_store([name])
  with tempfile.TemporaryDirectory(prefix='bro-benchmark-store-') as scratch:
    directory = Path(scratch) / 'store'
    materialize_scoped_store(files, directory)
    yield directory


def run_command(
  *, bro: str, instruction: str, llm: Optional[str], timeout_sec: Optional[int]
) -> str:
  """the one command a trial's agent phase runs.

  The bro's activity log goes to a file rather than to harbor, which buffers the
  whole exec stream and regex-scans it: only the terminal reply travels normally,
  with a bounded tail of the log added when the run fails so the classifier and
  the recorded detail have the framework's own error text. The bro runs under
  `setsid` and publishes its process group, which is what `kill_command` reaps.
  """
  arguments = [str(BUNDLE.shim), 'run', bro, instruction]
  if llm is not None:
    arguments += ['--llm', f':{llm}']
  bro_command = shlex.join(arguments)
  if timeout_sec is not None:
    bro_command = f'timeout --signal=TERM --kill-after={TERM_GRACE_SEC} {timeout_sec} {bro_command}'
  session = f'echo $$ > {PGID_FILE}; exec {bro_command} 2> {ACTIVITY_LOG}'
  return '\n'.join(
    [
      f'mkdir -p {AGENT_DIR}',
      # --wait makes setsid fork unconditionally and return the bro's own exit
      # status, so the process group the inner shell publishes is always the
      # bro's and the status is always its own
      f'setsid --wait bash -c {shlex.quote(session)} &',
      'status=0',
      'wait $! || status=$?',
      f'[ "$status" -eq 0 ] || tail -c {FAILURE_LOG_TAIL_BYTES} {ACTIVITY_LOG} >&2',
      'exit "$status"',
    ]
  )


def kill_command() -> str:
  """reap the bro's process group, TERM then KILL, always reporting success.

  This runs while harbor cancels the agent phase. A failure raised here would
  replace the cancellation and abort the trial before the verifier grades it,
  so every step tolerates a group that has already gone.
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
  """a bro under test: one `bro run …` process per trial.

  Kwargs (`--ak key=value`, or a job config's `agent.kwargs`):

  - `bro` — the registered persona to run, e.g. `terminal`
  - `llm_credential` — the credential hydrated into the container, a kind or a
    `kind+instance` name selecting a dedicated key
  - `run_timeout_sec` — an optional in-container ceiling on the run, for an
    operator who pins the agent budget with `agent.override_timeout_sec`
  """

  # replaces the inherited list rather than extending it: harbor's defaults are
  # prose needles, and the output scanned here carries the bro's reply to a
  # third-party task instruction, which reproduces them by accident. These name
  # the provider exceptions the framework's own failure output carries.
  ERROR_PATTERNS: ClassVar[list[ErrorPattern]] = [
    ErrorPattern(r'openai\.RateLimitError', ApiRateLimitError),
    ErrorPattern(r'openai\.InternalServerError', ApiInternalServerError),
    ErrorPattern(r'openai\.(APIConnectionError|APITimeoutError)', NetworkConnectionError),
    ErrorPattern(r'openai\.(AuthenticationError|PermissionDeniedError)', AgentAuthenticationError),
    ErrorPattern(r'openai\.NotFoundError', ModelNotFoundError),
    ErrorPattern(r'insufficient_quota', ApiUsageLimitError),
    ErrorPattern(r'openai\.APIStatusError', UnknownApiError),
  ]

  def __init__(
    self,
    bro: str,
    llm_credential: str = DEFAULT_LLM_CREDENTIAL,
    run_timeout_sec: Any = None,
    *args: Any,
    **kwargs: Any,
  ) -> None:
    super().__init__(*args, **kwargs)
    self._bro = bro
    self._llm_credential = llm_credential
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
  def to_agent_info(self) -> AgentInfo:
    """the recorded identity, qualified by the bro under test.

    A job keys its per-agent statistics by this name, so two agent entries
    driving different bros have to report different identities or their trials
    are summed into one.
    """
    return super().to_agent_info().model_copy(update={'name': f'{AGENT_NAME}:{self._bro}'})

  @override
  async def install(self, environment: BaseEnvironment) -> None:
    """upload the bundle and the credential store, then prove both in this image.

    Nothing is installed through a package manager: the task filesystem is what
    the verifier grades, and several tasks are about the Python environment the
    bundle would otherwise touch. The trailing `bro show` is the only check the
    host cannot make — it rejects an unknown bro name and smoke-tests the bundle
    in this task's own image, in the setup phase, so a misconfigured job aborts
    instead of being graded as a run of failed attempts.
    """
    bundle = built(default_root(workspace_root()))
    await self.exec_as_root(
      environment, command=f'mkdir -p {BUNDLE.root} {STORE_DIR} && chmod 700 {STORE_DIR}'
    )
    await environment.upload_dir(bundle.root, str(BUNDLE.root))
    with scoped_store(self._llm_credential) as directory:
      await environment.upload_dir(directory, str(STORE_DIR))
    # the agent phase runs as root in every task of this dataset, so the store
    # needs no chown — only the private mode the upload does not carry over
    await self.exec_as_root(environment, command=f'chmod 600 {STORE_DIR}/*')
    await self.exec_as_agent(environment, command=shlex.join([str(BUNDLE.shim), 'show', self._bro]))

  def run_env(self) -> dict[str, str]:
    return {
      'BRO_CONFIGS_DIR': str(STORE_DIR),
      'BRO_USAGE_FILE': str(USAGE_FILE),
      # task images ship no CA store unless their own layers add one, and the
      # bundle carries certifi's
      'SSL_CERT_FILE': str(BUNDLE.ca_bundle),
      # `bro run` takes no `--no-trails` flag; the env var is its recording
      # opt-out
      'TRAILS_DISABLED': '1',
    }

  @override
  async def run(
    self, instruction: str, environment: BaseEnvironment, context: AgentContext
  ) -> None:
    command = run_command(
      bro=self._bro,
      instruction=instruction,
      llm=self._llm,
      timeout_sec=self._run_timeout_sec,
    )
    try:
      await self.exec_as_agent(environment, command=command, env=self.run_env())
    except asyncio.CancelledError:
      # harbor bounds the phase by cancelling this coroutine, which only kills
      # the local exec client: without this the bro keeps running inside the
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
      # trial its grade; an unreapable bro is worth a line, not the result
      self.logger.warning('could not reap the bro in %s: %s', environment.session_id, error)

  @override
  def populate_context_post_run(self, context: AgentContext) -> None:
    """map the framework's four token classes onto harbor's three counters.

    Not one-to-one: harbor documents `n_input_tokens` as including cache, so it
    takes the whole prompt. Cost stays unset — the framework's usage accounting
    carries counts, not prices.
    """
    usage_file = self.logs_dir / USAGE_FILE.name
    if not usage_file.is_file():
      self.logger.debug('no usage file at %s; the run published none', usage_file)
      return
    try:
      usage = read_usage_file(usage_file)
    except (OSError, ValueError, KeyError) as error:
      # this runs in the trial's own cleanup, where an exception would abort the
      # trial before it is graded — so an unreadable file costs the token counts
      # and nothing else
      self.logger.warning('could not read %s: %s', usage_file, error)
      return
    totals = {'input': 0, 'cache_write': 0, 'cache_read': 0, 'output': 0}
    for counts in usage.per_model.values():
      for token_class in totals:
        totals[token_class] += counts[token_class]
    context.n_input_tokens = totals['input'] + totals['cache_write'] + totals['cache_read']
    context.n_cache_tokens = totals['cache_read']
    context.n_output_tokens = totals['output']
