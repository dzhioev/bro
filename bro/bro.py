import os
from abc import ABC
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Optional, Protocol, Self, cast

import bro.llm.llms.openai as llm_llms_openai
import bro.llm.mcp as llm_mcp
import bro.mcp as mcp
from bro import brash_policy, results, spells as spell_store, summon, watches
from bro.base import credentials, log
from bro.base.condition import (
  Condition,
  Contains,
  Entry,
  Iff,
  SetVariable,
  Variable,
  Variables,
  When,
  var,
)
from bro.base.offload import off_loop
from bro.broker.environment import BROKER_CHANNEL, BROKER_UPSTREAM
from bro.datasources.base import DataSource
from bro.datasources.file import FileSource
from bro.datasources.man import manual
from bro.harness import Harness, get_harness
from bro.launch.hold import UNATTENDED
from bro.llm.llm import EFFORT_LEVELS, NativeLLMSpec
from bro.llm.tracker import ToolStepSource
from bro.prompts import get_prompt, session_fragment
from bro.run_lifecycle import validate_answer
from bro.worker_types import type_name as worker_type_name

DEFAULT_LLM_SPEC: NativeLLMSpec = llm_llms_openai.LLMSpec(reasoning_effort='medium')

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


def _render_spells(*, include_cast: bool) -> str:
  run_instruction = (
    'call `bro::cast` with the enclosed text and follow the returned instructions. An error '
    'result is relayed as it came back — the spell is not carried here, the command is '
    'ambiguous, or an argument is missing — and no other spell stands in for it'
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
  drives the bro in this process. Every fact is read at call time: the trail
  opens after the service server is built, the tool position moves with every
  call, and the result store lives as long as the run. A process that assembles
  a bro without running one has none, and keeps its results in the session's
  store."""

  @property
  def trail_id(self) -> Optional[str]: ...

  @property
  def current_tool_step_id(self) -> Optional[ToolStepSource]: ...

  @property
  def result_store(self) -> results.Store: ...


class WatchRun(LiveRun, Protocol):
  @property
  def watch_store(self) -> watches.Store: ...

  @property
  def brash_policy(self) -> Optional[Path]: ...


RAISE_EXIT_STATUS = 1


class BroRaised(llm_mcp.ToolControlSignal):
  """Aborts a native Bro run."""

  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


class AnswerDelivered(llm_mcp.ToolControlSignal):
  """Ends a summoned native Bro run with its explicit answer."""

  def __init__(self, answer: str):
    super().__init__(answer)
    self.answer = answer


_RAISE_DESCRIPTION = (
  'abort the run because the request cannot be fulfilled. Call this when '
  'required credentials or API keys are missing, no appropriate tool or data '
  'source is available, the request contains contradictory constraints, the '
  'input is unclear or cannot be understood (gibberish, ambiguous, or missing '
  'the context needed to act), or any other blocker prevents completing the '
  'task. Do NOT reply with a clarifying question — no human will respond to it; '
  'raise instead. Pass a clear, specific reason — it surfaces to the caller as '
  'the failure cause. The call ends the session; nothing after it will run, so '
  'make the reason self-contained.'
)


def _raise_tool(harness: Harness, variables: Variables) -> llm_mcp.Tool:
  async def raise_session(reason: str) -> str:
    return await harness.end_session(reason, 'raised')

  return llm_mcp.FunctionTool(
    raise_session, name='raise', description=_RAISE_DESCRIPTION, variables=variables
  )


_ANSWER_DESCRIPTION = (
  'deliver the final answer of this summoned session to the summoner waiting on '
  'it, and end the session. This session runs on behalf of another session; call '
  'this exactly once, when the work is done, with a self-contained answer: the summoner '
  'sees nothing else of this session. A session that ends without this call '
  'reports no answer and surfaces to the summoner as a failure.'
  '{{when #tools contains raise}} An answer means the request was met; a request this run '
  'cannot fulfill, or declines, ends through `raise` with the reason instead.{{end}}'
  ' The call ends the session; nothing after it will run.'
)


def _answer_tool(harness: Harness, variables: Variables) -> llm_mcp.Tool:
  async def answer_session(answer: str) -> str:
    validate_answer(answer)
    return await harness.end_session(answer, 'ok')

  return llm_mcp.FunctionTool(
    answer_session, name='answer', description=_ANSWER_DESCRIPTION, variables=variables
  )


_SUMMON_DESCRIPTION = (
  'summon another bro: it runs your prompt in a new party or joins your party, as a quest '
  'whose id every `quest_*` tool takes. this call returns after host acceptance. pass `target` '
  '(a bro name; it must be in your `launch.bro.bros` set, and '
  'a target outside it — or a summon nested past the depth cap — fails immediately '
  'with the reason) and `prompt` (the full request, self-contained — the target '
  'shares no context with you). optional `timeout` (seconds, default 1800) bounds '
  'the run — an open-ended child (e.g. a dev run watching a PR through review) '
  'outlives the default and needs an explicit value sized in hours; optional '
  "`into` bases the child on a git ref instead of your workspace's "
  'current HEAD (uncommitted changes never transfer); optional `hold` sets the '
  "child's user-involvement level (default unattended). the child's run is shaped "
  "by the optional `harness` — an installed driving loop, the project's summon harness "
  'when omitted — and the optional '
  '`llm` — the LLM recipe it runs within that harness, written `provider:model:effort` '
  'with an optional `+fast` suffix and any field left empty '
  f"(effort is one of {', '.join(EFFORT_LEVELS)}; `::high` keeps the target's own "
  'provider and model, `:opus5` names a model; a recipe the harness cannot run fails the '
  'summon rather than switching the harness). its scope is shaped by '
  'the optional `grant` / `revoke` lists — entries are '
  f'`@bro`, `:launch.<type>`, or payload names ({summon.party_launch_choices()} for bro placement). '
  'a credential name is refused; use the optional `passes` list to make the child hold '
  'a credential instance covered by your pass rights, or configure the kind for the target bro. '
  'you can grant only launch names your own section covers; revokes are unrestricted, '
  'and restating a grant or revoke is harmless. the '
  'optional `share` list names artifact refs (from `artifact mint`) to hand the '
  'child read access to — only refs this session can itself read. '
  'the optional `talk` list widens the child quest from worker.say with owner.say, '
  'owner.question, worker.say, or worker.question. the optional `party` (`start` or '
  '`join`) and `isolation` (`boxed` or `unboxed`) fields place the child; an unmarked request '
  'starts boxed when permitted, otherwise unboxed. a join shares your tree and refuses '
  '`isolation`, `into`, and `manual`. acceptance returns the quest id; answers, questions, '
  'replies, refusals, and terminal states arrive through the session watch, and `quest_check` '
  'reads the retained outcome. `manual: true` registers a manual summon instead of spawning: '
  'acceptance returns a token and `ride` command to relay to the user, who launches the child '
  'session; manual refuses `timeout`/`hold`/`llm`/`harness`/`party`/`isolation` because the '
  'user’s launch owns them, and refuses `share` because no child workspace exists yet. it '
  'requires boxed or unboxed in its `launch.bro.party` set.'
)


_QUEST_CHECK_DESCRIPTION = (
  'read the outcome of a child quest by `quest_id` through the host journal, without '
  'blocking: a running state with its trail id, a question state with the open questions the '
  'child is stalled on (they wait on your reply, sent with `quest_say`), or the completed '
  'state with the answer. the end arrives through the session watch. unknown ids, evicted '
  'results, and failed or denied quests raise with their reason; `self` is refused, since the '
  'outcome of your own quest is yours to give.'
)


_QUEST_HISTORY_DESCRIPTION = (
  "read a quest's conversation by `quest_id` — `self` for your own — through the host "
  'journal, without blocking: its talk rights and the retained message tail, oldest first, '
  'with every open question marked `pending: true` in place, and `truncated: true` when older '
  'entries were dropped. answer each marked question from the other end with `quest_say` and '
  'its id as `reply_to`; new messages arrive through the session watch.'
)


_QUEST_SAY_DESCRIPTION = (
  'send a message that expects no reply on a quest by `quest_id` — a child quest, or `self` '
  "to your own summoner — or, with `reply_to`, the reply to a question. the quest's talk must "
  'permit the move. text over the message bound is refused with its size; mint an artifact '
  'and send the ref instead.'
)


_QUEST_ASK_DESCRIPTION = (
  'ask a question on a quest by `quest_id` — a child quest, or `self` to your own summoner — '
  'and return its id; `reply_to` makes it a counter-question to the question named. the reply '
  'arrives through the session watch and remains readable with `quest_history`. text over the '
  'message bound is refused with its size; mint an artifact and send the ref instead.'
)


_QUEST_SHARE_DESCRIPTION = (
  'hand an artifact ref this session can reach to a live child quest by `quest_id`. the host '
  'refuses an unknown, ended, non-bro, or unowned quest and a ref outside this session’s view.'
)


_QUEST_CANCEL_DESCRIPTION = (
  'ask the host to cancel a bro quest this session owns by `quest_id` and return when the '
  'request is accepted. the worker ends asynchronously, and whatever it launched in turn '
  'ends failed:orphaned with it; its end arrives through the session watch.'
)


_QUEST_WATCH_DESCRIPTION = (
  "set whether the session's quest watch wakes this session on its quiet lines or holds them "
  'until the next line that does wake it, which is the default. use it to follow a child live; '
  'returns the setting now in force.'
)


_QUEST_LIST_DESCRIPTION = (
  "list this session's caller-visible retained summon journal records, live first. "
  'each record carries its quest id, args, talk, pending questions, lifecycle state, '
  'timestamps, trail, and terminal outcome. use an id to recover an interrupted wait '
  'with `quest_check`.'
)


_BANNER_DESCRIPTION = (
  "return this session's environment facts as `key: value` lines: `isolation` "
  '(`boxed` or `unboxed`), workspace name and paths, the bro persona, the launch '
  'command, joined-party membership, the bros it may delegate to (`may_summon`), its quest '
  'chat rights (`talk`), launch permissions, and the trail it is recorded into (`trail_id`). call '
  'it once at session start to detect your '
  'environment.'
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


async def _run_summon_request(
  live_run: Optional[LiveRun],
  target: str,
  prompt: str,
  *,
  timeout: Optional[float],
  into: Optional[str],
  hold: Optional[str],
  grant: Optional[list[str]],
  revoke: Optional[list[str]],
  passes: Optional[list[str]],
  share: Optional[list[str]],
  llm: Optional[str],
  harness: Optional[str],
  party: Optional[Literal['start', 'join']],
  isolation: Optional[Literal['boxed', 'unboxed']],
  talk: Optional[list[str]],
  manual: bool,
) -> dict[str, Any]:
  from bro import summon as summon_client

  source = None if live_run is None else live_run.current_tool_step_id
  step_id = source['step_id'] if source is not None else None
  index = source['index'] if source is not None else None
  if manual:
    launch_owned = {
      'timeout': timeout,
      'hold': hold,
      'llm': llm,
      'harness': harness,
      'party': party,
      'isolation': isolation,
    }
    passed = sorted(name for name, value in launch_owned.items() if value is not None)
    if passed:
      raise ValueError(f"a manual summon's launch owns {', '.join(passed)}; drop the field(s)")
    if share is not None:
      raise ValueError(
        "a manual summon's workspace is not launched by summon control, so 'share' cannot be honored"
      )
    token = await off_loop(
      summon_client.summon_manual,
      target,
      prompt,
      into=into,
      grant=grant,
      revoke=revoke,
      passes=passes,
      talk=talk,
      step_id=step_id,
      index=index,
    )
    return {
      'state': 'accepted',
      'quest_id': token,
      'command': summon_client.manual_launch_command(token, target),
    }
  quest_id = await off_loop(
    summon_client.summon_detached,
    target,
    prompt,
    timeout=timeout,
    into=into,
    hold=hold,
    grant=grant,
    revoke=revoke,
    passes=passes,
    share=share,
    llm=llm,
    harness=harness,
    party=party,
    isolation=isolation,
    talk=talk,
    step_id=step_id,
    index=index,
  )
  return {'state': 'accepted', 'quest_id': quest_id}


def _summon_tool(variables: Variables, live_run: Optional[LiveRun]) -> llm_mcp.Tool:
  async def _summon(
    target: str,
    prompt: str,
    timeout: Optional[float] = None,
    into: Optional[str] = None,
    hold: Optional[str] = None,
    grant: Optional[list[str]] = None,
    revoke: Optional[list[str]] = None,
    passes: Optional[list[str]] = None,
    share: Optional[list[str]] = None,
    llm: Optional[str] = None,
    harness: Optional[str] = None,
    party: Optional[Literal['start', 'join']] = None,
    isolation: Optional[Literal['boxed', 'unboxed']] = None,
    talk: Optional[list[str]] = None,
    manual: bool = False,
  ) -> dict[str, Any]:
    return await _run_summon_request(
      live_run,
      target,
      prompt,
      timeout=timeout,
      into=into,
      hold=hold,
      grant=grant,
      revoke=revoke,
      passes=passes,
      share=share,
      llm=llm,
      harness=harness,
      party=party,
      isolation=isolation,
      talk=talk,
      manual=manual,
    )

  return llm_mcp.FunctionTool(
    _summon, name='summon', description=_SUMMON_DESCRIPTION, variables=variables
  )


def _quest_check_tool(variables: Variables) -> llm_mcp.Tool:
  from bro import quest as quest_client

  async def _quest_check(quest_id: str) -> dict[str, Any]:
    return quest_client.outcome_view(await off_loop(quest_client.check, quest_id))

  return llm_mcp.FunctionTool(
    _quest_check, name='quest_check', description=_QUEST_CHECK_DESCRIPTION, variables=variables
  )


def _quest_history_tool(variables: Variables) -> llm_mcp.Tool:
  from bro import quest as quest_client

  async def _quest_history(quest_id: str) -> dict[str, Any]:
    return quest_client.history_view(await off_loop(quest_client.history, quest_id))

  return llm_mcp.FunctionTool(
    _quest_history,
    name='quest_history',
    description=_QUEST_HISTORY_DESCRIPTION,
    variables=variables,
  )


def _quest_say_tool(variables: Variables) -> llm_mcp.Tool:
  from bro import quest as quest_client

  async def _quest_say(quest_id: str, text: str, reply_to: Optional[str] = None) -> dict[str, Any]:
    sent_on = await off_loop(quest_client.say, quest_id, text, reply_to=reply_to)
    return {'state': 'sent', 'quest_id': sent_on}

  return llm_mcp.FunctionTool(
    _quest_say, name='quest_say', description=_QUEST_SAY_DESCRIPTION, variables=variables
  )


def _quest_ask_tool(variables: Variables) -> llm_mcp.Tool:
  from bro import quest as quest_client

  async def _quest_ask(quest_id: str, text: str, reply_to: Optional[str] = None) -> dict[str, Any]:
    asked = await off_loop(quest_client.ask, quest_id, text, reply_to=reply_to)
    return {
      'state': 'asked',
      'quest_id': asked.quest_id,
      'question_id': asked.question_id,
    }

  return llm_mcp.FunctionTool(
    _quest_ask, name='quest_ask', description=_QUEST_ASK_DESCRIPTION, variables=variables
  )


def _quest_share_tool(variables: Variables) -> llm_mcp.Tool:
  from bro import quest as quest_client

  async def _quest_share(quest_id: str, ref: str) -> dict[str, Any]:
    shared_with = await off_loop(quest_client.share, quest_id, ref)
    return {'state': 'shared', 'quest_id': shared_with, 'ref': ref}

  return llm_mcp.FunctionTool(
    _quest_share, name='quest_share', description=_QUEST_SHARE_DESCRIPTION, variables=variables
  )


def _quest_list_tool(variables: Variables) -> llm_mcp.Tool:
  from bro import quest as quest_client

  async def _quest_list() -> dict[str, Any]:
    return await off_loop(quest_client.list_quests)

  return llm_mcp.FunctionTool(
    _quest_list, name='quest_list', description=_QUEST_LIST_DESCRIPTION, variables=variables
  )


def _quest_watch_tool(live_run: Optional[WatchRun], variables: Variables) -> llm_mcp.Tool:
  def quest_watch(wake_on_quiet: bool) -> dict[str, Any]:
    store = watches.session_store() if live_run is None else live_run.watch_store
    store.set_wake_on_quiet(watches.SESSION_WATCH_COMMAND, wake_on_quiet)
    return {'wake_on_quiet': wake_on_quiet}

  return llm_mcp.FunctionTool(
    quest_watch, name='quest_watch', description=_QUEST_WATCH_DESCRIPTION, variables=variables
  )


def _quest_cancel_tool(variables: Variables) -> llm_mcp.Tool:
  from bro import quest as quest_client

  async def _quest_cancel(quest_id: str) -> dict[str, Any]:
    status = await off_loop(quest_client.request_cancel, quest_id)
    return quest_client.cancel_view(status)

  return llm_mcp.FunctionTool(
    _quest_cancel, name='quest_cancel', description=_QUEST_CANCEL_DESCRIPTION, variables=variables
  )


_WATCH_DESCRIPTION = (
  'start a shell command line as a detached producer for the rest of this session. under this '
  'persona’s finite command list the line runs in brash, which starts only the commands the list '
  'admits and stops at the first it refuses, exiting 126 with the refusal among the watch’s lines; '
  'unrestricted personas run it in `bash -c`. its bounded output reaches the session through the '
  'shared watch store. a line the command marks quiet arrives with the next line that wakes the '
  'session, unless `wake_on_quiet` makes quiet lines wake it too.'
)

_UNWATCH_DESCRIPTION = (
  'stop the watch of the command line it names and its whole process group. the runtime-owned '
  'session watch cannot be stopped through this tool.'
)


def _watch_tools(
  *,
  live_run: Optional[WatchRun],
  unrestricted: bool,
  variables: Variables,
) -> list[llm_mcp.Tool]:
  def store() -> watches.Store:
    return watches.session_store() if live_run is None else live_run.watch_store

  def policy() -> Optional[Path]:
    if unrestricted:
      return None
    return brash_policy.published() if live_run is None else live_run.brash_policy

  def watch(command: str, wake_on_quiet: bool = False) -> str:
    started = store().start(command, policy(), wake_on_quiet=wake_on_quiet)
    return f'watching `{started.command}`'

  def unwatch(command: str) -> str:
    store().stop(command)
    return f'stopped watching `{command}`'

  return [
    llm_mcp.FunctionTool(watch, name='watch', description=_WATCH_DESCRIPTION, variables=variables),
    llm_mcp.FunctionTool(
      unwatch, name='unwatch', description=_UNWATCH_DESCRIPTION, variables=variables
    ),
  ]


# the core service roster's tool names. Each service server extends this closed
# `#tools` universe with the selected harness's own tools.
_CORE_SERVICE_TOOL_NAMES = (
  'banner',
  results.PAGE_TOOL,
  results.LIST_TOOL,
  'cast',
  'raise',
  'answer',
  'summon',
  'quest_check',
  'quest_history',
  'quest_say',
  'quest_ask',
  'quest_share',
  'quest_list',
  'quest_cancel',
  'quest_watch',
  'watch',
  'unwatch',
)


def _build_service_server(
  bro: 'BaseBro',
  *,
  hold: str,
  harness: mcp.HarnessLike,
  result_store: results.StoreSource,
  live_run: Optional[LiveRun] = None,
) -> llm_mcp.MCPServer:
  # built only on the paths that serve a bro, never at construction: deriving the
  # FunctionTool schemas below pulls the mcp/fastmcp stack (~1s), which metadata
  # surfaces (credential scoping, prompt composition, `bro show`) must not pay.
  # the core roster is decided by the caller's surface and local process state:
  # `banner`, `page` and `results` are unconditional; `cast` needs spells and its
  # optional secret; `raise` only makes sense unattended (a caller to abort to,
  # where an attended session reports to its human) and on a harness able to end
  # this session;
  # `answer` is the summoned run's delivery surface — it needs the summoned mark,
  # broker intent, and a harness able to end this session; the summon tools need
  # the same intent. The harness contributes the tools it serves itself. The
  # combined roster then feeds the tools' rendering vocabulary: service tools are
  # harness features, the one tool surface that conditions on system and
  # harness-contributed facts, injected next to the `#tools` roster.
  from bro.summon import summoned

  session_harness = _harness_object(harness)
  has_cast = len(bro.spell_paths) > 0 and spell_store.cast_available()
  has_raise = hold == UNATTENDED and session_harness.can_end_session()
  has_broker = any(os.environ.get(name) is not None for name in (BROKER_CHANNEL, BROKER_UPSTREAM))
  has_answer = has_broker and summoned() and session_harness.can_end_session()
  has_quest_watch = has_broker and len(summon.effective_may_summon()) > 0
  brash = bro.reach().brash
  harness_tools = session_harness.own_tools(bro, live_run)
  harness_tool_names = tuple(tool.name for tool in harness_tools)
  duplicate_names = {
    name
    for name in harness_tool_names
    if harness_tool_names.count(name) > 1 or name in _CORE_SERVICE_TOOL_NAMES
  }
  if len(duplicate_names) > 0:
    raise ValueError(
      f'harness {session_harness.name!r} owns duplicate service tools: {sorted(duplicate_names)}'
    )

  mounted = ['banner', results.PAGE_TOOL, results.LIST_TOOL]
  if has_cast:
    mounted.append('cast')
  if has_raise:
    mounted.append('raise')
  if has_answer:
    mounted.append('answer')
  if has_broker:
    mounted.extend(
      [
        'summon',
        'quest_check',
        'quest_history',
        'quest_say',
        'quest_ask',
        'quest_share',
        'quest_list',
        'quest_cancel',
      ]
    )
  if has_quest_watch:
    mounted.append('quest_watch')
  if brash is not None:
    mounted.extend(['watch', 'unwatch'])
  mounted.extend(harness_tool_names)
  tool_universe = (*_CORE_SERVICE_TOOL_NAMES, *harness_tool_names)
  variables: Variables = {
    **mcp.surface_variables(harness=harness),
    'tools': SetVariable(frozenset(mounted), universe=frozenset(tool_universe)),
  }

  tools: list[llm_mcp.Tool] = [
    _banner_tool(bro, live_run, variables),
    *results.store_tools(result_store),
  ]
  if has_cast:
    tools.append(spell_store.build_cast_tool(bro, harness=harness, hold=hold))
  if has_raise:
    tools.append(_raise_tool(session_harness, variables))
  if has_answer:
    tools.append(_answer_tool(session_harness, variables))
  if has_broker:
    tools.append(_summon_tool(variables, live_run))
    tools.append(_quest_check_tool(variables))
    tools.append(_quest_history_tool(variables))
    tools.append(_quest_say_tool(variables))
    tools.append(_quest_ask_tool(variables))
    tools.append(_quest_share_tool(variables))
    tools.append(_quest_list_tool(variables))
    tools.append(_quest_cancel_tool(variables))
  if has_quest_watch:
    tools.append(_quest_watch_tool(cast(Optional[WatchRun], live_run), variables))
  if brash is not None:
    tools.extend(
      _watch_tools(
        live_run=cast(Optional[WatchRun], live_run),
        unrestricted=brash.unrestricted,
        variables=variables,
      )
    )
  tools.extend(harness_tools)
  assert [tool.name for tool in tools] == mounted
  server = llm_mcp.InProcessMCPServer('bro', tools)
  server.tool_universe = tool_universe
  return server


def _result_store(live_run: Optional[LiveRun]) -> results.StoreSource:
  if live_run is None:
    return results.session_store
  return lambda: live_run.result_store


def _harness_object(harness: mcp.HarnessLike) -> Harness:
  """Resolve the selected harness object."""
  if isinstance(harness, Harness):
    return harness
  return get_harness(harness)


def _built(spec: mcp.MCPServerSpec) -> llm_mcp.MCPServer:
  server = spec.build()
  if server.namespace != spec.namespace:
    raise ValueError(
      f'a spec declared for namespace {spec.namespace!r} built a server for {server.namespace!r}'
    )
  return server


def feature(name: str) -> Condition:
  """membership condition on the bro's `#features` vocabulary — the code
  spelling of the `#features contains <name>` directive, for gating
  `tools` / `data_sources` entries: `when(feature('brog'), mount(brog_mcp.toolset))`."""
  return var('features').contains(name)


def _feature_variables(features: dict[str, Condition | bool]) -> Variables:
  def enabled(name: str) -> bool:
    gate = features[name]
    if isinstance(gate, bool):
      return gate
    return gate.evaluate(mcp.surface_variables(creds=credentials.known_names()))

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


def _reduce_class_entries(
  class_name: str, layers: list[mcp.ToolLayer]
) -> dict[mcp.ReachKey, mcp.ReachEntry]:
  """one class's selected entries, reduced to one entry per key."""
  reduced: dict[mcp.ReachKey, mcp.ReachEntry] = {}
  for layer in layers:
    for entry in layer.reach_entries:
      current = reduced.get(entry.key)
      if current is None:
        reduced[entry.key] = entry
        continue
      merged = current.merge(entry)
      if merged is None:
        raise ValueError(
          f'{class_name}.tools declares {current} and {entry} under one key ({entry.key}); '
          'they do not reduce to one entry, and list order does not decide between them'
        )
      reduced[entry.key] = merged
  return reduced


def _fold_reach(declarations: list[tuple[str, list[mcp.ToolLayer]]]) -> mcp.Reach:
  """the reach `declarations` amount to: each declaring class's name and its
  selected layers, nearest class first, as the MRO lists them. The first class
  that declares a key decides it."""
  decided: dict[mcp.ReachKey, mcp.ReachEntry] = {}
  for class_name, layers in declarations:
    for key, entry in _reduce_class_entries(class_name, layers).items():
      decided.setdefault(key, entry)
  # keys keep the order a base-to-derived reading first declares them in
  order = dict.fromkeys(
    entry.key
    for _, layers in reversed(declarations)
    for layer in layers
    for entry in layer.reach_entries
  )
  groups: dict[str, mcp.ReachEntry] = {}
  server_specs: list[mcp.MCPServerSpec] = []
  sources: list[Optional[DataSource]] = []
  pages: list[FileSource] = []
  for key in order:
    entry = decided[key]
    if isinstance(entry, mcp.Files | mcp.Brash | mcp.Web | mcp.Delegation):
      groups[key.name] = entry
    elif isinstance(entry, mcp.Mount | mcp.Cli | mcp.Server):
      server_specs.append(entry.spec)
    elif isinstance(entry, mcp.Source):
      sources.append(entry.source)
    elif isinstance(entry, mcp.Man):
      # the pages amount to one manual, mounted where the first of them was
      # declared: a namespace is one server, so pages from several classes
      # share a single `read` tool
      if len(pages) == 0:
        sources.append(None)
      pages.append(entry.page)
    elif not isinstance(entry, mcp.Revoked):
      raise TypeError(f'unknown reach entry {entry!r}')
  return mcp.Reach(
    files=cast(Optional[mcp.Files], groups.get('files')),
    brash=cast(Optional[mcp.Brash], groups.get('brash')),
    web=cast(Optional[mcp.Web], groups.get('web')),
    delegation=cast(Optional[mcp.Delegation], groups.get('delegation')),
    server_specs=tuple(server_specs),
    sources=tuple(manual(pages) if entry is None else entry for entry in sources),
  )


# attributes that once held component declarations -> the guidance for each
_RETIRED_COMPONENT_DECLARATION_ATTRIBUTES = {
  'mcp_servers': "'mcp_servers' was renamed to 'tools'",
  'data_sources': (
    "'data_sources' folded into 'tools': declare a data source as source(...) and a "
    "reference page as man('<topic>')"
  ),
}


def _gate_credential(gate: Condition | bool) -> Optional[str]:
  """the credential kind a `creds.contains(<kind>)` gate probes; None for any
  other gate."""
  if not isinstance(gate, Contains):
    return None
  container = gate.container
  if (
    isinstance(container, Variable) and container.name == 'creds' and isinstance(gate.element, str)
  ):
    return gate.element
  return None


def _validate_credential_gate(gate: Condition | bool, declaration: str) -> None:
  kind = _gate_credential(gate)
  if kind is not None:
    credentials.require_kind_declaration(kind, declaration)


def _declared_components(entries: Iterable[Entry[Any]], declaration: str) -> list[tuple[Any, str]]:
  components: list[tuple[Any, str]] = []
  for index, entry in enumerate(entries):
    entry_declaration = f'{declaration}[{index}]'
    if isinstance(entry, When):
      _validate_credential_gate(entry.condition, f'{entry_declaration} condition')
      components.append((entry.item, entry_declaration))
    elif isinstance(entry, Iff):
      for branch_index, (gate, item) in enumerate(entry.branches):
        branch_declaration = f'{entry_declaration} branch {branch_index}'
        _validate_credential_gate(gate, f'{branch_declaration} condition')
        components.append((item, branch_declaration))
      if entry.otherwise is not None:
        components.append((entry.otherwise[0], f'{entry_declaration} else branch'))
    else:
      components.append((entry, entry_declaration))
  return components


def _entry_secrets(entry: mcp.ReachEntry) -> Optional[tuple[str, tuple[str, ...], tuple[str, ...]]]:
  """what an entry's manifest names, its needed and its optional credentials; None
  for an entry that mounts nothing."""
  if isinstance(entry, mcp.Mount | mcp.Cli | mcp.Server):
    spec = entry.spec
    return f'MCP server {spec.namespace!r}', spec.needed_secrets, spec.optional_secrets
  if isinstance(entry, mcp.Source):
    component = entry.source
  elif isinstance(entry, mcp.Man):
    component = entry.page
  else:
    return None
  return type(component).__name__, component.needed_secrets, component.optional_secrets


def _validate_tool_declarations(entries: Iterable[Entry[Any]], declaration: str) -> None:
  for component, component_declaration in _declared_components(entries, declaration):
    if isinstance(component, DataSource):
      raise TypeError(
        f'{component_declaration} is a data source; declare it as source(...) in tools'
      )
    if not isinstance(component, mcp.ToolLayer):
      raise TypeError(f'{component_declaration} is {component!r}, not a ToolLayer')
    for entry in component.reach_entries:
      secrets = _entry_secrets(entry)
      if secrets is None:
        continue
      manifest, needed, optional = secrets
      for name in needed:
        credentials.require_kind_declaration(
          name, f'{component_declaration} {manifest}.needed_secrets'
        )
      for name in optional:
        credentials.require_kind_declaration(
          name, f'{component_declaration} {manifest}.optional_secrets'
        )


def _declares_components(value: object) -> bool:
  entries = value if isinstance(value, list) else (value,)
  for entry in entries:
    if isinstance(entry, When):
      components = (entry.item,)
    elif isinstance(entry, Iff):
      components = tuple(item for _, item in entry.branches) + (entry.otherwise or ())
    else:
      components = (entry,)
    if any(isinstance(component, mcp.ToolLayer | DataSource) for component in components):
      return True
  return False


class BaseBro(ABC):
  name: str
  description: str
  llm_spec: NativeLLMSpec = DEFAULT_LLM_SPEC
  # the bro's tool reach: tool groups, mounts, `cli` tools, data sources, and
  # reference pages, each entry keyed by its kind and a name. entries may be
  # wrapped with `bro.base.condition.when(...)` / grouped with `iff(...)` to gate
  # them on `#creds` and `#features`; a wrapped entry whose condition does not
  # hold is omitted before keys resolve. each key resolves along the MRO to the
  # first class declaring it (`_fold_reach`).
  tools: ClassVar[list[Entry[mcp.ToolLayer]]] = []
  # named optional capabilities: feature name → the gate deciding whether the
  # feature is on — a `Condition` over the environment's resolvable credentials
  # (`creds.contains('brog')`), or a plain bool constant as in `when` (True
  # pins the feature on, False disables it). one declaration switches every
  # consuming site together: components gate via `when(feature('<name>'), …)`,
  # static text via `{{iff #features contains <name>}}` — so a gated component
  # enters the manifest, mounts, and renders its text only where its gates
  # resolve. the credential a `creds.contains(<kind>)` gate probes is the
  # feature's own, tiered with it: in `optional_secrets()` while gated, in
  # `needed_secrets()` once pinned on. MRO-walked, with derived classes
  # overriding parents per name — `{'<name>': True}` pins an inherited
  # feature on, turning its components and that credential into hard
  # requirements. False is terminal: redeclaring a
  # feature a base class disabled fails construction, so an opt-out binds the
  # whole sub-hierarchy.
  features: ClassVar[dict[str, Condition | bool]] = {}
  # credentials no component expresses — the escape hatch for a bro's environment
  # needs. MRO-walked and unioned, so a subclass declares only what it adds.
  # folded into `needed_secrets()`.
  extra_secrets: tuple[str, ...] = ()
  # bros this bro may summon — the targets seeded into a session's launch section. root sessions get
  # it adjusted per session by `--grant @bro`/`--revoke @bro`; a summoned child
  # follows the bare seeds, so summons chain transitively through seeded bros
  # under the host's depth cap (see ride/ride/bro_worker.py). MRO-walked and
  # unioned like `extra_secrets`.
  may_summon: tuple[str, ...] = ()
  # worker types this bro may launch, seeded as whole `:launch.<type>` keys before
  # the launch's configured layers fold. `bro` is always seeded by the framework.
  # MRO-walked and unioned like `may_summon`.
  may_launch: tuple[str, ...] = ()
  # session-start steps for the session's workspace, applied to its root at
  # session start. every start of a session runs them, resumes included, so a
  # step is idempotent and leaves state the workspace already carries alone.
  # MRO-walked and concatenated like `extra_secrets`.
  provisioning: tuple[ProvisionStep, ...] = ()
  # the bro's spells: markdown files named relative to the `spells/` directory
  # beside the declaring module, each served as `spell::<file stem>`.
  # MRO-walked, a derived class's file replacing a parent's of the same name.
  spells: tuple[str, ...] = ()
  # subclasses declare their own `system_prompt = "..."` as a class attribute;
  # `__init__` walks the MRO from base to derived and concatenates each class's
  # own contribution. so a `ReviewDev(Dev)` subclass declares only what it adds —
  # Dev's prompt (and Bro's) are picked up automatically. inherit directly from
  # BaseBro to opt out of the concrete `Bro`'s shared defaults.
  system_prompt: str = ''
  # the bro's own class prompts (MRO-concatenated); set in __init__
  persona: str

  def __init_subclass__(cls, **kwargs: Any) -> None:
    super().__init_subclass__(**kwargs)
    for attribute_name, value in vars(cls).items():
      if attribute_name == 'tools' or not _declares_components(value):
        continue
      message = (
        f'{cls.__name__}.{attribute_name} contains component declarations under an attribute '
        "BaseBro does not read; move them to 'tools'"
      )
      retired = _RETIRED_COMPONENT_DECLARATION_ATTRIBUTES.get(attribute_name)
      if retired is not None:
        message += f'; {retired}'
      raise TypeError(message)

  def __init__(self, system_prompt: Optional[str] = None):
    tool_declarations: list[tuple[str, list[Entry[mcp.ToolLayer]]]] = []
    prompt_parts: list[str] = []
    extra_secret_names: list[str] = []
    may_summon_names: list[str] = []
    may_launch_types: list[str] = []
    provision_steps: list[ProvisionStep] = []
    spell_paths: dict[str, Path] = {}
    feature_gates: dict[str, Condition | bool] = {}
    feature_credentials: dict[str, str] = {}
    for cls in reversed(type(self).__mro__):
      raw_tools = cls.__dict__.get('tools')
      if raw_tools is not None:
        _validate_tool_declarations(raw_tools, f'{cls.__name__}.tools')
        tool_declarations.insert(0, (cls.__name__, list(raw_tools)))
      raw_prompt = cls.__dict__.get('system_prompt')
      if isinstance(raw_prompt, str) and len(raw_prompt) > 0:
        prompt_parts.append(raw_prompt)
      raw_extra = cls.__dict__.get('extra_secrets')
      if raw_extra is not None:
        for name in raw_extra:
          credentials.require_kind_declaration(name, f'{cls.__name__}.extra_secrets')
        extra_secret_names.extend(raw_extra)
      raw_summon = cls.__dict__.get('may_summon')
      if raw_summon is not None:
        may_summon_names.extend(raw_summon)
      raw_launch = cls.__dict__.get('may_launch')
      if raw_launch is not None:
        if not isinstance(raw_launch, tuple):
          raise TypeError(f'{cls.__name__}.may_launch must be a tuple of worker type names')
        for worker_type in raw_launch:
          try:
            worker_type_name(worker_type)
          except ValueError as error:
            raise ValueError(f'{cls.__name__}.may_launch: {error}') from error
          if worker_type == 'bro':
            raise ValueError(f'{cls.__name__}.may_launch must not name bro; the framework seeds it')
          may_launch_types.append(worker_type)
      raw_provisioning = cls.__dict__.get('provisioning')
      if raw_provisioning is not None:
        provision_steps.extend(raw_provisioning)
      raw_spells = cls.__dict__.get('spells')
      if raw_spells is not None:
        spell_paths.update(spell_store.declared_spells(cls, raw_spells))
      raw_features = cls.__dict__.get('features')
      if raw_features is not None:
        for feature_name, gate in raw_features.items():
          _validate_credential_gate(gate, f'{cls.__name__}.features[{feature_name!r}]')
          if feature_gates.get(feature_name) is False and gate is not False:
            raise ValueError(
              f'{cls.__name__} re-enables feature {feature_name!r} disabled by a base '
              'class; a False gate is terminal for the sub-hierarchy'
            )
          feature_gates[feature_name] = gate
          kind = _gate_credential(gate)
          if kind is not None:
            feature_credentials[feature_name] = kind
    self._extra_secrets: tuple[str, ...] = tuple(extra_secret_names)
    self._may_summon: tuple[str, ...] = tuple(may_summon_names)
    self._may_launch: tuple[str, ...] = tuple(may_launch_types)
    self._provisioning: tuple[ProvisionStep, ...] = tuple(provision_steps)
    self._spells: dict[str, Path] = spell_paths
    for spell_name, spell_path in self._spells.items():
      spell_store.load_spell(spell_name, spell_path)
    self._features: dict[str, Condition | bool] = feature_gates
    self._feature_credentials: dict[str, str] = feature_credentials
    # the membership probe is lazy, so the vocabulary built here stays current
    # with the store — only selection (below) bakes feature truth in.
    self._feature_vocabulary: Variables = _feature_variables(feature_gates)
    self._tool_declarations = tool_declarations
    # harness name -> the live servers built for it
    self._live_mcp: dict[str, list[llm_mcp.MCPServer]] = {}
    self._system_prompt_override: Optional[str] = None
    # explicit `system_prompt=...` arg overrides MRO collection — escape hatch
    # for callers that need a dynamic prompt (e.g. PM injects current time).
    if system_prompt is not None:
      prompt_parts = [system_prompt] if len(system_prompt) > 0 else []
    self.persona = (
      '\n\n'.join([f'# Persona: {self.name}', *prompt_parts]) if len(prompt_parts) > 0 else ''
    )
    # a declaration whose entries do not fold fails here, not at first use
    self.reach()

  def composed_prompt(self, harness: mcp.HarnessLike, *, hold: str) -> str:
    """Compose the declaration with the selected harness and session hold."""
    harness = _harness_object(harness)
    data_sources = list(self.reach().sources)
    parts = []
    shared = _load_shared_prompts()
    if len(shared) > 0:
      parts.append(shared)
    if len(self.persona) > 0:
      parts.append(self.persona)
    parts.append(get_prompt('tool_names.md').strip())
    if len(data_sources) > 0:
      parts.append(_render_data_sources(data_sources))
    spell_instructions = self.spell_instructions()
    if len(spell_instructions) > 0:
      parts.append(spell_instructions)
    instructions = harness.prompt_instructions()
    if len(instructions) > 0:
      parts.append(instructions)
    return mcp.render_text(
      '\n\n'.join(parts),
      harness=harness,
      creds=credentials.known_names(),
      may_summon=summon.effective_may_summon(),
      hold=hold,
      extra=self._feature_vocabulary,
    ).strip()

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
    `DataSource.vocabulary`. Any surface that renders `persona` on its own must
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
  def spell_paths(self) -> Mapping[str, Path]:
    """the bro's spell files by spell name, read-only."""
    return MappingProxyType(self._spells)

  def get_spell_body(self, name: str, *, harness: mcp.HarnessLike, hold: str) -> str:
    path = self._spells.get(name)
    if path is None:
      available = ', '.join(sorted(self._spells)) if len(self._spells) > 0 else '(none)'
      raise KeyError(f'no spell named {name!r}; available: {available}')
    spell = spell_store.load_spell(name, path)
    return mcp.render_text(
      spell.body,
      harness=harness,
      creds=credentials.known_names(),
      may_summon=summon.effective_may_summon(),
      hold=hold,
      extra=self._feature_vocabulary,
    ).strip()

  def spell_descriptions(self) -> list[tuple[str, str]]:
    return [
      (name, spell_store.load_spell(name, path).description) for name, path in self._spells.items()
    ]

  def spell_instructions(self) -> str:
    if len(self.spell_descriptions()) == 0:
      return ''
    return _render_spells(include_cast=spell_store.cast_available())

  def reach(self) -> mcp.Reach:
    """the harness-neutral tool reach this declaration folds to in this
    environment: each class's entries selected on `#creds` and `#features`,
    then each key resolved along the MRO."""
    known = credentials.known_names()
    return _fold_reach(
      [
        (class_name, mcp.select(entries, creds=known, extra=self._feature_vocabulary))
        for class_name, entries in self._tool_declarations
      ]
    )

  def _components_for(self, harness: Harness) -> tuple[list[mcp.MCPServerSpec], list[DataSource]]:
    """the servers and data sources `harness` mounts for this bro: those its
    reach declares, after the ones the harness serves the reach's groups with."""
    reach = self.reach()
    return [*harness.serve(reach).server_specs, *reach.server_specs], list(reach.sources)

  def _feature_secrets(self, *, pinned: bool) -> set[str]:
    return {
      kind
      for name, kind in self._feature_credentials.items()
      if self._features[name] is not False and (self._features[name] is True) == pinned
    }

  def needed_secrets(self, harness: mcp.HarnessLike) -> tuple[str, ...]:
    # the bro's component credential manifest for a consuming harness: the union
    # of each declared MCP server's + data source's `needed_secrets`, over only
    # the components that hold on `harness` — a surface never hydrates a secret
    # of a component it doesn't mount — plus the bro's MRO-collected
    # `extra_secrets` and the credentials of its pinned-on features. NOT the LLM
    # key — that is added only by surfaces that run the bro as an LLM process
    # (`bro run` / `bro chat`); a claude-code session themed as the bro uses its
    # own auth, not the bro's spec. the host hydrates the
    # per-surface set into a scoped store; a secret used but not declared
    # surfaces as SecretNotFound — an under-declaration to fix.
    specs, sources = self._components_for(_harness_object(harness))
    names: set[str] = set()
    for spec in specs:
      names.update(_component_needed_secrets(spec))
    for ds in sources:
      names.update(_component_needed_secrets(ds))
    names.update(self._extra_secrets)
    names.update(self._feature_secrets(pinned=True))
    return tuple(sorted(names))

  def optional_secrets(self, harness: mcp.HarnessLike) -> tuple[str, ...]:
    # the bro's best-effort credential tier: the union of each declared MCP
    # server's + data source's `optional_secrets` over the same per-harness
    # component set as `needed_secrets`, plus the credentials of its gated
    # features and the cast key when this bro has spells. minus anything
    # already required — a hard requirement is never downgraded. an unpicked
    # optional empty instance may be absent without failing a managed launch.
    specs, sources = self._components_for(_harness_object(harness))
    names: set[str] = set()
    for spec in specs:
      names.update(_component_optional_secrets(spec))
    for ds in sources:
      names.update(_component_optional_secrets(ds))
    names.update(self._feature_secrets(pinned=False))
    if len(self._spells) > 0:
      names.add(spell_store.CAST_SECRET)
    return tuple(sorted(names - set(self.needed_secrets(harness))))

  def missing_secrets(self, harness: mcp.HarnessLike) -> tuple[str, ...]:
    # every required name — the component manifest plus the LLM key, since
    # run()/send() execute the bro as an LLM process — that does not resolve in
    # this process's credential store. the optional tier is never gated.
    required = set(self.needed_secrets(harness)) | set(self.llm_spec.needed_secrets())
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
    self, servers: list[llm_mcp.MCPServer], *, harness: mcp.HarnessLike, hold: str
  ) -> list[llm_mcp.MCPServer]:
    if any(server.namespace == spell_store.NAMESPACE for server in servers):
      raise ValueError(f'namespace {spell_store.NAMESPACE!r} is reserved for bro framework tools')
    if len(self._spells) == 0:
      return servers
    return [*servers, spell_store.build_spell_server(self, harness=harness, hold=hold)]

  def _live_mcp_servers(self, harness: mcp.HarnessLike) -> list[llm_mcp.MCPServer]:
    # specs materialize here, on first tool use — always in a serving process,
    # post-secrets — and are built once per harness: a live server may hold real
    # resources and every run through this bro on one harness reuses the same set.
    session_harness = _harness_object(harness)
    servers = self._live_mcp.get(session_harness.name)
    if servers is None:
      specs, sources = self._components_for(session_harness)
      servers = [_built(spec) for spec in specs]
      servers.extend(source.as_mcp_server() for source in sources)
      self._live_mcp[session_harness.name] = servers
    return servers

  def close(self) -> None:
    """release the live MCP servers this bro materialized; a bro whose tools were
    never used holds none. Best-effort: a failing teardown must not mask the
    outcome of whatever ran the bro."""
    for server in (server for servers in self._live_mcp.values() for server in servers):
      try:
        server.close()
      except Exception as error:
        log.warning('failed to close the %s server: %s', server.namespace, error)

  def assemble(
    self,
    *,
    harness: mcp.HarnessLike,
    hold: str,
    live_run: Optional[LiveRun] = None,
  ) -> list[llm_mcp.MCPServer]:
    """materialize this declaration for one consuming surface, in a session
    under `hold`, every tool's result kept in the run's result store."""
    result_store = _result_store(live_run)
    servers = list(self._live_mcp_servers(harness))
    servers.append(
      _build_service_server(
        self, hold=hold, harness=harness, result_store=result_store, live_run=live_run
      )
    )
    return results.kept(
      self._servers_with_spell_tools(servers, harness=harness, hold=hold), result_store
    )

  def system_prompt_for(self, *, hold: str, harness: mcp.HarnessLike) -> str:
    """The selected harness's composed prompt plus the session fragments."""
    # the hold is pinned at run start, so the matching hold fragment is
    # injected rather than detected by the agent — run() defaults unattended,
    # send() guided, with the launch surfaces overriding per their --hold flag
    # (the level files are documented in prompts/AGENTS.md).
    harness = _harness_object(harness)
    fragment = session_fragment(
      hold,
      harness=harness,
      creds=credentials.known_names(),
      talk=summon.talk(),
    )
    prompt = (
      self._system_prompt_override
      if self._system_prompt_override is not None
      else self.composed_prompt(harness, hold=hold)
    )
    return f'{prompt}\n\n{fragment}'
