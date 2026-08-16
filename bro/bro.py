import json
import os
import sys
import traceback
from abc import ABC
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Optional, Self

import bro.llm.llms.openai as llm_llms_openai
import bro.llm.mcp as llm_mcp
from bro import spells as spell_store
from bro.base import credentials, log
from bro.base.condition import Condition, Entry, Iff, SetVariable, Variables, When, var
from bro.base.offload import off_loop
from bro.channel import BroChannel
from bro.datasources.base import DataSource
from bro.llm.llm import EFFORT_LEVELS, LLM, NativeLLMSpec
from bro.llm.observer import (
  NullObserver,
  Observer,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)
from bro.llm.tracker import EndReason, NullTracker, ToolStepSource, Tracker
from bro.prompts import get_prompt, hold_fragment
from bro.summon import SUMMONER_ENV
from bro.trails.display.config import PresetName, preset
from bro.trails.display.core import DisplaySession
from bro.trails.display.live import LiveDisplayObserver
from bro.trails.display.terminal import StreamRenderer
from bro.trails.record.bro import Recorder

DEFAULT_LLM_SPEC: NativeLLMSpec = llm_llms_openai.LLMSpec()


_TRAILS_DISABLED_ENV = 'TRAILS_DISABLED'


def _observer_scope(observer: Observer) -> AbstractContextManager[Observer]:
  if isinstance(observer, AbstractContextManager):
    return observer
  return nullcontext(observer)


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
  # or repairing trails-server itself (recording is otherwise mandatory and
  # crash-on-failure, so a broken server blocks every bro). this only governs the default
  # factory: a per-run `tracker=` and a custom `set_default_tracker_factory(...)`
  # still take precedence.
  if os.environ.get(_TRAILS_DISABLED_ENV) is not None:
    return NullTracker()
  # recording is otherwise mandatory in production: the `trails` secret must
  # resolve from `~/.bro/trails.json`. a missing
  # secret is a setup error, not a fallback path — `NullTracker` is opt-in:
  # - kill switch: `TRAILS_DISABLED` set in the environment.
  # - tests: `conftest.py`'s `set_default_tracker_factory(NullTracker)`.
  # - one-shot exploration: `bro.run(..., surface='experiment', tracker=NullTracker())`.
  try:
    config = credentials.get_json('trails')
  except credentials.SecretNotFound as e:
    raise RuntimeError(
      'trails: secret not found; configure ~/.bro/trails.json to enable recording, '
      'or pass tracker=NullTracker() to skip explicitly'
    ) from e
  return Recorder(config['base_url'], config['token'])


# default factory for the per-run `Tracker` an unconfigured bro uses. swap with
# `set_default_tracker_factory(...)` — `conftest.py` pins it to `NullTracker`
# so tests never try to record.
_default_tracker_factory: Callable[[], Tracker] = _default_factory


def set_default_tracker_factory(factory: Callable[[], Tracker]) -> None:
  global _default_tracker_factory
  _default_tracker_factory = factory


_SHARED_PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts' / 'shared'


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
      'Third-party skills load through `bro::skill`. A user message starting with `/<name>` '
      'requests that skill: call `bro::skill` with its name, then execute the returned '
      'instructions with the rest of the message as arguments. An empty body means the skill '
      'is unavailable.',
    ]
  )


def _render_spells(*, include_cast: bool) -> str:
  run_instruction = (
    'call `bro::cast` with the enclosed text and follow the returned instructions'
    if include_cast
    else "call the named spell's own tool"
  )
  return '\n'.join(
    [
      '## Spells',
      '',
      'Spells are named procedures exposed as canonical `spell::` tools. To run one, call its '
      'tool and execute the returned instructions.',
      '',
      '`[[…]]` marks a spell — in a user message, a spell body, a doc — with the enclosed text '
      'phrased to fit its sentence rather than spelled as the canonical name: `please [[land '
      'the pr]]`, `did you [[land]]?` and `I [[landed PR:54]]` all name the `land` spell. Run '
      f'the marked spell only where the sentence asks you to: {run_instruction}. Anywhere else '
      'the marker only names it.',
    ]
  )


