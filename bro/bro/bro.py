import asyncio
import json
import os
import traceback
from abc import ABC
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, ClassVar, Optional, Self

import llm.llms.chat_gpt
import llm.mcp
from base import credentials, log
from base.condition import Condition, Entry, SetVariable, Variables, var
from bro import scripts as script_store
from bro.channel import BroChannel
from bro.datasources.base import DataSource
from bro.summon import SUMMONER_ENV
from llm.llm import EFFORT_LEVELS, LLM, LLMSpec
from llm.observer import BoringRenderer, NullObserver, Observer
from llm.tracker import EndReason, NullTracker, ToolStepSource, Tracker
from prompts import get_prompt, hold_fragment
from trails.record.bro import Recorder

DEFAULT_LLM_SPEC: LLMSpec = llm.llms.chat_gpt.LLMSpec()


_TRAILS_DISABLED_ENV = 'TRAILS_DISABLED'


def _summoned_by_from_env() -> Optional[dict[str, Any]]:
  # consumed on read: tool subprocesses inherit this process's environment, so a
  # nested in-place run inside the summoned child's container must not re-stamp
  # the parent's summoned_by on its own trail — it was not itself summoned
  raw = os.environ.pop(SUMMONER_ENV, None)
  if raw is None:
    return None
  summoned_by = json.loads(raw)
  if not isinstance(summoned_by, dict):
    raise ValueError(f'{SUMMONER_ENV} must be a JSON object')
  keys = set(summoned_by)
  if keys == {'session'} and isinstance(summoned_by['session'], str):
    return None
  if keys == {'target', 'trail_id'} and all(isinstance(summoned_by[key], str) for key in keys):
    return {'trail_id': summoned_by['trail_id']}
  if not {'trail_id'}.issubset(keys) or not keys.issubset({'trail_id', 'step_id', 'index'}):
    raise ValueError(f'{SUMMONER_ENV} has an invalid summoned_by shape')
  trail_id = summoned_by['trail_id']
  step_id = summoned_by.get('step_id')
  index = summoned_by.get('index')
  if (
    not isinstance(trail_id, str)
    or len(trail_id) == 0
    or (
      step_id is not None
      and (not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 0)
    )
    or (
      index is not None
      and (step_id is None or not isinstance(index, int) or isinstance(index, bool) or index < 0)
    )
  ):
    raise ValueError(f'{SUMMONER_ENV} has an invalid summoned_by shape')
  return summoned_by


def _default_factory() -> Tracker:
  # explicit kill switch wins over everything: define `TRAILS_DISABLED` (to any
  # value, presence is what counts — same convention as `NO_COLOR` /
  # `CW_IN_CONTAINER`) to skip recording for a process — local dev, ad-hoc runs,
  # or deploying a fix while trails-server itself is down (recording is otherwise
  # mandatory + crash-on-failure, so a broken server blocks every bro,
  # including the devoops bro that would fix it). this only governs the default
  # factory: a per-run `tracker=` and a custom `set_default_tracker_factory(...)`
  # still take precedence.
  if os.environ.get(_TRAILS_DISABLED_ENV) is not None:
    return NullTracker()
  # recording is otherwise mandatory in production: the `trails` secret must
  # resolve (`trails/bootstrap.sh` writes `~/.ppp/trails.json`). a missing
  # secret is a setup error, not a fallback path — `NullTracker` is opt-in:
  # - kill switch: `TRAILS_DISABLED` set in the environment.
  # - tests: `conftest.py`'s `set_default_tracker_factory(NullTracker)`.
  # - one-shot exploration: `bro.run(..., surface='experiment', tracker=NullTracker())`.
  try:
    config = credentials.get_json('trails')
  except credentials.SecretNotFound as e:
    raise RuntimeError(
      'trails: secret not found; run trails/bootstrap.sh to enable '
      'recording, or pass tracker=NullTracker() to skip explicitly'
    ) from e
  return Recorder(config['base_url'], config['token'])


# default factory for the per-run `Tracker` an unconfigured bro uses. swap with
# `set_default_tracker_factory(...)` — `conftest.py` pins it to `NullTracker`
# so tests never try to record.
_default_tracker_factory: Callable[[], Tracker] = _default_factory


def set_default_tracker_factory(factory: Callable[[], Tracker]) -> None:
  global _default_tracker_factory
  _default_tracker_factory = factory


_SHARED_PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts' / 'shared'


def _load_shared_prompts() -> str:
  if not _SHARED_PROMPTS_DIR.is_dir():
    return ''
  parts = []
  for path in sorted(_SHARED_PROMPTS_DIR.glob('*.md')):
    parts.append(path.read_text().strip())
  return '\n\n'.join(parts)


def _render_data_sources(sources: list[DataSource]) -> str:
  lines = [
    '## Data sources',
    '',
    'The following read-only data sources are mounted for this session:',
    '',
  ]
  for ds in sources:
    lines.append(f'- **{ds.name}** — {ds.rendered_summary()}')
  lines.append('')
  # the namespace example uses a source this bro actually mounts, so it never
  # points at a tool the session lacks
  lines.append(
    "Each source's tools live in its own `<name>-source` namespace — the "
    f'`{sources[0].name}` tools are `{sources[0].namespace}::…`. See the tool '
    'listings for what each source exposes.'
  )
  return '\n'.join(lines)


def _render_skill_loader() -> str:
  return '\n'.join(
    [
      '## Skills',
      '',
      'Third-party skills load through `@::skill`. A user message starting with `/<name>` '
      'requests that skill: call `@::skill` with its name, then execute the returned '
      'instructions with the rest of the message as arguments. An empty body means the skill '
      'is unavailable.',
    ]
  )


