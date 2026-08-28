import os
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Optional, Protocol, Self

import bro.llm.llms.openai as llm_llms_openai
import bro.llm.mcp as llm_mcp
import bro.mcp as mcp
from bro import spells as spell_store, summon
from bro.base import credentials, log
from bro.base.condition import Condition, Entry, Iff, SetVariable, Variables, When, var
from bro.base.offload import off_loop
from bro.channel import BroChannel
from bro.datasources.base import DataSource
from bro.datasources.man import ManPage, manual
from bro.llm.llm import EFFORT_LEVELS, NativeLLMSpec
from bro.llm.tracker import ToolStepSource
from bro.prompts import get_prompt, session_fragment

DEFAULT_LLM_SPEC: NativeLLMSpec = llm_llms_openai.LLMSpec()

ProvisionStep = Callable[[Path], None]


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


class LiveRun(Protocol):
  """the in-flight run the service tools report against, implemented by whatever
  drives the bro in this process. Both facts are read at call time: the trail
  opens after the service server is built, and the tool position moves with
  every call. A process that assembles a bro without running one has none."""

  @property
  def trail_id(self) -> Optional[str]: ...

  @property
  def current_tool_step_id(self) -> Optional[ToolStepSource]: ...


RAISE_EXIT_STATUS = 1


class BroRaised(llm_mcp.ToolControlSignal):
  """aborts a Bro run: raised by the `raise` service tool, and by the run-start
  credential gate when required secrets don't resolve."""

  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


class AnswerDelivered(llm_mcp.ToolControlSignal):
  """ends a summoned Bro run with its explicit answer: raised by the `answer`
  service tool's bare flavor; the surface that drives the run turns it into the
  run's ok result."""

  def __init__(self, answer: str):
    super().__init__(answer)
    self.answer = answer


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
      terminate_session(status=RAISE_EXIT_STATUS)

  await off_loop(record_and_kill)
  # never reaches the agent in practice: ending the session interrupts the turn
  # this call belongs to, and an interrupted turn's pending results are dropped
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


def _raise_tool(wire: mcp.Wire, variables: Variables) -> llm_mcp.Tool:
  target = _raise if wire == 'bare' else _claude_raise
  return llm_mcp.FunctionTool(
    target, name='raise', description=_RAISE_DESCRIPTION, variables=variables
  )


def _answer(answer: str) -> str:
  raise AnswerDelivered(answer)


async def _claude_answer(answer: str) -> str:
  # the claude twin of _claude_raise, for the clean end: no exception can end
  # the consuming claude session, so send the run's result over the broker channel,
  # then terminate the session. Unlike raise, an undeliverable answer must not
  # kill the session — without a channel the summoner would never hear it, so
  # that errors back to the agent instead.
  from bro.workspace.session import terminate_session

  def record_and_kill() -> None:
    channel = BroChannel.from_env()
    if channel is None:
      raise RuntimeError(
        'no broker channel: the answer cannot reach the summoner; surface it to the user instead'
      )
    log.info('answer delivered to the summoner')
    channel.completed(answer, 'ok')
    channel.close()
    terminate_session(status=0)

  await off_loop(record_and_kill)
  # never reaches the agent, for the reason `_claude_raise` gives
  return 'the answer is recorded and the session is being terminated. Stop working now.'


_ANSWER_DESCRIPTION = (
  'deliver the final answer of this summoned session to the summoner waiting on '
  'it, and end the session. This session runs on behalf of another session; call '
  'this exactly once, when the work is done — in an attended session, once the '
  'user confirms nothing is left — with a self-contained answer: the summoner '
  'sees nothing else of this session. A session that ends without this call '
  'reports no answer and surfaces to the summoner as a failure.'
  '{{when #wire = mcp}} The call records the answer and terminates the session; '
  'nothing after it will run.{{end}}'
)