class BroRaised(llm_mcp.ToolControlSignal):
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
  from bro.workspace.session import terminate_session

  def record_and_kill() -> None:
    log.warning('raise: %s', reason)
    try:
      channel = BroChannel.from_env()
      if channel is not None:
        channel.completed(reason, 'raised')
        channel.close()
    finally:
      terminate_session()

  await off_loop(record_and_kill)
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


def _raise_tool(wire: llm_mcp.Wire, variables: Variables) -> llm_mcp.Tool:
  target = _raise if wire == 'bare' else _claude_raise
  return llm_mcp.FunctionTool(
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
  'by the optional `llm` — the LLM recipe it runs, written `provider:model:effort` '
  'with an optional `+fast` suffix and any field left empty '
  f"(effort is one of {', '.join(EFFORT_LEVELS)}; `::high` keeps the target's own "
  'provider and model, `:opus5` names a model) — and its scope by '
  'the optional `grant` / `revoke` lists — each entry a credential name, or `@bro` '
  "for a summonable target of the child's own. a credential grant replaces the "
  "child's selected same-kind name. you can only grant what you hold yourself (a "
  'credential in your own scope, a bro in your own allow-list), and both directions '
  "are strict, so naming something the child's scope already has (or, for "
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
  'persona, the launch command, the bros it may delegate to (`may_summon`), and '
  'the trail it is recorded into (`trail_id`). call it once at session start to '
  'detect your environment.'
)


def _banner_tool(bro: 'BaseBro', variables: Variables) -> llm_mcp.Tool:
  # the same facts `cw banner --llm` prints, rendered in-process. the bro name is
  # passed explicitly because an in-process run's environment carries the
  # launcher's CW_BRO (or none), not this bro's; the trail id is read at call
  # time because the run's trail opens after this server is built. the workspace
  # import stays function-local so `import bro` stays cheap.
  def _banner() -> str:
    from bro.workspace.banner import render_banner

    return render_banner(llm=True, bro=bro.name, trail_id=bro.trail_id)

  return llm_mcp.FunctionTool(
    _banner, name='banner', description=_BANNER_DESCRIPTION, variables=variables
  )


def _summon_tool(
  variables: Variables, current_tool_step_id: Callable[[], Optional[ToolStepSource]]
) -> llm_mcp.Tool:
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
    llm: Optional[str] = None,
  ) -> str:
    source = current_tool_step_id()
    step_id = source['step_id'] if source is not None else None
    index = source['index'] if source is not None else None
    if detach:
      return await off_loop(
        summon_client.summon_detached,
        target,
        prompt,
        timeout=timeout,
        into=into,
        hold=hold,
        grant=grant,
        revoke=revoke,
        llm=llm,
        step_id=step_id,
        index=index,
      )
    client = summon_client.open_client()
    try:
      return await off_loop(
        summon_client.summon_and_wait,
        target,
        prompt,
        timeout=timeout,
        into=into,
        hold=hold,
        grant=grant,
        revoke=revoke,
        llm=llm,
        step_id=step_id,
        index=index,
        client=client,
      )
    finally:
      client.close()

  return llm_mcp.FunctionTool(
    _summon, name='summon', description=_SUMMON_DESCRIPTION, variables=variables
  )


def _summon_list_tool(variables: Variables) -> llm_mcp.Tool:
  from bro import summon as summon_client

  async def _summon_list() -> dict[str, Any]:
    return await off_loop(summon_client.list_summons)

  return llm_mcp.FunctionTool(
    _summon_list, name='summon_list', description=_SUMMON_LIST_DESCRIPTION, variables=variables
  )


def _summon_check_tool(variables: Variables) -> llm_mcp.Tool:
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
        answer = await off_loop(
          summon_client.collect_summon, request_id, timeout=timeout, client=client
        )
      finally:
        client.close()
      return {'state': 'completed', 'answer': answer}
    if timeout is not None:
      raise ValueError('timeout only bounds a wait; a plain check never blocks')
    status = await off_loop(summon_client.check_summon, request_id, last_seen=last_seen)
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

  return llm_mcp.FunctionTool(
    _summon_check, name='summon_check', description=_SUMMON_CHECK_DESCRIPTION, variables=variables
  )