def _render_scripts(*, include_dispatcher: bool) -> str:
  lines = [
    '## Scripts',
    '',
    'Scripts are named procedures exposed as canonical `@::` tools. To run one, call its '
    'tool and execute the returned instructions.',
  ]
  if include_dispatcher:
    lines.extend(
      [
        '',
        'Text enclosed as `@:<free text>:@` is a natural-language script command. Call '
        '`@::@` with `<free text>` and execute the returned script instructions.',
      ]
    )
  return '\n'.join(lines)


class BroRaised(llm.mcp.ToolControlSignal):
  """aborts a Bro run: raised by the `raise` service tool, and by the run-start
  credential gate when required secrets don't resolve."""

  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


def _raise(reason: str) -> str:
  raise BroRaised(reason)


async def _claude_raise(reason: str) -> str:
  # no exception can abort the consuming claude session, so record the abort
  # over the broker channel where one exists, then terminate the session (the
  # workspace layer owns the mechanics). blocking ops, so off-loop; the finally
  # keeps the kill unconditional.
  from workspace.session import terminate_session

  def record_and_kill() -> None:
    log.warning('raise: %s', reason)
    try:
      channel = BroChannel.from_env()
      if channel is not None:
        channel.completed(reason, 'raised')
        channel.close()
    finally:
      terminate_session()

  await asyncio.to_thread(record_and_kill)
  # unreachable in practice — claude dies awaiting this result
  return 'the abort is recorded and the session is being terminated. Stop working now.'


_RAISE_DESCRIPTION = (
  'abort the run because the request cannot be fulfilled. Call this when '
  'required credentials or API keys are missing, no appropriate tool or data '
  'source is available, the request contains contradictory constraints, the '
  'input is unclear or cannot be understood (gibberish, ambiguous, or missing '
  'the context needed to act), or any other blocker prevents completing the '
  'task. Do NOT reply with a clarifying question — there is no follow-up turn; '
  'raise instead. Pass a clear, specific reason — it surfaces to the caller as '
  'the failure cause.'
  '{{when #wire = mcp}} The call records the abort and terminates the session; '
  'nothing after it will run, so make the reason self-contained.{{end}}'
)


def _raise_tool(wire: llm.mcp.Wire, variables: Variables) -> llm.mcp.Tool:
  target = _raise if wire == 'bare' else _claude_raise
  return llm.mcp.FunctionTool(
    target, name='raise', description=_RAISE_DESCRIPTION, variables=variables
  )


# the {{when #wire = mcp}} blocks render only into the MCP-served builds
# (persona and --raw claude sessions consume the toolset over streamable HTTP,
# where the harness bounds a silent tool call at MCP_TOOL_TIMEOUT — short of a
# real child's runtime, so the blocking modes are a trap there); the in-process
# builds (wire 'bare') have no transport to die on and render the plain text.
# the lost-request-id recovery path forks on `#tools`: `summon_list` mounts
# only when the session tracks summon status.
_SUMMON_DESCRIPTION = (
  'summon another bro: it runs your prompt in its own isolated container with its '
  'own credentials and this call blocks — typically for minutes — until its answer '
  'comes back. pass `target` (a bro name; you have your own summon allow-list, and '
  'a target outside it — or a summon nested past the depth cap — fails immediately '
  'with the reason) and `prompt` (the full request, self-contained — the target '
  'shares no context with you). optional `timeout` (seconds, default 1800) bounds '
  'the run — an open-ended child (e.g. a dev run watching a PR through review) '
  'outlives the default and needs an explicit value sized in hours; optional '
  "`into` bases the child on a git ref instead of your workspace's "
  'current HEAD (uncommitted changes never transfer); optional `hold` sets the '
  "child's user-involvement level (default unattended). the child's run is shaped "
  'by the optional `effort` (reasoning level: '
  f'{", ".join(EFFORT_LEVELS)}) and `fast` (the provider fast knob), and its scope by '
  'the optional `grant` / `revoke` lists — each entry a credential name, or `@bro` '
  "for a summonable target of the child's own. you can only grant what you hold "
  'yourself (a credential in your own scope, a bro in your own allow-list), and both '
  "directions are strict, so naming something the child's scope already has (or, for "
  'a revoke, lacks) fails the summon. '
  'fails with the reason when the run raises, errors out, '
  'or dies. `detach: true` returns the request id right after the send instead of '
  'blocking — poll or collect it with `summon_check`.'
  '{{when #wire = mcp}} CAUTION: this tool is served over MCP, and the harness '
  'times a silent tool call out after ten minutes while a child working a real '
  'task typically runs longer — such a blocking summon dies client-side with a '
  'transport timeout while the child keeps running, and the reply (with the '
  'request id) is lost with the call. an ask that answers within the budget '
  'can block; for a real task, pass `detach: true` and poll with summon_check. '
  'if a blocking call did time out, do NOT re-summon — the child keeps running'
  '{{iff #tools contains summon_list}}: recover the request id with summon_list '
  'and reattach via summon_check (`last_seen: 0` re-reads a result that was '
  'already delivered to the dead call){{else}}; this session tracks no summon '
  'status, so a lost request id cannot be rediscovered — if you no longer have '
  'it, surface the timeout instead of retrying{{end}}.{{end}}'
)


_SUMMON_CHECK_DESCRIPTION = (
  'check on a detached or interrupted summon by its request id. by default a '
  'non-blocking peek: returns `{state: completed, answer}` once an unread result '
  'is in, `{state: pending, trail_id?, seq?}` while the child still runs — it '
  'consumes nothing and disturbs no concurrent waiter, so polling is safe and '
  'repeatable — and `{state: collected, seq?}` when the result was already read. '
  'optional `last_seen` (a sequence number; 0 = the start) re-reads the '
  'conversation from that point regardless of read status — the recovery path '
  'when a result was read by a wait whose reply never reached you; the response '
  '`seq` is your new cursor. `wait: true` blocks until the answer; the wait is a '
  'lock, so it fails right away when another process is already waiting on the '
  'id, and errors once the result was collected. optional `timeout` (seconds, '
  'only with `wait`) bounds that wait. fails with the reason when the id is '
  'unknown or when the summon failed.'
  '{{when #wire = mcp}} CAUTION: this tool is served over MCP, and the harness '
  'times a silent tool call out after ten minutes — a `wait: true` on a longer '
  'run dies client-side the same way; prefer non-blocking polls, and recover '
  'from a died wait with `last_seen: 0`.{{end}}'
)