def _answer_tool(wire: mcp.Wire, variables: Variables) -> llm_mcp.Tool:
  target = _answer if wire == 'bare' else _claude_answer
  return llm_mcp.FunctionTool(
    target, name='answer', description=_ANSWER_DESCRIPTION, variables=variables
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
  "by the optional `harness` — the driving loop it runs under: `bro` (default — the target's "
  'own LLM process) or `claude` (a one-shot managed Claude Code session) — and the optional '
  '`llm` — the LLM recipe it runs within that harness, written `provider:model:effort` '
  'with an optional `+fast` suffix and any field left empty '
  f"(effort is one of {', '.join(EFFORT_LEVELS)}; `::high` keeps the target's own "
  'provider and model, `:opus5` names a model; a recipe the harness cannot run fails the '
  'summon rather than switching the harness). its scope is shaped by '
  'the optional `grant` / `revoke` lists — each entry a credential name, or `@bro` '
  "for a summonable target of the child's own. a credential grant replaces the "
  "child's selected same-kind name. you can only grant what you hold yourself (a "
  'credential in your own scope, a bro in your own allow-list), and both directions '
  "are strict, so naming something the child's scope already has (or, for "
  'a revoke, lacks) fails the summon. the same bound covers `harness` / `llm`: a '
  'driving loop needing a credential you do not hold (claude needs the Claude '
  'OAuth token) fails the summon, whatever the target itself declares. the '
  'optional `share` list names artifact refs (from `artifact mint`) to hand the '
  'child read access to — only refs this session can itself read. '
  'fails with the reason when the run raises, errors out, '
  'or dies. `detach: true` returns the request id right after the send instead of '
  'blocking — poll or collect it with `summon_check`. `manual: true` registers a '
  'manual summon instead of spawning: the call returns as soon as the host '
  'accepts (a denial fails it right there) with a token and a `ride` command — '
  'relay both to the user, who launches the child session themselves, '
  'interactively and at their own pace; the session then answers like any summon, '
  'so poll the token with `summon_check` (a manual summon never blocks for the '
  'answer, and it refuses `timeout`/`hold`/`llm`/`harness` — the user’s launch '
  'owns those). use it when the child needs the user in the loop.'
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


def _banner_tool(bro: 'BaseBro', live_run: Optional[LiveRun], variables: Variables) -> llm_mcp.Tool:
  # the same facts `ride banner --llm` prints, rendered in-process. the bro name is
  # passed explicitly because an in-process run's environment carries the
  # launcher's RIDE_BRO (or none), not this bro's. the workspace import stays
  # function-local so `import bro` stays cheap.
  def _banner() -> str:
    from bro.workspace.banner import render_banner

    trail_id = None if live_run is None else live_run.trail_id
    return render_banner(llm=True, bro=bro.name, trail_id=trail_id)

  return llm_mcp.FunctionTool(
    _banner, name='banner', description=_BANNER_DESCRIPTION, variables=variables
  )


def _summon_tool(variables: Variables, live_run: Optional[LiveRun]) -> llm_mcp.Tool:
  # a fresh channel client per call, opened on the loop and closed in `finally`
  # so a cancelled tool call (the MCP client timed out or aborted) unblocks the
  # off-loop wait: the broxy sees the waiter go, and the result buffers for a
  # later summon_check instead of feeding an abandoned thread. the blocking wait
  # runs off-loop so an interactive surface stays responsive under a long summon.
  # the run's tool position names the summon call's projected source, so the
  # child's `summoned_by` can carry the precise fork position.
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
    share: Optional[list[str]] = None,
    llm: Optional[str] = None,
    harness: Optional[str] = None,
    manual: bool = False,
  ) -> str:
    source = None if live_run is None else live_run.current_tool_step_id
    step_id = source['step_id'] if source is not None else None
    index = source['index'] if source is not None else None
    if manual:
      launch_owned = {'timeout': timeout, 'hold': hold, 'llm': llm, 'harness': harness}
      passed = sorted(name for name, value in launch_owned.items() if value is not None)
      if len(passed) > 0:
        raise ValueError(f"a manual summon's launch owns {', '.join(passed)}; drop the field(s)")
      if share is not None:
        raise ValueError(
          "a manual summon's container is not launched by the host, so 'share' cannot be honored"
        )
      # a manual child is launched and paced by a human, so the manual path only
      # waits for the host's acceptance — a blocking wait for the answer would
      # outlive any transport budget
      token = await off_loop(
        summon_client.summon_manual,
        target,
        prompt,
        into=into,
        grant=grant,
        revoke=revoke,
        step_id=step_id,
        index=index,
      )
      command = summon_client.manual_launch_command(token, target)
      return (
        f'manual summon accepted; token {token}. relay the launch command to the '
        f'user: `{command}` — then poll the token with summon_check'
      )
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
        share=share,
        llm=llm,
        harness=harness,
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
        share=share,
        llm=llm,
        harness=harness,
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
  'answer',
  'summon',
  'summon_check',
  'summon_list',
)