# the service roster's tool names — the closed `#tools` universe the service
# descriptions render against
_SERVICE_TOOL_NAMES = (
  'banner',
  'cast',
  'skill',
  'raise',
  'summon',
  'summon_check',
  'summon_list',
)


def _build_service_server(
  bro: 'BaseBro', *, include_raise: bool, harness: llm_mcp.Harness, wire: llm_mcp.Wire
) -> llm_mcp.MCPServer:
  # the roster is decided by the caller's surface and local process state:
  # `banner` is unconditional; `cast` needs spells and its optional secret;
  # `skill` bridges only harnesses without a native loader; `raise` only makes
  # sense non-interactively (a caller to abort to — interactive callers pass
  # include_raise=False); `summon`/`summon_check` need a broker channel and
  # `summon_list` the session's summon-status file on top. the decided roster
  # then feeds the tools' rendering vocabulary: service tools are harness
  # features, the one tool surface that conditions on system facts, so `#wire`
  # is injected next to the `#tools` roster.
  has_cast = len(bro.spells) > 0 and spell_store.cast_available()
  has_broker = os.environ.get('BROKER_CHANNEL') is not None
  has_summon_list = False
  if has_broker:
    from bro import summon as summon_module

    has_summon_list = os.environ.get(summon_module.STATUS_ENV) is not None

  mounted = ['banner']
  if has_cast:
    mounted.append('cast')
  if harness == 'bro':
    mounted.append('skill')
  if include_raise:
    mounted.append('raise')
  if has_broker:
    mounted.extend(['summon', 'summon_check'])
  if has_summon_list:
    mounted.append('summon_list')
  variables: Variables = {
    **llm_mcp.surface_variables(wire=wire),
    'tools': SetVariable(frozenset(mounted), universe=frozenset(_SERVICE_TOOL_NAMES)),
  }

  tools: list[llm_mcp.Tool] = [_banner_tool(bro, variables)]
  if has_cast:
    tools.append(spell_store.build_cast_tool(bro, harness=harness, wire=wire))
  if harness == 'bro':
    tools.append(spell_store.build_skill_tool())
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
  server = llm_mcp.InProcessMCPServer('bro', tools)
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
  `tools` / `data_sources` entries: `when(feature('brog'), mount(brog_mcp.toolset))`."""
  return var('features').contains(name)


def _feature_variables(features: dict[str, Condition | bool]) -> Variables:
  # a Condition gate evaluates against its own vocabulary — `creds` probing
  # `available` lazily with no closed universe, deliberately not the `#creds`
  # fact; see `reference/conditions.md` "Bro features" for why a scoped store
  # breaks the latter. a bool gate is a declaration-time constant, as in `when`.
  gate_variables: Variables = {'creds': SetVariable(lambda name: credentials.available(name))}

  def enabled(name: str) -> bool:
    gate = features[name]
    return gate if isinstance(gate, bool) else gate.evaluate(gate_variables)

  return {'features': SetVariable(enabled, universe=frozenset(features))}


def _component_needed_secrets(component: llm_mcp.MCPServerSpec | DataSource) -> set[str]:
  # a component declares its credentials as plain metadata (a spec field, or a
  # DataSource class attribute), so reading the manifest never builds a live
  # server. no real component extends a non-empty base's declaration, so an MRO
  # union would be identical.
  return set(component.needed_secrets)


def _component_optional_secrets(component: llm_mcp.MCPServerSpec | DataSource) -> set[str]:
  # mirror of `_component_needed_secrets` for the best-effort tier (`optional_secrets`).
  return set(component.optional_secrets)


def _fold_tool_layers(
  layers: list[llm_mcp.ToolLayer], harness: llm_mcp.Harness
) -> tuple[list[llm_mcp.MCPServerSpec], tuple[str, ...]]:
  server_specs: list[llm_mcp.MCPServerSpec] = []
  blocked_names: list[str] = []
  for layer in layers:
    server_specs.extend(layer.server_specs)
    if len(layer.blocked_native_tool_names) > 0 and harness != 'claude':
      raise ValueError(
        f'cannot block native tools {layer.blocked_native_tool_names!r} on the {harness!r} '
        'harness; it serves only the tools the bro declares'
      )
    blocked_names.extend(layer.blocked_native_tool_names)
  return server_specs, tuple(dict.fromkeys(blocked_names))


_COMPONENT_DECLARATION_ATTRIBUTES = frozenset({'data_sources', 'tools'})
_RETIRED_COMPONENT_DECLARATION_ATTRIBUTES = {'mcp_servers': 'tools'}


def _component_destinations(value: object) -> set[str]:
  destinations: set[str] = set()
  entries = value if isinstance(value, list) else (value,)
  for entry in entries:
    if isinstance(entry, When):
      components = (entry.item,)
    elif isinstance(entry, Iff):
      components = tuple(item for _, item in entry.branches) + (entry.otherwise or ())
    else:
      components = (entry,)
    for component in components:
      if isinstance(component, llm_mcp.ToolLayer):
        destinations.add('tools')
      elif isinstance(component, DataSource):
        destinations.add('data_sources')
  return destinations


class BaseBro(ABC):
  name: str
  description: str
  llm_spec: NativeLLMSpec = DEFAULT_LLM_SPEC
  # entries may be wrapped with `bro.base.condition.when(...)` / grouped with
  # `iff(...)` to gate them on the assembling surface's facts (`#harness`,
  # `#creds`); a wrapped entry whose condition does not hold is omitted before
  # the declaration is applied. each `tools` layer mounts server specs, blocks
  # harness-native tools, or does both; data sources remain a separate read-only
  # contract.
  data_sources: ClassVar[list[Entry[DataSource]]] = []
  tools: ClassVar[list[Entry[llm_mcp.ToolLayer]]] = []
  # named optional capabilities: feature name → the gate deciding whether the
  # feature is on — a `Condition` over the environment's resolvable credentials
  # (`creds.contains('brog')`), or a plain bool constant as in `when` (True
  # pins the feature on, False disables it). one declaration switches every
  # consuming site together: components gate via `when(feature('<name>'), …)`,
  # static text via `{{iff #features contains <name>}}` — so a gated component
  # enters the manifest, mounts, and renders its text only where its gates
  # resolve. MRO-walked like `tools`, with derived classes overriding
  # parents per name — `{'<name>': True}` pins an inherited feature on, turning
  # its components into hard requirements. False is terminal: redeclaring a
  # feature a base class disabled fails construction, so an opt-out binds the
  # whole sub-hierarchy.
  features: ClassVar[dict[str, Condition | bool]] = {}
  # credentials no component expresses — the escape hatch for a bro's environment
  # needs. MRO-walked and unioned like `tools`, so a subclass declares only
  # what it adds. folded into
  # `needed_secrets()`.
  extra_secrets: tuple[str, ...] = ()
  # bros this bro may summon — its static outgoing allow-list. root sessions get
  # it adjusted per session by `--grant @bro`/`--revoke @bro`; a summoned child
  # follows the bare seeds, so summons chain transitively through seeded bros
  # under the host's depth cap (see bro/launch/summon_control.py). MRO-walked and
  # unioned like `extra_secrets`.
  may_summon: tuple[str, ...] = ()
  # whether the bro does docker work (building/pushing images for deploys) and so
  # needs the host docker socket. an explicit capability, inherited normally. the
  # host grants `/var/run/docker.sock` to a `--raw`/bro-run container only when this
  # is set (claude code sessions get it unconditionally); see bro/launch/scope.py.
  needs_docker: bool = False
  # subclasses declare their own `system_prompt = "..."` as a class attribute;
  # `__init__` walks the MRO from base to derived and concatenates each class's
  # own contribution. so a `ReviewDev(Dev)` subclass declares only what it adds —
  # Dev's prompt (and Bro's) are picked up automatically. same for `tools` and
  # `data_sources`. inherit directly from BaseBro to opt out
  # of the concrete `Bro`'s shared defaults.
  system_prompt: str = ''
  # the bro's own class prompts (MRO-concatenated); set in __init__
  persona: str
  # `system_prompt` with the Claude-Code tool-name rule in place of the
  # bro-native one; set in __init__, consumed by `cw ss --raw`
  claude_system_prompt: str

  _llm: Optional[LLM] = None

  def __init_subclass__(cls, **kwargs: Any) -> None:
    super().__init_subclass__(**kwargs)
    for attribute_name, value in vars(cls).items():
      if attribute_name in _COMPONENT_DECLARATION_ATTRIBUTES:
        continue
      component_destinations = _component_destinations(value)
      if len(component_destinations) == 0:
        continue
      destination_text = ' or '.join(repr(name) for name in sorted(component_destinations))
      message = (
        f'{cls.__name__}.{attribute_name} contains component declarations under an attribute '
        f'BaseBro does not read; move them to {destination_text}'
      )
      retired_destination = _RETIRED_COMPONENT_DECLARATION_ATTRIBUTES.get(attribute_name)
      if retired_destination in component_destinations:
        message += f'; {attribute_name!r} was renamed to {retired_destination!r}'
      raise TypeError(message)

  def __init__(self, system_prompt: Optional[str] = None):
    tool_entries: list[Entry[llm_mcp.ToolLayer]] = []
    data_source_entries: list[Entry[DataSource]] = []
    prompt_parts: list[str] = []
    extra_secret_names: list[str] = []
    may_summon_names: list[str] = []
    feature_gates: dict[str, Condition | bool] = {}
    for cls in reversed(type(self).__mro__):
      raw_tools = cls.__dict__.get('tools')
      if raw_tools is not None:
        tool_entries.extend(raw_tools)
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
        for feature_name, gate in raw_features.items():
          if feature_gates.get(feature_name) is False and gate is not False:
            raise ValueError(
              f'{cls.__name__} re-enables feature {feature_name!r} disabled by a base '
              'class; a False gate is terminal for the sub-hierarchy'
            )
          feature_gates[feature_name] = gate
    self._extra_secrets: tuple[str, ...] = tuple(extra_secret_names)
    self._may_summon: tuple[str, ...] = tuple(may_summon_names)
    self._features: dict[str, Condition | bool] = feature_gates
    # the membership probe is lazy, so the vocabulary built here stays current
    # with the store — only selection (below) bakes feature truth in.
    self._feature_vocabulary: Variables = _feature_variables(feature_gates)
    # the raw declaration entries, kept for per-harness selection
    # (_components_for); the bro-harness selection is materialized eagerly —
    # the prompt compositions below and the live-server cache read it. wire is
    # not a fact — component inclusion is wire-independent (the wire only
    # spells tool names).
    self._tool_entries = tool_entries
    self._data_source_entries = data_source_entries
    surface_creds = credentials.known_names()
    selected_tools = llm_mcp.select(
      tool_entries, harness='bro', creds=surface_creds, extra=self._feature_vocabulary
    )
    self._mcp_specs, _ = _fold_tool_layers(selected_tools, 'bro')
    self._data_sources: list[DataSource] = llm_mcp.select(
      data_source_entries, harness='bro', creds=surface_creds, extra=self._feature_vocabulary
    )
    # built lazily by _live_mcp_servers(): metadata surfaces (needed_secrets on
    # hosts, prompt composition) never construct live servers.
    self._live_mcp: Optional[list[llm_mcp.MCPServer]] = None
    # lazy for the same reason: building service FunctionTools derives their
    # schemas, which imports the mcp/fastmcp stack (~1s) — metadata surfaces
    # never pay it.
    self._service_server_cache: Optional[llm_mcp.MCPServer] = None
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
    # reads as a stray fragment. no shared / data-source / spells blocks here;
    # injected into dive-in Claude Code sessions (cw/system_prompt.py) so they
    # carry the bro's policies outside --raw mode.
    self.persona = (
      '\n\n'.join([f'# Persona: {self.name}', *prompt_parts]) if len(prompt_parts) > 0 else ''
    )
    shared = _load_shared_prompts()
    spell_instructions = self.spell_instructions()

    def compose(wire: llm_mcp.Wire) -> str:
      parts = []
      if len(shared) > 0:
        parts.append(shared)
      if len(self.persona) > 0:
        parts.append(self.persona)
      parts.append(get_prompt('tool_names.md').strip())
      if len(self._data_sources) > 0:
        parts.append(_render_data_sources(self._data_sources))
      if len(spell_instructions) > 0:
        parts.append(spell_instructions)
      parts.append(_render_skill_loader())
      # last, so it sits at the end of the prompt where instruction recency is
      # strongest; the file's directives scope it to the claude-bare surface.
      parts.append(get_prompt('grounding.md').strip())
      # both composed flavors serve the bro harness; only the wire scheme differs.
      # stripped: a fragment whose whole body is a skipped directive block
      # (grounding.md outside the claude-bare surface) collapses to bare join
      # separators at the prompt edge.
      return llm_mcp.render_text(
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

  def has_feature(self, name: str) -> bool:
    """whether the named feature is declared and its gate holds in this
    environment. an undeclared name reads as off — the probe is for harness
    code asking an arbitrary persona about a capability, unlike renders, whose
    closed universe makes an unknown name an error."""
    return name in self._features and feature(name).evaluate(self._feature_vocabulary)

  @property
  def spells(self) -> dict[str, Path]:
    return spell_store.collect_spells(list(reversed(type(self).__mro__)))

  def get_spell_body(self, name: str, *, harness: llm_mcp.Harness, wire: llm_mcp.Wire) -> str:
    path = self.spells.get(name)
    if path is None:
      available = ', '.join(sorted(self.spells)) if len(self.spells) > 0 else '(none)'
      raise KeyError(f'no spell named {name!r}; available: {available}')
    spell = spell_store.load_spell(name, path)
    return llm_mcp.render_text(
      spell.body,
      harness=harness,
      wire=wire,
      creds=credentials.known_names(),
      extra=self._feature_vocabulary,
    ).strip()

  def spell_descriptions(self) -> list[tuple[str, str]]:
    return [
      (name, spell_store.load_spell(name, path).description) for name, path in self.spells.items()
    ]

  def spell_instructions(self) -> str:
    if len(self.spell_descriptions()) == 0:
      return ''
    return _render_spells(include_cast=spell_store.cast_available())

  def _selected_tools_for(
    self, harness: llm_mcp.Harness
  ) -> tuple[list[llm_mcp.MCPServerSpec], tuple[str, ...]]:
    selected: list[llm_mcp.ToolLayer] = llm_mcp.select(
      self._tool_entries,
      harness=harness,
      creds=credentials.known_names(),
      extra=self._feature_vocabulary,
    )
    return _fold_tool_layers(selected, harness)

  def blocked_tool_names(self, harness: llm_mcp.Harness) -> tuple[str, ...]:
    """harness-native tool names blocked by this bro's selected layers."""
    _, blocked_names = self._selected_tools_for(harness)
    return blocked_names

  def _components_for(
    self, harness: llm_mcp.Harness
  ) -> tuple[list[llm_mcp.MCPServerSpec], list[DataSource]]:
    # the declared components that hold on `harness`. the bro-harness selection
    # is the one materialized in __init__ (prompt composition and the
    # live-server cache read it); any other harness selects on demand from the
    # raw entries.
    if harness == 'bro':
      return self._mcp_specs, self._data_sources
    specs, _ = self._selected_tools_for(harness)
    sources: list[DataSource] = llm_mcp.select(
      self._data_source_entries,
      harness=harness,
      creds=credentials.known_names(),
      extra=self._feature_vocabulary,
    )
    return specs, sources

  def needed_secrets(self, harness: llm_mcp.Harness = 'bro') -> tuple[str, ...]:
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

  def optional_secrets(self, harness: llm_mcp.Harness = 'bro') -> tuple[str, ...]:
    # the bro's best-effort credential tier: the union of each declared MCP
    # server's + data source's `optional_secrets` over the same per-harness
    # component set as `needed_secrets`, plus the cast key when this bro has
    # spells. minus anything already required — a hard requirement is never
    # downgraded. absent optional secrets degrade the capability instead of
    # failing the launch.
    specs, sources = self._components_for(harness)
    names: set[str] = set()
    for spec in specs:
      names.update(_component_optional_secrets(spec))
    for ds in sources:
      names.update(_component_optional_secrets(ds))
    if len(self.spells) > 0:
      names.add(spell_store.CAST_SECRET)
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

  def _provision_workspace(self) -> None:
    # feature-declared workspace provisioning, run at session start (cw's
    # in-place runner is the claude-harness counterpart): a commit-accounting
    # persona gets the footer hooks installed into its managed workspace, so
    # agent commits carry the token footer with no session involvement. scoped
    # to managed workspaces — an in-place run in an arbitrary repo must not
    # write into it — and hooks already present are left alone.
    if not self.has_feature('commit-accounting') or os.environ.get('CW_NAME') is None:
      return
    from bro.workflow.commit_footer import install_hooks

    install_hooks(Path.cwd(), overwrite=False)

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
    self._provision_workspace()
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

    self._close_live_servers()
    self._lifetime_active = False
    self._last_end_reason = reason
    self._last_end_detail = detail
    log.verbose('bro lifetime ended: %s', reason)
    self._tracker.end_trail(reason, detail=detail)
    return False

  @classmethod
  def create(cls, llm_spec: NativeLLMSpec) -> Self:
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
    effective_observer = observer if observer is not None else self._make_observer()
    with _observer_scope(effective_observer):
      effective_observer.on_event(TurnStartedEvent(input))
      refusal = self._start_refusal()
      if refusal is not None:
        effective_observer.on_event(TurnFailedEvent(refusal))
        raise BroRaised(refusal)
      try:
        llm, messages, trail_id = self._start(
          input,
          interactive=False,
          hold=hold,
          observer=effective_observer,
          tracker=tracker,
          surface=surface,
          summoned_by=_summoned_by_from_env(),
        )
      except Exception as error:
        effective_observer.on_event(TurnFailedEvent(str(error)))
        raise
      log.info('run started%s', f' (trail {trail_id})' if len(trail_id) > 0 else '')
      channel = self._make_channel()
      if channel is not None:
        channel.started(trail_id)
      result: Optional[str] = None
      try:
        with self:
          try:
            result = await llm.send(messages, request_timeout=request_timeout)
          except Exception as error:
            effective_observer.on_event(TurnFailedEvent(str(error)))
            raise
          effective_observer.on_event(TurnCompletedEvent(result))
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
      effective_observer = observer if observer is not None else self._make_observer()
      effective_observer.on_event(TurnStartedEvent(message))
      refusal = self._start_refusal()
      if refusal is not None:
        # in-reply report; the LLM stays unbuilt, so a later send re-checks
        effective_observer.on_event(TurnRefusedEvent(refusal))
        return refusal
      # the tracker is locked in on first send (the LLM is constructed once and
      # records one trail); later calls can't swap it. surface (the trail
      # header's surface label) and hold are locked in the same way.
      try:
        self._llm, messages, _ = self._start(
          message,
          interactive=True,
          hold=hold,
          observer=effective_observer,
          tracker=tracker,
          surface=surface,
          summoned_by=None,
        )
      except Exception as error:
        effective_observer.on_event(TurnFailedEvent(str(error)))
        raise
    else:
      if observer is not None:
        # unlike the tracker, the observer is rebindable mid-conversation: a
        # preseeded bro (bro.fork) built its LLM before the interactive surface
        # existed, so the surface attaches its renderer on its first send.
        self._observer = observer
        self._llm.observer = observer
      effective_observer = self._observer
      effective_observer.on_event(TurnStartedEvent(message))
      messages = [{'role': 'user', 'content': message}]
    try:
      result = await self._llm.send(messages, request_timeout=request_timeout)
    except Exception as error:
      effective_observer.on_event(TurnFailedEvent(str(error)))
      raise
    effective_observer.on_event(TurnCompletedEvent(result))
    return result

  def _servers_with_spell_tools(
    self,
    servers: list[llm_mcp.MCPServer],
    *,
    harness: llm_mcp.Harness,
    wire: llm_mcp.Wire,
  ) -> list[llm_mcp.MCPServer]:
    if any(server.namespace == spell_store.NAMESPACE for server in servers):
      raise ValueError(f'namespace {spell_store.NAMESPACE!r} is reserved for bro framework tools')
    if len(self.spells) == 0:
      return servers
    return [*servers, spell_store.build_spell_server(self, harness=harness, wire=wire)]

  @property
  def _service_server(self) -> llm_mcp.MCPServer:
    if self._service_server_cache is None:
      self._service_server_cache = _build_service_server(
        self, include_raise=True, harness='bro', wire='bare'
      )
    return self._service_server_cache

  def _live_mcp_servers(self) -> list[llm_mcp.MCPServer]:
    # specs materialize here, on first tool use — always in a serving process,
    # post-secrets — and are built once: a live server may hold real resources
    # and every run through this bro reuses the same set.
    if self._live_mcp is None:
      self._live_mcp = [spec.build() for spec in self._mcp_specs]
      self._live_mcp.extend(ds.as_mcp_server() for ds in self._data_sources)
    return self._live_mcp

  def _close_live_servers(self) -> None:
    # a live server may hold real resources, and the lifetime is the seam that
    # releases them. best-effort: a failing teardown must not mask the run's own
    # outcome.
    if self._live_mcp is None:
      return
    for server in self._live_mcp:
      try:
        server.close()
      except Exception as error:
        log.warning('failed to close the %s server: %s', server.namespace, error)

  def _mcp_servers_for(self, *, hold: str) -> list[llm_mcp.MCPServer]:
    # the in-process LLM builds (always bare wire): the `raise` service tool
    # mounts only at the unattended hold — with no human channel the agent
    # needs a way to abort; every other level reports blockers in its reply,
    # as its hold fragment instructs (same gate as the claude assemblies).
    # non-unattended builds recreate the service server without `raise` rather
    # than dropping it wholesale.
    service_server = (
      self._service_server
      if hold == 'unattended'
      else _build_service_server(self, include_raise=False, harness='bro', wire='bare')
    )
    return self._servers_with_spell_tools(
      [*self._live_mcp_servers(), service_server], harness='bro', wire='bare'
    )

  def claude_bro_mcp_servers(self) -> list[llm_mcp.MCPServer]:
    # the MCP servers a `cw ss --raw` Claude Code session mounts (through
    # the generic server's `bro:<name>` surface): declared servers, spells, and the
    # service tools. procedures serve the bro branch (`--bare` strips claude's
    # built-ins, so the session drives work through the bro toolset, not
    # Monitor/Bash) over mcp wire names. `raise` mounts only for an unattended
    # session.
    return self._servers_with_spell_tools(
      [
        *self._live_mcp_servers(),
        _build_service_server(
          self, include_raise=_unattended_claude_session(), harness='bro', wire='mcp'
        ),
      ],
      harness='bro',
      wire='mcp',
    )

  def claude_persona_mcp_servers(self) -> list[llm_mcp.MCPServer]:
    # the MCP servers a cw-session themed as this bro mounts — claude's full
    # harness with the bro as its persona, served through the generic server's
    # `persona:<name>` surface: the declared servers and data sources that hold
    # on the claude harness — an entry gated to the bro harness (the dev
    # toolset, the reference FileSources) never mounts, claude's built-in tools
    # cover it — plus the service server and spells server (`raise` only for an
    # unattended session). Claude's own skill mechanism remains available.
    specs, sources = self._components_for('claude')
    servers: list[llm_mcp.MCPServer] = [spec.build() for spec in specs]
    servers.extend(ds.as_mcp_server() for ds in sources)
    servers.append(
      _build_service_server(
        self, include_raise=_unattended_claude_session(), harness='claude', wire='mcp'
      )
    )
    return self._servers_with_spell_tools(servers, harness='claude', wire='mcp')

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
    configuration = preset(PresetName.OBSERVER, context_label=self.name)
    return LiveDisplayObserver(DisplaySession(configuration, StreamRenderer(sys.stderr)))

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