_SUMMON_LIST_DESCRIPTION = (
  "list this session's summons as the host recorded them: `active` entries "
  '(request_id, target, trail_id, started_at) and `last` — the most recent '
  'finished one (request_id, target, trail_id, outcome). use it to rediscover a '
  'request id you lost — e.g. a blocking summon whose reply never reached you — '
  'then reattach with summon_check.'
)


_BANNER_DESCRIPTION = (
  "return this session's environment facts as `key: value` lines: `kind` "
  '(docker container vs host worktree), workspace name and paths, the bro '
  'persona, and the launch command. call it once at session start to detect '
  'your environment.'
)


def _banner_tool(bro_name: str, variables: Variables) -> llm.mcp.Tool:
  # the same facts `cw banner --llm` prints, rendered in-process. the bro name is
  # passed explicitly because an in-process run's environment carries the
  # launcher's CW_BRO (or none), not this bro's. the workspace import stays
  # function-local so `import bro` stays cheap.
  def _banner() -> str:
    from workspace.banner import render_banner

    return render_banner(llm=True, bro=bro_name)

  return llm.mcp.FunctionTool(
    _banner, name='banner', description=_BANNER_DESCRIPTION, variables=variables
  )


def _summon_tool(
  variables: Variables, current_tool_step_id: Callable[[], Optional[ToolStepSource]]
) -> llm.mcp.Tool:
  # a fresh channel client per call, opened on the loop and closed in `finally`
  # so a cancelled tool call (the MCP client timed out or aborted) unblocks the
  # off-loop wait: the broxy sees the waiter go, and the terminal buffers for a
  # later summon_check instead of feeding an abandoned thread. the blocking wait
  # runs off-loop so an interactive surface stays responsive under a long summon.
  # `current_tool_step_id` names the summon call's projected source (None on
  # surfaces without an in-process tracker), so the child's `summoned_by` can
  # carry the precise fork position.
  from bro import summon as summon_client

  async def _summon(
    target: str,
    prompt: str,
    timeout: Optional[float] = None,
    into: Optional[str] = None,
    detach: bool = False,
    hold: Optional[str] = None,
    grant: Optional[list[str]] = None,
    revoke: Optional[list[str]] = None,
    effort: Optional[str] = None,
    fast: bool = False,
  ) -> str:
    source = current_tool_step_id()
    step_id = source['step_id'] if source is not None else None
    index = source['index'] if source is not None else None
    if detach:
      return await asyncio.to_thread(
        summon_client.summon_detached,
        target,
        prompt,
        timeout=timeout,
        into=into,
        hold=hold,
        grant=grant,
        revoke=revoke,
        effort=effort,
        fast=fast,
        step_id=step_id,
        index=index,
      )
    client = summon_client.open_client()
    try:
      return await asyncio.to_thread(
        summon_client.summon_and_wait,
        target,
        prompt,
        timeout=timeout,
        into=into,
        hold=hold,
        grant=grant,
        revoke=revoke,
        effort=effort,
        fast=fast,
        step_id=step_id,
        index=index,
        client=client,
      )
    finally:
      client.close()

  return llm.mcp.FunctionTool(
    _summon, name='summon', description=_SUMMON_DESCRIPTION, variables=variables
  )


def _summon_list_tool(variables: Variables) -> llm.mcp.Tool:
  from bro import summon as summon_client

  async def _summon_list() -> dict[str, Any]:
    return await asyncio.to_thread(summon_client.list_summons)

  return llm.mcp.FunctionTool(
    _summon_list, name='summon_list', description=_SUMMON_LIST_DESCRIPTION, variables=variables
  )


def _summon_check_tool(variables: Variables) -> llm.mcp.Tool:
  # the wait path owns its client like _summon_tool, for the same cancellation
  # abort; the plain peek is answered locally and immediately, so it keeps the
  # per-call client inside the worker thread.
  from bro import summon as summon_client

  async def _summon_check(
    request_id: str,
    wait: bool = False,
    timeout: Optional[float] = None,
    last_seen: Optional[int] = None,
  ) -> dict[str, Any]:
    if wait:
      if last_seen is not None:
        raise ValueError('last_seen is a cursor read; it does not combine with wait')
      client = summon_client.open_client()
      try:
        answer = await asyncio.to_thread(
          summon_client.collect_summon, request_id, timeout=timeout, client=client
        )
      finally:
        client.close()
      return {'state': 'completed', 'answer': answer}
    if timeout is not None:
      raise ValueError('timeout only bounds a wait; a plain check never blocks')
    status = await asyncio.to_thread(summon_client.check_summon, request_id, last_seen=last_seen)
    if status.pending:
      pending: dict[str, Any] = {'state': 'pending'}
      if status.trail_id is not None:
        pending['trail_id'] = status.trail_id
      if status.seq is not None:
        pending['seq'] = status.seq
      return pending
    if status.answer is None:  # collected: the conversation ended, its result was read
      collected: dict[str, Any] = {
        'state': 'collected',
        'hint': 'the result was already read; re-read the conversation with last_seen: 0',
      }
      if status.seq is not None:
        collected['seq'] = status.seq
      return collected
    completed: dict[str, Any] = {'state': 'completed', 'answer': status.answer}
    if status.seq is not None:
      completed['seq'] = status.seq
    return completed

  return llm.mcp.FunctionTool(
    _summon_check, name='summon_check', description=_SUMMON_CHECK_DESCRIPTION, variables=variables
  )