def _build_service_server(
  bro: 'BaseBro',
  *,
  include_raise: bool,
  harness: mcp.Harness,
  wire: mcp.Wire,
  live_run: Optional[LiveRun] = None,
) -> llm_mcp.MCPServer:
  # built only on the paths that serve a bro, never at construction: deriving the
  # FunctionTool schemas below pulls the mcp/fastmcp stack (~1s), which metadata
  # surfaces (credential scoping, prompt composition, `bro show`) must not pay.
  # the roster is decided by the caller's surface and local process state:
  # `banner` is unconditional; `cast` needs spells and its optional secret;
  # `skill` bridges only harnesses without a native loader; `raise` only makes
  # sense non-interactively (a caller to abort to — interactive callers pass
  # include_raise=False); `answer` is the summoned run's delivery surface — it
  # needs the summoned mark and a channel to send the result on, plus a
  # killable session on the mcp wire (the bare flavor ends the run by
  # exception); `summon`/`summon_check` need a broker channel and
  # `summon_list` the session's summon-status file on top. the decided roster
  # then feeds the tools' rendering vocabulary: service tools are harness
  # features, the one tool surface that conditions on system facts, so `#wire`
  # is injected next to the `#tools` roster.
  from bro.summon import summoned

  has_cast = len(bro.spells) > 0 and spell_store.cast_available()
  has_broker = os.environ.get('BROKER_CHANNEL') is not None
  has_answer = (
    has_broker and summoned() and (wire == 'bare' or os.environ.get('RIDE_RUNNER_PID') is not None)
  )
  has_summon_list = False
  if has_broker:
    from bro import summon_status

    has_summon_list = summon_status.status_path() is not None

  mounted = ['banner']
  if has_cast:
    mounted.append('cast')
  if harness == 'bro':
    mounted.append('skill')
  if include_raise:
    mounted.append('raise')
  if has_answer:
    mounted.append('answer')
  if has_broker:
    mounted.extend(['summon', 'summon_check'])
  if has_summon_list:
    mounted.append('summon_list')
  variables: Variables = {
    **mcp.surface_variables(wire=wire),
    'tools': SetVariable(frozenset(mounted), universe=frozenset(_SERVICE_TOOL_NAMES)),
  }

  tools: list[llm_mcp.Tool] = [_banner_tool(bro, live_run, variables)]
  if has_cast:
    tools.append(spell_store.build_cast_tool(bro, harness=harness, wire=wire))
  if harness == 'bro':
    tools.append(spell_store.build_skill_tool())
  if include_raise:
    tools.append(_raise_tool(wire, variables))
  if has_answer:
    tools.append(_answer_tool(wire, variables))
  if has_broker:
    tools.append(_summon_tool(variables, live_run))
    tools.append(_summon_check_tool(variables))
    if has_summon_list:
      tools.append(_summon_list_tool(variables))
  assert [tool.name for tool in tools] == mounted
  server = llm_mcp.InProcessMCPServer('bro', tools)
  server.tool_universe = _SERVICE_TOOL_NAMES
  return server


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