# the service roster's tool names — the closed `#tools` universe the service
# descriptions render against
_SERVICE_TOOL_NAMES = ('banner', 'raise', 'summon', 'summon_check', 'summon_list')


def _build_service_server(
  bro: 'BaseBro', *, include_raise: bool, wire: llm.mcp.Wire
) -> llm.mcp.MCPServer:
  # the roster is decided by the caller's surface and local process state:
  # `banner` is unconditional; `raise` only makes sense non-interactively (a
  # caller to abort to — interactive callers pass include_raise=False);
  # `summon`/`summon_check` need a broker channel and `summon_list` the session's
  # summon-status file on top. the decided roster then feeds the tools'
  # rendering vocabulary: service tools are harness features, the one tool
  # surface that conditions on system facts, so `#wire` is injected next to the
  # `#tools` roster.
  has_broker = os.environ.get('BROKER_CHANNEL') is not None
  has_summon_list = False
  if has_broker:
    from bro import summon as summon_module

    has_summon_list = os.environ.get(summon_module.STATUS_ENV) is not None

  mounted = ['banner']
  if include_raise:
    mounted.append('raise')
  if has_broker:
    mounted.extend(['summon', 'summon_check'])
  if has_summon_list:
    mounted.append('summon_list')
  variables: Variables = {
    **llm.mcp.surface_variables(wire=wire),
    'tools': SetVariable(frozenset(mounted), universe=frozenset(_SERVICE_TOOL_NAMES)),
  }

  tools: list[llm.mcp.Tool] = [_banner_tool(bro.name, variables)]
  if include_raise:
    tools.append(_raise_tool(wire, variables))
  if has_broker:
    # read at call time from the run's live tracker; a serving process where the
    # bro never runs (the session MCP server) keeps the NullTracker's None
    tools.append(_summon_tool(variables, lambda: bro._tracker.current_tool_step_id))
    tools.append(_summon_check_tool(variables))
    if has_summon_list:
      tools.append(_summon_list_tool(variables))
  assert [tool.name for tool in tools] == mounted
  server = llm.mcp.InProcessMCPServer('bro', tools)
  server.tool_universe = _SERVICE_TOOL_NAMES
  return server


def _unattended_claude_session() -> bool:
  # BRO_HOLD carries the session's user-involvement level, CW_RUNNER_PID makes
  # it terminatable (both exported by cw's in-place runner); `raise` needs an
  # unattended session and a runner to signal.
  return os.environ.get('BRO_HOLD') == 'unattended' and os.environ.get('CW_RUNNER_PID') is not None


def feature(name: str) -> Condition:
  """membership condition on the bro's `#features` vocabulary — the code
  spelling of the `#features contains <name>` directive, for gating
  `mcp_servers` / `data_sources` entries: `when(feature('brog'), brog.mcp)`."""
  return var('features').contains(name)


def _feature_variables(features: dict[str, tuple[str, ...]]) -> Variables:
  # membership probes the gating secrets with `available`, deliberately not the
  # `#creds` fact — see `reference/conditions.md` "Bro features" for why a
  # scoped store breaks the latter
  def enabled(name: str) -> bool:
    return all(credentials.available(secret) for secret in features[name])

  return {'features': SetVariable(enabled, universe=frozenset(features))}


def _component_needed_secrets(component: llm.mcp.MCPServerSpec | DataSource) -> set[str]:
  # a component declares its credentials as plain metadata (a spec field, or a
  # DataSource class attribute), so reading the manifest never builds a live
  # server. no real component extends a non-empty base's declaration, so an MRO
  # union would be identical.
  return set(component.needed_secrets)


def _component_optional_secrets(component: llm.mcp.MCPServerSpec | DataSource) -> set[str]:
  # mirror of `_component_needed_secrets` for the best-effort tier (`optional_secrets`).
  return set(component.optional_secrets)


class BaseBro(ABC):
  name: str
  description: str
  llm_spec: LLMSpec = DEFAULT_LLM_SPEC
  # an entry may be wrapped with `base.condition.when(...)` / grouped with
  # `iff(...)` to gate it on the assembling surface's facts (`#harness`,
  # `#creds`); a wrapped entry whose condition does not hold never mounts and
  # its spec never builds. an `mcp_servers` entry is a tool-pack module
  # (`flow.mcp` — its conventional `spec` Toolset, the full roster), a bare
  # `Toolset`, or an `MCPServerSpec` from a scoping call
  # (`flow.mcp.spec('add_task')`); see `llm.mcp.as_spec`.
  data_sources: ClassVar[list[Entry[DataSource]]] = []
  mcp_servers: ClassVar[list[Entry[llm.mcp.MCPServerSpec | llm.mcp.Toolset[Any] | ModuleType]]] = []
  # named optional capabilities: feature name → the secrets that must all
  # resolve for the feature to be on (empty tuple = unconditionally on). one
  # declaration switches every consuming site together: components gate via
  # `when(feature('<name>'), …)`, static text via `{{iff #features contains
  # <name>}}` — so a gated component enters the manifest, mounts, and renders
  # its text only where its gates resolve. MRO-walked like `mcp_servers`, with
  # derived classes overriding parents per name — `{'<name>': ()}` pins an
  # inherited feature on, turning its components into hard requirements.
  features: ClassVar[dict[str, tuple[str, ...]]] = {}
  # credentials no component expresses — the escape hatch for a bro's environment
  # needs (ppp-dev → `github`; devoops → `aws`). MRO-walked and unioned like
  # `mcp_servers`, so a subclass declares only what it adds. folded into
  # `needed_secrets()`.
  extra_secrets: tuple[str, ...] = ()
  # bros this bro may summon — its static outgoing allow-list. root sessions get
  # it adjusted per session by `--grant @bro`/`--revoke @bro`; a summoned child
  # follows the bare seeds, so summons chain transitively through seeded bros
  # under the host's depth cap (see bro/launch/summon_control.py). MRO-walked and unioned like
  # `extra_secrets`. ppp-dev seeds `devoops`; everyone else is empty (grows by
  # precedent).
  may_summon: tuple[str, ...] = ()
  # whether the bro does docker work (building/pushing images for deploys) and so
  # needs the host docker socket. an explicit capability, inherited normally. the
  # host grants `/var/run/docker.sock` to a `--raw`/bro-run container only when this
  # is set (claude code sessions get it unconditionally); see bro/launch/scope.py.
  needs_docker: bool = False
  # subclasses declare their own `system_prompt = "..."` as a class attribute;
  # `__init__` walks the MRO from base to derived and concatenates each class's
  # own contribution. so `PPPDev(Dev)` only needs to declare what PPPDev adds —
  # Dev's prompt (and Bro's) are picked up automatically. same for
  # `mcp_servers` and `data_sources`. inherit directly from BaseBro to opt out
  # of the concrete `Bro`'s shared defaults.
  system_prompt: str = ''
  # the bro's own class prompts (MRO-concatenated); set in __init__
  persona: str
  # `system_prompt` with the Claude-Code tool-name rule in place of the
  # bro-native one; set in __init__, consumed by `cw ss --raw`
  claude_system_prompt: str

  _llm: Optional[LLM] = None

  def __init__(self, system_prompt: Optional[str] = None):
    mcp_entries: list[Entry[llm.mcp.MCPServerSpec | llm.mcp.Toolset[Any] | ModuleType]] = []
    data_source_entries: list[Entry[DataSource]] = []
    prompt_parts: list[str] = []
    extra_secret_names: list[str] = []
    may_summon_names: list[str] = []
    feature_gates: dict[str, tuple[str, ...]] = {}
    for cls in reversed(type(self).__mro__):
      raw_mcp = cls.__dict__.get('mcp_servers')
      if raw_mcp is not None:
        mcp_entries.extend(raw_mcp)
      raw_sources = cls.__dict__.get('data_sources')
      if raw_sources is not None:
        data_source_entries.extend(raw_sources)
      raw_prompt = cls.__dict__.get('system_prompt')
      if isinstance(raw_prompt, str) and len(raw_prompt) > 0:
        prompt_parts.append(raw_prompt)
      raw_extra = cls.__dict__.get('extra_secrets')
      if raw_extra is not None:
        extra_secret_names.extend(raw_extra)
      raw_summon = cls.__dict__.get('may_summon')
      if raw_summon is not None:
        may_summon_names.extend(raw_summon)
      raw_features = cls.__dict__.get('features')
      if raw_features is not None:
        feature_gates.update(raw_features)
    self._extra_secrets: tuple[str, ...] = tuple(extra_secret_names)
    self._may_summon: tuple[str, ...] = tuple(may_summon_names)
    self._features: dict[str, tuple[str, ...]] = feature_gates
    # the membership probe is lazy, so the vocabulary built here stays current
    # with the store — only selection (below) bakes feature truth in.
    self._feature_vocabulary: Variables = _feature_variables(feature_gates)
    # the raw declaration entries, kept for per-harness selection
    # (_components_for); the bro-harness selection is materialized eagerly —
    # the prompt compositions below and the live-server cache read it. wire is
    # not a fact — component inclusion is wire-independent (the wire only
    # spells tool names).
    self._mcp_entries = mcp_entries
    self._data_source_entries = data_source_entries
    surface_creds = credentials.known_names()
    self._mcp_specs: list[llm.mcp.MCPServerSpec] = [
      llm.mcp.as_spec(entry)
      for entry in llm.mcp.select(
        mcp_entries, harness='bro', creds=surface_creds, extra=self._feature_vocabulary
      )
    ]
    self._data_sources: list[DataSource] = llm.mcp.select(
      data_source_entries, harness='bro', creds=surface_creds, extra=self._feature_vocabulary
    )
    # built lazily by _live_mcp_servers(): metadata surfaces (needed_secrets on
    # hosts, prompt composition) never construct live servers.
    self._live_mcp: Optional[list[llm.mcp.MCPServer]] = None
    # lazy for the same reason: building service FunctionTools derives their
    # schemas, which imports the mcp/fastmcp stack (~1s) — metadata surfaces
    # never pay it.
    self._service_server_cache: Optional[llm.mcp.MCPServer] = None
    self._llm = None
    # default to no-op; BaseBro.run() swaps in a real observer per invocation so the
    # LLM construction path picks it up via self._observer.
    self._observer: Observer = NullObserver()
    # sibling of _observer — the tracker records the run for offline analysis
    # rather than rendering it to stderr. swapped in BaseBro.run() / .send() the
    # same way _observer is.
    self._tracker: Tracker = NullTracker()
    # the id of the trail the current run records to — set when the trail opens
    # (first send / run start, or by bro.fork on a preseeded bro); None until
    # then and when recording is off. surfaces read it to point the user at the
    # recorded conversation (e.g. `call`'s resume hint).
    self.trail_id: Optional[str] = None
    self._lifetime_active = False
    self._last_end_reason: Optional[EndReason] = None
    self._last_end_detail: Optional[str] = None
    # explicit `system_prompt=...` arg overrides MRO collection — escape hatch
    # for callers that need a dynamic prompt (e.g. PM injects current time).
    if system_prompt is not None:
      prompt_parts = [system_prompt] if len(system_prompt) > 0 else []
    # the bro's own persona: MRO-concatenated class system_prompt(s) under a
    # `# Persona: <name>` heading — the segment lands inside larger composed
    # prompts (below, and cw's append prompt), where headingless identity text
    # reads as a stray fragment. no shared / data-source / scripts blocks here;
    # injected into dive-in Claude Code sessions (cw/system_prompt.py) so they
    # carry the bro's policies outside --raw mode.
    self.persona = (
      '\n\n'.join([f'# Persona: {self.name}', *prompt_parts]) if len(prompt_parts) > 0 else ''
    )
    shared = _load_shared_prompts()
    script_instructions = self.script_instructions()

    def compose(wire: llm.mcp.Wire) -> str:
      parts = []
      if len(shared) > 0:
        parts.append(shared)
      if len(self.persona) > 0:
        parts.append(self.persona)
      parts.append(get_prompt('tool_names.md').strip())
      if len(self._data_sources) > 0:
        parts.append(_render_data_sources(self._data_sources))
      if len(script_instructions) > 0:
        parts.append(script_instructions)
      parts.append(_render_skill_loader())
      # last, so it sits at the end of the prompt where instruction recency is
      # strongest; the file's directives scope it to the claude-bare surface.
      parts.append(get_prompt('grounding.md').strip())
      # both composed flavors serve the bro harness; only the wire scheme differs.
      # stripped: a fragment whose whole body is a skipped directive block
      # (grounding.md outside the claude-bare surface) collapses to bare join
      # separators at the prompt edge.
      return llm.mcp.render_text(
        '\n\n'.join(parts),
        harness='bro',
        wire=wire,
        creds=credentials.known_names(),
        extra=self._feature_vocabulary,
      ).strip()

    self.system_prompt = compose('bare')
    # the same prompt over mcp wire names — what a `cw ss --raw` session passes
    # as --system-prompt (cw/claude_argv.py).
    self.claude_system_prompt = compose('mcp')

  @property
  def agent(self) -> str:
    # the surface identity stamped on published usage (the usage file and, from
    # there, commit footers): bro runs are namespaced under bro// so the token
    # reads as a bro surface next to identities like 'Claude Code <version>'.
    return f'bro//{self.name}'

  def vocabulary(self) -> Variables:
    """the bro's own rendering vocabulary — `#features` over the declared
    feature names. merged (as `extra`) next to the surface facts wherever this
    bro's declarations or text evaluate; the bro counterpart of
    `DataSource.vocabulary`. Any surface that renders the raw `persona` must
    pass it too — the class prompts may carry `#features` directives."""
    return self._feature_vocabulary

  @property
  def scripts(self) -> dict[str, Path]:
    return script_store.collect_scripts(list(reversed(type(self).__mro__)))

  def get_script_body(self, name: str, *, harness: llm.mcp.Harness, wire: llm.mcp.Wire) -> str:
    path = self.scripts.get(name)
    if path is None:
      available = ', '.join(sorted(self.scripts)) if len(self.scripts) > 0 else '(none)'
      raise KeyError(f'no script named {name!r}; available: {available}')
    script = script_store.load_script(name, path)
    return llm.mcp.render_text(
      script.body,
      harness=harness,
      wire=wire,
      creds=credentials.known_names(),
      extra=self._feature_vocabulary,
    ).strip()

  def script_descriptions(self) -> list[tuple[str, str]]:
    return [
      (name, script_store.load_script(name, path).description)
      for name, path in self.scripts.items()
    ]

  def script_instructions(self) -> str:
    if len(self.script_descriptions()) == 0:
      return ''
    return _render_scripts(include_dispatcher=script_store.dispatcher_available())

  def _components_for(
    self, harness: llm.mcp.Harness
  ) -> tuple[list[llm.mcp.MCPServerSpec], list[DataSource]]:
    # the declared components that hold on `harness`. the bro-harness selection
    # is the one materialized in __init__ (prompt composition and the
    # live-server cache read it); any other harness selects on demand from the
    # raw entries.
    if harness == 'bro':
      return self._mcp_specs, self._data_sources
    surface_creds = credentials.known_names()
    specs = [
      llm.mcp.as_spec(entry)
      for entry in llm.mcp.select(
        self._mcp_entries, harness=harness, creds=surface_creds, extra=self._feature_vocabulary
      )
    ]
    sources: list[DataSource] = llm.mcp.select(
      self._data_source_entries,
      harness=harness,
      creds=surface_creds,
      extra=self._feature_vocabulary,
    )
    return specs, sources

  def needed_secrets(self, harness: llm.mcp.Harness = 'bro') -> tuple[str, ...]:
    # the bro's component credential manifest for a consuming harness: the union
    # of each declared MCP server's + data source's `needed_secrets`, over only
    # the components that hold on `harness` — a surface never hydrates a secret
    # of a component it doesn't mount — plus the bro's MRO-collected
    # `extra_secrets`. NOT the LLM key — that is added only by surfaces that run
    # the bro as an LLM process (`bro run` / `bro chat`); a claude-code session themed as
    # the bro uses its own auth, not the bro's spec. the host hydrates the
    # per-surface set into a scoped store; a secret used but not declared
    # surfaces as SecretNotFound — an under-declaration to fix.
    specs, sources = self._components_for(harness)
    names: set[str] = set()
    for spec in specs:
      names.update(_component_needed_secrets(spec))
    for ds in sources:
      names.update(_component_needed_secrets(ds))
    names.update(self._extra_secrets)
    return tuple(sorted(names))

  def optional_secrets(self, harness: llm.mcp.Harness = 'bro') -> tuple[str, ...]:
    # the bro's best-effort credential tier: the union of each declared MCP
    # server's + data source's `optional_secrets` over the same per-harness
    # component set as `needed_secrets`, plus the dispatcher key when this bro has
    # scripts. minus anything already required — a hard requirement is never
    # downgraded. absent optional secrets degrade the capability instead of
    # failing the launch.
    specs, sources = self._components_for(harness)
    names: set[str] = set()
    for spec in specs:
      names.update(_component_optional_secrets(spec))
    for ds in sources:
      names.update(_component_optional_secrets(ds))
    if len(self.scripts) > 0:
      names.add(script_store.DISPATCHER_SECRET)
    return tuple(sorted(names - set(self.needed_secrets(harness))))

  def missing_secrets(self) -> tuple[str, ...]:
    # every required name — the component manifest plus the LLM key, since
    # run()/send() execute the bro as an LLM process — that does not resolve in
    # this process's credential store. the optional tier is never gated.
    required = set(self.needed_secrets()) | set(self.llm_spec.needed_secrets())
    return tuple(sorted(name for name in required if not credentials.available(name)))

  def _start_refusal(self) -> Optional[str]:
    # the run-start credential gate: the refusal listing every missing secret,
    # or None to start. checked before any machinery (tracker, LLM, live
    # servers) so a missing secret surfaces at start, not mid-run at first use;
    # each surface delivers it per its mode — run() raises and send() returns it.
    missing = self.missing_secrets()
    if len(missing) == 0:
      return None
    return f'{self.name} cannot start: missing credentials: {", ".join(missing)}'

  def _start(
    self,
    input: str,
    *,
    interactive: bool,
    hold: str,
    observer: Optional[Observer],
    tracker: Optional[Tracker],
    surface: str,
    summoned_by: Optional[dict[str, Any]],
  ) -> tuple[LLM, list[dict], str]:
    # the shared start sequence of run() and send(): lock in observer/tracker —
    # caller-supplied ones win (CLIs use this to force --boring and tests inject
    # recording fakes), set on self before _create_llm so the
    # LLM construction path picks them up — then build the LLM, compose the
    # hold prompt, open the trail, and seed the message list.
    self._observer = observer if observer is not None else self._make_observer()
    self._tracker = tracker if tracker is not None else self._make_tracker()
    llm = self._create_llm(hold=hold)
    system_prompt = self._system_prompt_for(hold=hold)
    trail_id = self._tracker.start_trail(
      bro=self.name,
      llm_spec=self.llm_spec.dump(),
      system_prompt=system_prompt,
      forked_from=None,
      interactive=interactive,
      surface=surface,
      hold=hold,
      summoned_by=summoned_by,
    )
    self.trail_id = trail_id if len(trail_id) > 0 else None
    messages = [
      {'role': 'system', 'content': system_prompt},
      {'role': 'user', 'content': input},
    ]
    return llm, messages, trail_id

  def __enter__(self) -> Self:
    if self._lifetime_active:
      raise RuntimeError('bro lifetime is already active')
    self._lifetime_active = True
    self._last_end_reason = None
    self._last_end_detail = None
    return self

  def __exit__(
    self,
    exception_type: Optional[type[BaseException]],
    exception: Optional[BaseException],
    exception_traceback: Optional[TracebackType],
  ) -> bool:
    del exception_type, exception_traceback
    if self._lifetime_active is not True:
      raise RuntimeError('bro lifetime is not active')

    reason: EndReason = 'ok'
    detail: Optional[str] = None
    if isinstance(exception, BroRaised):
      reason = 'raised'
      detail = exception.reason
    elif isinstance(exception, Exception):
      reason = 'error'
      detail = str(exception)
      self._record_error_step(exception)

    self._lifetime_active = False
    self._last_end_reason = reason
    self._last_end_detail = detail
    log.verbose('bro lifetime ended: %s', reason)
    self._tracker.end_trail(reason, detail=detail)
    return False

  @classmethod
  def create(cls, llm_spec: LLMSpec) -> Self:
    # factory for a construction-time LLMSpec override — applied after the bro's
    # own __init__, so subclass constructors never need to know about it. the
    # spec replaces the class default in full; build a new spec (or call
    # `spec.fast()`) if you want to tweak a single knob on top of the bro's
    # defaults.
    bro = cls()
    bro.llm_spec = llm_spec
    return bro

  async def run(
    self,
    input: str,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    request_timeout: Optional[float] = None,
    *,
    surface: str,
    hold: str = 'unattended',
  ) -> str:
    refusal = self._start_refusal()
    if refusal is not None:
      raise BroRaised(refusal)
    llm, messages, trail_id = self._start(
      input,
      interactive=False,
      hold=hold,
      observer=observer,
      tracker=tracker,
      surface=surface,
      summoned_by=_summoned_by_from_env(),
    )
    log.info('run started%s', f' (trail {trail_id})' if len(trail_id) > 0 else '')
    channel = self._make_channel()
    if channel is not None:
      channel.started(trail_id)
    result: Optional[str] = None
    try:
      with self:
        result = await llm.send(messages, request_timeout=request_timeout)
        return result
    finally:
      if channel is not None:
        if self._last_end_reason is None:
          raise RuntimeError('run lifetime ended without an outcome')
        channel_result = result if self._last_end_reason == 'ok' else self._last_end_detail
        channel.completed(channel_result, self._last_end_reason)
        channel.close()

  async def send(
    self,
    message: str,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    request_timeout: Optional[float] = None,
    *,
    surface: str,
    hold: str = 'guided',
  ) -> str:
    if self._llm is None:
      refusal = self._start_refusal()
      if refusal is not None:
        # in-reply report; the LLM stays unbuilt, so a later send re-checks
        return refusal
      # the tracker is locked in on first send (the LLM is constructed once and
      # records one trail); later calls can't swap it. surface (the trail
      # header's surface label — `call`, `process-inbox`) and hold are locked
      # in the same way.
      self._llm, messages, _ = self._start(
        message,
        interactive=True,
        hold=hold,
        observer=observer,
        tracker=tracker,
        surface=surface,
        summoned_by=None,
      )
    else:
      if observer is not None:
        # unlike the tracker, the observer is rebindable mid-conversation: a
        # preseeded bro (bro.fork) built its LLM before the interactive surface
        # existed, so the surface attaches its renderer on its first send.
        self._observer = observer
        self._llm.observer = observer
      messages = [{'role': 'user', 'content': message}]
    return await self._llm.send(messages, request_timeout=request_timeout)

  def _servers_with_at_tools(
    self,
    servers: list[llm.mcp.MCPServer],
    *,
    harness: llm.mcp.Harness,
    wire: llm.mcp.Wire,
  ) -> list[llm.mcp.MCPServer]:
    if any(server.namespace == script_store.NAMESPACE for server in servers):
      raise ValueError(f'namespace {script_store.NAMESPACE!r} is reserved for bro framework tools')
    if harness == 'claude' and len(self.scripts) == 0:
      return servers
    return [*servers, script_store.build_server(self, harness=harness, wire=wire)]

  @property
  def _service_server(self) -> llm.mcp.MCPServer:
    if self._service_server_cache is None:
      self._service_server_cache = _build_service_server(self, include_raise=True, wire='bare')
    return self._service_server_cache

  def _live_mcp_servers(self) -> list[llm.mcp.MCPServer]:
    # specs materialize here, on first tool use — always in a serving process,
    # post-secrets — and are built once: a live server may hold real resources
    # (flow's shared System), so every run through this bro reuses the same set.
    if self._live_mcp is None:
      self._live_mcp = [spec.build() for spec in self._mcp_specs]
      self._live_mcp.extend(ds.as_mcp_server() for ds in self._data_sources)
    return self._live_mcp

  def _mcp_servers_for(self, *, hold: str) -> list[llm.mcp.MCPServer]:
    # the in-process LLM builds (always bare wire): the `raise` service tool
    # mounts only at the unattended hold — with no human channel the agent
    # needs a way to abort; every other level reports blockers in its reply,
    # as its hold fragment instructs (same gate as the claude assemblies).
    # non-unattended builds recreate the service server without `raise` rather
    # than dropping it wholesale.
    service_server = (
      self._service_server
      if hold == 'unattended'
      else _build_service_server(self, include_raise=False, wire='bare')
    )
    return self._servers_with_at_tools(
      [*self._live_mcp_servers(), service_server], harness='bro', wire='bare'
    )

  def claude_bro_mcp_servers(self) -> list[llm.mcp.MCPServer]:
    # the MCP servers a `cw ss --raw` Claude Code session mounts (through
    # the generic server's `bro:<name>` surface): declared servers, scripts, and the
    # service tools. procedures serve the bro branch (`--bare` strips claude's
    # built-ins, so the session drives work through the bro toolset, not
    # Monitor/Bash) over mcp wire names. `raise` mounts only for an unattended
    # session.
    return self._servers_with_at_tools(
      [
        *self._live_mcp_servers(),
        _build_service_server(self, include_raise=_unattended_claude_session(), wire='mcp'),
      ],
      harness='bro',
      wire='mcp',
    )

  def claude_persona_mcp_servers(self) -> list[llm.mcp.MCPServer]:
    # the MCP servers a cw-session themed as this bro mounts — claude's full
    # harness with the bro as its persona, served through the generic server's
    # `persona:<name>` surface: the declared servers and data sources that hold
    # on the claude harness — an entry gated to the bro harness (the dev
    # toolset, the reference FileSources) never mounts, claude's built-in tools
    # cover it — plus the service server and scripts server (`raise` only for an
    # unattended session). Claude's own skill mechanism remains available.
    specs, sources = self._components_for('claude')
    servers: list[llm.mcp.MCPServer] = [spec.build() for spec in specs]
    servers.extend(ds.as_mcp_server() for ds in sources)
    servers.append(
      _build_service_server(self, include_raise=_unattended_claude_session(), wire='mcp')
    )
    return self._servers_with_at_tools(servers, harness='claude', wire='mcp')

  def _system_prompt_for(self, *, hold: str) -> str:
    # the hold is pinned at run start, so the matching hold fragment is
    # injected rather than detected by the agent — run() defaults unattended,
    # send() guided, with the launch surfaces overriding per their --hold flag
    # (the level files are documented in prompts/CLAUDE.md).
    fragment = hold_fragment(
      hold,
      harness='bro',
      wire='bare',
      creds=credentials.known_names(),
    )
    return f'{self.system_prompt}\n\n{fragment}'

  def _record_error_step(self, error: BaseException) -> None:
    # best-effort: recording the failure must never mask it — the tracker may
    # well be down for the same reason the run is failing.
    try:
      self._tracker.step(
        'error', {'message': str(error), 'traceback': ''.join(traceback.format_exception(error))}
      )
    except Exception as step_error:
      log.warning('failed to record the error step: %s', step_error)

  def _make_observer(self) -> Observer:
    return BoringRenderer(prefix=self.name)

  def _make_tracker(self) -> Tracker:
    return _default_tracker_factory()

  def _make_channel(self) -> Optional[BroChannel]:
    # None (no BROKER_CHANNEL in the environment) keeps the lifecycle emission inert
    return BroChannel.from_env()

  def _create_llm(self, *, hold: str) -> LLM:
    return self.llm_spec.create_llm(
      mcp_servers=self._mcp_servers_for(hold=hold),
      observer=self._observer,
      tracker=self._tracker,
      # the LLM publishes cumulative usage under the bro's surface identity (the
      # usage file must be self-describing — an in-process run's CW_BRO is the
      # launcher's, not this bro's).
      agent=self.agent,
    )