def _component_needed_secrets(component: mcp.MCPServerSpec | DataSource) -> set[str]:
  # a component declares its credentials as plain metadata (a spec field, or a
  # DataSource class attribute), so reading the manifest never builds a live
  # server. no real component extends a non-empty base's declaration, so an MRO
  # union would be identical.
  return set(component.needed_secrets)


def _component_optional_secrets(component: mcp.MCPServerSpec | DataSource) -> set[str]:
  # mirror of `_component_needed_secrets` for the best-effort tier (`optional_secrets`).
  return set(component.optional_secrets)


@dataclass(frozen=True)
class _ToolSelection:
  """what a bro's tool layers amount to on one harness."""

  server_specs: list[mcp.MCPServerSpec]
  blocked_tool_names: tuple[str, ...]
  # native tool name -> the commands it may reach, for the harness to enforce
  narrowed_tool_commands: dict[str, tuple[str, ...]]


def _fold_tool_layers(layers: list[mcp.ToolLayer], harness: mcp.Harness) -> _ToolSelection:
  server_specs: list[mcp.MCPServerSpec] = []
  blocked_names: list[str] = []
  narrowed: dict[str, list[str]] = {}
  handed_back: dict[str, str] = {}
  for layer in layers:
    server_specs.extend(layer.server_specs)
    native = (
      layer.blocked_native_tool_names
      + layer.served_native_tool_names
      + tuple(name for name, _ in layer.native_tool_commands)
    )
    if len(native) > 0 and harness != 'claude':
      raise ValueError(
        f'cannot declare native tools {native!r} on the {harness!r} harness; '
        'it serves only the tools the bro declares'
      )
    blocked_names.extend(layer.blocked_native_tool_names)
    for name, command in layer.native_tool_commands:
      narrowed.setdefault(name, []).append(command)
      handed_back[name] = 'narrowed to specific commands'
    for name in layer.served_native_tool_names:
      handed_back[name] = 'served whole'
  blocked = dict.fromkeys(blocked_names)
  for name, form in handed_back.items():
    # a tool handed back is one the harness serves, so it leaves the block set
    if name not in blocked:
      raise ValueError(
        f'{name} is {form} but never blocked; handing a native tool back means '
        'nothing where the bro does not withhold it'
      )
    del blocked[name]
  return _ToolSelection(
    server_specs=server_specs,
    blocked_tool_names=tuple(blocked),
    narrowed_tool_commands={
      name: tuple(dict.fromkeys(commands)) for name, commands in narrowed.items()
    },
  )


def _fold_man_pages(entries: list[DataSource | ManPage]) -> list[DataSource]:
  # the declared pages amount to one manual, mounted where the first of them was
  # declared — a namespace is one server, so a hierarchy contributing pages from
  # several classes still serves a single `read` tool over all of them.
  pages = [entry for entry in entries if isinstance(entry, ManPage)]
  sources: list[DataSource] = []
  folded = False
  for entry in entries:
    if not isinstance(entry, ManPage):
      sources.append(entry)
    elif not folded:
      sources.append(manual(pages))
      folded = True
  return sources


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
      if isinstance(component, mcp.ToolLayer):
        destinations.add('tools')
      elif isinstance(component, DataSource | ManPage):
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
  # contract, one entry per source — or per reference page (`man('<topic>')`),
  # which fold into a single manual.
  data_sources: ClassVar[list[Entry[DataSource | ManPage]]] = []
  tools: ClassVar[list[Entry[mcp.ToolLayer]]] = []
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
  # under the host's depth cap (see ride/ride/summon_control.py). MRO-walked and
  # unioned like `extra_secrets`.
  may_summon: tuple[str, ...] = ()
  # session-start steps for the session's workspace, applied to its root at
  # session start. every start of a session runs them, resumes included, so a
  # step is idempotent and leaves state the workspace already carries alone.
  # MRO-walked and concatenated like `extra_secrets`.
  provisioning: tuple[ProvisionStep, ...] = ()
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
  # bro-native one; set in __init__, consumed by `ride solo|along --raw`
  claude_system_prompt: str

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
    tool_entries: list[Entry[mcp.ToolLayer]] = []
    data_source_entries: list[Entry[DataSource | ManPage]] = []
    prompt_parts: list[str] = []
    extra_secret_names: list[str] = []
    may_summon_names: list[str] = []
    provision_steps: list[ProvisionStep] = []
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
      raw_provisioning = cls.__dict__.get('provisioning')
      if raw_provisioning is not None:
        provision_steps.extend(raw_provisioning)
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
    self._provisioning: tuple[ProvisionStep, ...] = tuple(provision_steps)
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
    selected_tools = mcp.select(
      tool_entries, harness='bro', creds=surface_creds, extra=self._feature_vocabulary
    )
    self._mcp_specs = _fold_tool_layers(selected_tools, 'bro').server_specs
    self._data_sources: list[DataSource] = _fold_man_pages(
      mcp.select(
        data_source_entries, harness='bro', creds=surface_creds, extra=self._feature_vocabulary
      )
    )
    # built lazily by _live_mcp_servers(): metadata surfaces (needed_secrets on
    # hosts, prompt composition) never construct live servers.
    self._live_mcp: Optional[list[llm_mcp.MCPServer]] = None
    # explicit `system_prompt=...` arg overrides MRO collection — escape hatch
    # for callers that need a dynamic prompt (e.g. PM injects current time).
    if system_prompt is not None:
      prompt_parts = [system_prompt] if len(system_prompt) > 0 else []
    # the bro's own persona: MRO-concatenated class system_prompt(s) under a
    # `# Persona: <name>` heading — the segment lands inside larger composed
    # prompts (below, and ride's append prompt), where headingless identity text
    # reads as a stray fragment. no shared / data-source / spells blocks here;
    # injected into dive-in Claude Code sessions (ride/ride/claude/system_prompt.py) so they
    # carry the bro's policies outside --raw mode.
    self.persona = (
      '\n\n'.join([f'# Persona: {self.name}', *prompt_parts]) if len(prompt_parts) > 0 else ''
    )
    shared = _load_shared_prompts()
    spell_instructions = self.spell_instructions()

    def compose(wire: mcp.Wire) -> str:
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
      return mcp.render_text(
        '\n\n'.join(parts),
        harness='bro',
        wire=wire,
        creds=credentials.known_names(),
        may_summon=summon.effective_may_summon(),
        extra=self._feature_vocabulary,
      ).strip()

    self.system_prompt = compose('bare')
    # the same prompt over mcp wire names — what a `ride solo|along --raw` session passes
    # as --system-prompt (ride/ride/claude/claude_argv.py).
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
    environment. an undeclared name reads as off — the probe answers for an
    arbitrary persona, unlike renders, whose closed universe makes an unknown
    name an error."""
    return name in self._features and feature(name).evaluate(self._feature_vocabulary)

  def provision_workspace(self, workspace: Path) -> None:
    """apply the declared session-start steps to `workspace`, the root of the
    tree the session works in."""
    for step in self._provisioning:
      step(workspace)

  @property
  def spells(self) -> dict[str, Path]:
    return spell_store.collect_spells(list(reversed(type(self).__mro__)))

  def get_spell_body(self, name: str, *, harness: mcp.Harness, wire: mcp.Wire) -> str:
    path = self.spells.get(name)
    if path is None:
      available = ', '.join(sorted(self.spells)) if len(self.spells) > 0 else '(none)'
      raise KeyError(f'no spell named {name!r}; available: {available}')
    spell = spell_store.load_spell(name, path)
    return mcp.render_text(
      spell.body,
      harness=harness,
      wire=wire,
      creds=credentials.known_names(),
      may_summon=summon.effective_may_summon(),
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

  def _selected_tools_for(self, harness: mcp.Harness) -> '_ToolSelection':
    selected: list[mcp.ToolLayer] = mcp.select(
      self._tool_entries,
      harness=harness,
      creds=credentials.known_names(),
      extra=self._feature_vocabulary,
    )
    return _fold_tool_layers(selected, harness)

  def blocked_tool_names(self, harness: mcp.Harness) -> tuple[str, ...]:
    """harness-native tool names blocked by this bro's selected layers."""
    return self._selected_tools_for(harness).blocked_tool_names

  def narrowed_tool_commands(self, harness: mcp.Harness) -> dict[str, tuple[str, ...]]:
    """harness-native tool name -> the commands this bro's selected layers narrow
    it to; the harness rejects every other command the tool is called with."""
    return self._selected_tools_for(harness).narrowed_tool_commands

  def _components_for(
    self, harness: mcp.Harness
  ) -> tuple[list[mcp.MCPServerSpec], list[DataSource]]:
    # the declared components that hold on `harness`. the bro-harness selection
    # is the one materialized in __init__ (prompt composition and the
    # live-server cache read it); any other harness selects on demand from the
    # raw entries.
    if harness == 'bro':
      return self._mcp_specs, self._data_sources
    specs = self._selected_tools_for(harness).server_specs
    sources = _fold_man_pages(
      mcp.select(
        self._data_source_entries,
        harness=harness,
        creds=credentials.known_names(),
        extra=self._feature_vocabulary,
      )
    )
    return specs, sources

  def needed_secrets(self, harness: mcp.Harness = 'bro') -> tuple[str, ...]:
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

  def optional_secrets(self, harness: mcp.Harness = 'bro') -> tuple[str, ...]:
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

  def _servers_with_spell_tools(
    self,
    servers: list[llm_mcp.MCPServer],
    *,
    harness: mcp.Harness,
    wire: mcp.Wire,
  ) -> list[llm_mcp.MCPServer]:
    if any(server.namespace == spell_store.NAMESPACE for server in servers):
      raise ValueError(f'namespace {spell_store.NAMESPACE!r} is reserved for bro framework tools')
    if len(self.spells) == 0:
      return servers
    return [*servers, spell_store.build_spell_server(self, harness=harness, wire=wire)]

  def _live_mcp_servers(self) -> list[llm_mcp.MCPServer]:
    # specs materialize here, on first tool use — always in a serving process,
    # post-secrets — and are built once: a live server may hold real resources
    # and every run through this bro reuses the same set.
    if self._live_mcp is None:
      self._live_mcp = [spec.build() for spec in self._mcp_specs]
      self._live_mcp.extend(ds.as_mcp_server() for ds in self._data_sources)
    return self._live_mcp

  def close(self) -> None:
    """release the live MCP servers this bro materialized; a bro whose tools were
    never used holds none. Best-effort: a failing teardown must not mask the
    outcome of whatever ran the bro."""
    if self._live_mcp is None:
      return
    for server in self._live_mcp:
      try:
        server.close()
      except Exception as error:
        log.warning('failed to close the %s server: %s', server.namespace, error)

  def assemble(
    self,
    *,
    harness: mcp.Harness,
    wire: mcp.Wire,
    include_raise: bool,
    live_run: Optional[LiveRun] = None,
  ) -> list[llm_mcp.MCPServer]:
    """materialize this declaration for one consuming surface."""
    if harness == 'bro':
      servers = list(self._live_mcp_servers())
    else:
      specs, sources = self._components_for(harness)
      servers = [spec.build() for spec in specs]
      servers.extend(source.as_mcp_server() for source in sources)
    servers.append(
      _build_service_server(
        self,
        include_raise=include_raise,
        harness=harness,
        wire=wire,
        live_run=live_run,
      )
    )
    return self._servers_with_spell_tools(servers, harness=harness, wire=wire)

  def system_prompt_for(self, *, hold: str) -> str:
    """the bro-native system prompt under a hold — the composed prompt plus the
    session fragments."""
    # the hold is pinned at run start, so the matching hold fragment is
    # injected rather than detected by the agent — run() defaults unattended,
    # send() guided, with the launch surfaces overriding per their --hold flag
    # (the level files are documented in prompts/AGENTS.md).
    fragment = session_fragment(
      hold,
      harness='bro',
      wire='bare',
      creds=credentials.known_names(),
    )
    return f'{self.system_prompt}\n\n{fragment}'
