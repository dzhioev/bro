import asyncio
import json
import os
import sys
from abc import ABC
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, Optional, Self

import llm.llms.chat_gpt
import llm.mcp
from base import credentials, log
from base.condition import Entry, SetVariable, Variables
from bro.channel import BroChannel
from bro.datasources.base import DataSource
from llm.llm import LLM, LLMSpec
from llm.observer import BoringRenderer, NullObserver, Observer
from llm.tracker import EndReason, HTTPTracker, NullTracker, Tracker
from prompts import get_prompt, mode_fragment
from summon import SUMMONER_ENV

DEFAULT_LLM_SPEC: LLMSpec = llm.llms.chat_gpt.LLMSpec()


_TRAILS_DISABLED_ENV = 'TRAILS_DISABLED'


def _summoner_from_env() -> Optional[dict[str, Any]]:
  raw = os.environ.get(SUMMONER_ENV)
  if raw is None:
    return None
  summoner = json.loads(raw)
  if not isinstance(summoner, dict):
    raise ValueError(f'{SUMMONER_ENV} must be a JSON object')
  if set(summoner) == {'session'} and isinstance(summoner['session'], str):
    return summoner
  if (
    set(summoner) == {'target', 'trail_id'}
    and isinstance(summoner['target'], str)
    and isinstance(summoner['trail_id'], str)
  ):
    return summoner
  raise ValueError(f'{SUMMONER_ENV} has an invalid summoner shape')


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
  # resolve (`setup/bootstrap_trails.sh` writes `~/.ppp/trails.json`). a missing
  # secret is a setup error, not a fallback path — `NullTracker` is opt-in:
  # - kill switch: `TRAILS_DISABLED` set in the environment.
  # - tests: `conftest.py`'s `set_default_tracker_factory(NullTracker)`.
  # - one-shot exploration: `bro.run(..., tracker=NullTracker())`.
  try:
    config = credentials.get_json('trails')
  except credentials.SecretNotFound as e:
    raise RuntimeError(
      'trails: secret not found; run setup/bootstrap_trails.sh to enable '
      'recording, or pass tracker=NullTracker() to skip explicitly'
    ) from e
  return HTTPTracker(config['base_url'], config['token'])


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
  lines = ['## Data sources', '', 'You have access to the following read-only data sources:', '']
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


def _collect_skills(classes: list[type]) -> dict[str, Path]:
  # walk classes in base→derived order; for each class located in a real package
  # (__file__ is an __init__.py), collect *.md skills from <pkg>/skills/.
  # later writes overwrite earlier ones, so derived classes win on name collision.
  # framework classes (BaseBro in bro/bro.py) and ad-hoc test subclasses are
  # naturally skipped because their __file__ is not an __init__.py.
  # the concrete `Bro` (bro/bros/bro/__init__.py) sits at the base of every bro's
  # MRO, so dropping a skill into `bro/bros/bro/skills/` makes it the shared
  # skill mechanism — that skill becomes available to every bro by default,
  # parallel to how `system_prompt` and `mcp_servers` are inherited and
  # concatenated. derived bros can shadow it by declaring a skill of the same
  # name in their own `skills/` dir.
  found: dict[str, Path] = {}
  for cls in classes:
    module = sys.modules.get(cls.__module__)
    module_file = getattr(module, '__file__', None) if module is not None else None
    if module_file is None:
      continue
    file_path = Path(module_file).resolve()
    if file_path.name != '__init__.py':
      continue
    skills_dir = file_path.parent / 'skills'
    if not skills_dir.is_dir():
      continue
    for path in sorted(skills_dir.glob('*.md')):
      found[path.stem] = path
  return found


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
  # parse `---`-delimited YAML frontmatter. only flat one-line key:value pairs
  # are supported — the skill format we use, same as Claude Code's SKILL.md.
  # returns ({}, text) if no frontmatter; otherwise (kv-dict, body).
  if not text.startswith('---\n'):
    return ({}, text)
  end = text.find('\n---\n', 4)
  if end < 0:
    return ({}, text)
  fm: dict[str, str] = {}
  for line in text[4:end].splitlines():
    if ':' not in line:
      continue
    key, _, value = line.partition(':')
    fm[key.strip()] = value.strip()
  return (fm, text[end + 5 :])


def _load_skill(name: str, path: Path) -> tuple[dict[str, str], str]:
  # parse the skill file and enforce that its `name:` frontmatter (when
  # present) agrees with the filename stem — filename is canonical because
  # `_collect_skills` keys on it, so a disagreement is a silent footgun.
  fm, body = _parse_frontmatter(path.read_text())
  declared = fm.get('name')
  if declared is not None and declared != name:
    raise ValueError(
      f'skill {path}: frontmatter name={declared!r} disagrees with filename '
      f'stem {name!r}; filename is canonical'
    )
  return fm, body


def _first_sentence(text: str) -> str:
  # truncate at the first sentence boundary (`.`/`!`/`?` followed by whitespace)
  # so a paragraph-length frontmatter `description:` doesn't bloat the bro
  # system prompt. the first sentence is by convention the "when to invoke"
  # trigger guide — that's what the LLM needs to pick the right skill.
  text = text.strip()
  for i, c in enumerate(text):
    if c in '.!?' and i + 1 < len(text) and text[i + 1].isspace():
      return text[: i + 1]
  return text


def _render_skills(skills: list[tuple[str, str]]) -> str:
  lines = [
    '## Available skills',
    '',
    'You have the following named skills available. To invoke one, call the '
    "`bro::skill` tool with its name — the tool returns the skill's markdown "
    'body, which you then execute.',
    '',
    'A user message starting with `/<name>` is an invocation of the skill '
    'named `<name>`; the rest of the message is its arguments.',
    '',
  ]
  for name, description in skills:
    lines.append(f'- **{name}** — {_first_sentence(description)}')
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
  # over the broker channel where one exists, then terminate the session (cw
  # owns the mechanics). blocking ops, so off-loop; the finally keeps the kill
  # unconditional.
  from cw import terminate_session

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


_SKILL_DESCRIPTION = (
  'load a named skill and execute its body. pass `name` matching one of the '
  'skills listed under `## Available skills` in your system prompt. returns the '
  "skill's markdown body — follow its steps. fails if the name is unknown."
)


# the {{when #wire = mcp}} blocks render only into the MCP-served builds
# (persona and --bro claude sessions consume the toolset over streamable HTTP,
# where the harness bounds a silent tool call at about a minute — far under a
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
  'current HEAD (uncommitted changes never transfer). fails with the reason when the run raises, errors out, '
  'or dies. `detach: true` returns the request id right after the send instead of '
  'blocking — poll or collect it with `summon_check`.'
  '{{when #wire = mcp}} CAUTION: this tool is served over MCP, and the harness '
  'times a silent tool call out after about a minute while a summoned child '
  'typically runs for many — a blocking summon of a real task usually dies '
  'client-side with a transport timeout while the child keeps running, and the '
  'reply (with the request id) is lost with the call. for anything but a quick '
  'ask, pass `detach: true` and poll with summon_check. if a blocking call did '
  'time out, do NOT re-summon — the child keeps running'
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
  'times a silent tool call out after about a minute — a `wait: true` on a long '
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
  # launcher's CW_BRO (or none), not this bro's. the cw hub import stays
  # function-local so `import bro` stays cheap.
  def _banner() -> str:
    from cw import render_banner

    return render_banner(llm=True, bro=bro_name)

  return llm.mcp.FunctionTool(
    _banner, name='banner', description=_BANNER_DESCRIPTION, variables=variables
  )


def _summon_tool(variables: Variables) -> llm.mcp.Tool:
  # a fresh channel client per call, opened on the loop and closed in `finally`
  # so a cancelled tool call (the MCP client timed out or aborted) unblocks the
  # off-loop wait: the broxy sees the waiter go, and the terminal buffers for a
  # later summon_check instead of feeding an abandoned thread. the blocking wait
  # runs off-loop so an interactive surface stays responsive under a long summon.
  import summon as summon_client

  async def _summon(
    target: str,
    prompt: str,
    timeout: Optional[float] = None,
    into: Optional[str] = None,
    detach: bool = False,
  ) -> str:
    if detach:
      return await asyncio.to_thread(
        summon_client.summon_detached, target, prompt, timeout=timeout, into=into
      )
    client = summon_client.open_client()
    try:
      return await asyncio.to_thread(
        summon_client.summon_and_wait, target, prompt, timeout=timeout, into=into, client=client
      )
    finally:
      client.close()

  return llm.mcp.FunctionTool(
    _summon, name='summon', description=_SUMMON_DESCRIPTION, variables=variables
  )


def _summon_list_tool(variables: Variables) -> llm.mcp.Tool:
  import summon as summon_client

  async def _summon_list() -> dict[str, Any]:
    return await asyncio.to_thread(summon_client.list_summons)

  return llm.mcp.FunctionTool(
    _summon_list, name='summon_list', description=_SUMMON_LIST_DESCRIPTION, variables=variables
  )


def _summon_check_tool(variables: Variables) -> llm.mcp.Tool:
  # the wait path owns its client like _summon_tool, for the same cancellation
  # abort; the plain peek is answered locally and immediately, so it keeps the
  # per-call client inside the worker thread.
  import summon as summon_client

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
_SERVICE_TOOL_NAMES = ('banner', 'raise', 'skill', 'summon', 'summon_check', 'summon_list')


def _build_service_server(
  bro: 'BaseBro', *, include_raise: bool, harness: llm.mcp.Harness, wire: llm.mcp.Wire
) -> llm.mcp.MCPServer:
  # the roster is decided by the caller's surface and local process state:
  # `banner` is unconditional; `raise` only makes sense non-interactively (a
  # caller to abort to — interactive callers pass include_raise=False); `skill`
  # mounts only on the bro harness (the claude harness gets skills as rendered
  # SKILL.md files, cw/bro.py); `summon`/`summon_check` need a broker channel
  # and `summon_list` the session's summon-status file on top. the decided
  # roster then feeds the tools' rendering vocabulary: service tools are
  # harness features, the one tool surface that conditions on system facts, so
  # `#wire` is injected next to the `#tools` roster.
  has_skill = len(bro.skills) > 0 and harness == 'bro'
  has_broker = os.environ.get('BROKER_CHANNEL') is not None
  has_summon_list = False
  if has_broker:
    import summon as summon_module

    has_summon_list = os.environ.get(summon_module.STATUS_ENV) is not None

  mounted = ['banner']
  if include_raise:
    mounted.append('raise')
  if has_skill:
    mounted.append('skill')
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
  if has_skill:

    def skill(name: str) -> str:
      return bro.get_skill_body(name, harness=harness, wire=wire)

    tools.append(
      llm.mcp.FunctionTool(skill, name='skill', description=_SKILL_DESCRIPTION, variables=variables)
    )
  if has_broker:
    tools.append(_summon_tool(variables))
    tools.append(_summon_check_tool(variables))
    if has_summon_list:
      tools.append(_summon_list_tool(variables))
  assert [tool.name for tool in tools] == mounted
  server = llm.mcp.InProcessMCPServer('bro', tools)
  server.tool_universe = _SERVICE_TOOL_NAMES
  return server


def _unattended_claude_session() -> bool:
  # CW_MODE carries the session's user-involvement level, CW_RUNNER_PID makes
  # it terminatable (both exported by cw's in-place runner); `raise` needs an
  # unattended session and a runner to signal.
  return os.environ.get('CW_MODE') == 'unattended' and os.environ.get('CW_RUNNER_PID') is not None


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
  # credentials no component expresses — the escape hatch for a bro's environment
  # needs (ppp-dev → `github`; devoops → `aws`). MRO-walked and unioned like
  # `mcp_servers`, so a subclass declares only what it adds. folded into
  # `needed_secrets()`.
  extra_secrets: tuple[str, ...] = ()
  # bros this bro may summon — its static outgoing allow-list. root sessions get
  # it adjusted per session by --grant-summon/--revoke-summon; a summoned child
  # follows the bare seeds, so summons chain transitively through seeded bros
  # under the host's depth cap (see cw/summon.py). MRO-walked and unioned like
  # `extra_secrets`. ppp-dev seeds `devoops`; everyone else is empty (grows by
  # precedent).
  may_summon: tuple[str, ...] = ()
  # whether the bro does docker work (building/pushing images for deploys) and so
  # needs the host docker socket. an explicit capability, inherited normally. the
  # host grants `/var/run/docker.sock` to a `--bro`/`ask` container only when this
  # is set (claude code sessions get it unconditionally); see cw/secrets.py.
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
  # bro-native one; set in __init__, consumed by `cw ss --bro`
  claude_system_prompt: str

  _llm: Optional[LLM] = None

  def __init__(self, system_prompt: Optional[str] = None):
    mcp_entries: list[Entry[llm.mcp.MCPServerSpec | llm.mcp.Toolset[Any] | ModuleType]] = []
    data_source_entries: list[Entry[DataSource]] = []
    prompt_parts: list[str] = []
    extra_secret_names: list[str] = []
    may_summon_names: list[str] = []
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
    self._extra_secrets: tuple[str, ...] = tuple(extra_secret_names)
    self._may_summon: tuple[str, ...] = tuple(may_summon_names)
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
      for entry in llm.mcp.select(mcp_entries, harness='bro', creds=surface_creds)
    ]
    self._data_sources: list[DataSource] = llm.mcp.select(
      data_source_entries, harness='bro', creds=surface_creds
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
    # explicit `system_prompt=...` arg overrides MRO collection — escape hatch
    # for callers that need a dynamic prompt (e.g. PM injects current time).
    if system_prompt is not None:
      prompt_parts = [system_prompt] if len(system_prompt) > 0 else []
    # the bro's own persona: MRO-concatenated class system_prompt(s) under a
    # `# Persona: <name>` heading — the segment lands inside larger composed
    # prompts (below, and cw's append prompt), where headingless identity text
    # reads as a stray fragment. no shared / data-source / skills blocks here;
    # injected into dive-in Claude Code sessions (cw/system_prompt.py) so they
    # carry the bro's policies outside --bro mode.
    self.persona = (
      '\n\n'.join([f'# Persona: {self.name}', *prompt_parts]) if len(prompt_parts) > 0 else ''
    )
    shared = _load_shared_prompts()
    descriptions = self.skill_descriptions()

    def compose(wire: llm.mcp.Wire) -> str:
      parts = []
      if len(shared) > 0:
        parts.append(shared)
      if len(self.persona) > 0:
        parts.append(self.persona)
      # the namespaced-tool convention only matters once the bro actually has
      # tools or skills (which reference tools by their `ns::tool` name).
      if len(self._mcp_specs) > 0 or len(self._data_sources) > 0 or len(descriptions) > 0:
        parts.append(get_prompt('tool_names.md').strip())
      if len(self._data_sources) > 0:
        parts.append(_render_data_sources(self._data_sources))
      if len(descriptions) > 0:
        parts.append(_render_skills(descriptions))
      # both composed flavors serve the bro harness — the only per-flavor fact is
      # the wire scheme. cw-sessions never see these prompts;
      # they get the raw `persona` rendered by cw with its own surface facts.
      return llm.mcp.render_text(
        '\n\n'.join(parts), harness='bro', wire=wire, creds=credentials.known_names()
      )

    self.system_prompt = compose('bare')
    # the same prompt over mcp wire names — what a `cw ss --bro` session passes
    # as --system-prompt (cw/claude_argv.py).
    self.claude_system_prompt = compose('mcp')

  @property
  def agent(self) -> str:
    # the surface identity stamped on published usage (the usage file and, from
    # there, commit footers): bro runs are namespaced under bro// so the token
    # reads as a bro surface next to identities like 'Claude Code <version>'.
    return f'bro//{self.name}'

  @property
  def skills(self) -> dict[str, Path]:
    # walks <pkg>/skills/*.md along the MRO (base→derived); derived classes
    # override parents on name collision. computed on each access — the FS walk
    # is cheap and avoids stale state if a skill file is added at runtime.
    return _collect_skills(list(reversed(type(self).__mro__)))

  def get_skill_body(self, name: str, *, harness: llm.mcp.Harness, wire: llm.mcp.Wire) -> str:
    # return the markdown body of the named skill with frontmatter stripped and
    # template directives rendered for the consuming surface — each surface
    # sees only its own procedure. raises KeyError if the name is not one of
    # `self.skills`.
    skills = self.skills
    path = skills.get(name)
    if path is None:
      available = ', '.join(sorted(skills)) if len(skills) > 0 else '(none)'
      raise KeyError(f'no skill named {name!r}; available: {available}')
    _, body = _load_skill(name, path)
    return llm.mcp.render_text(
      body, harness=harness, wire=wire, creds=credentials.known_names()
    ).strip()

  def skill_descriptions(self) -> list[tuple[str, str]]:
    # return (name, description) pairs for each available skill, in the same
    # order as `self.skills`. description comes from the frontmatter; empty
    # string if missing.
    result: list[tuple[str, str]] = []
    for name, path in self.skills.items():
      fm, _ = _load_skill(name, path)
      result.append((name, fm.get('description', '')))
    return result

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
      for entry in llm.mcp.select(self._mcp_entries, harness=harness, creds=surface_creds)
    ]
    sources: list[DataSource] = llm.mcp.select(
      self._data_source_entries, harness=harness, creds=surface_creds
    )
    return specs, sources

  def needed_secrets(self, harness: llm.mcp.Harness = 'bro') -> tuple[str, ...]:
    # the bro's component credential manifest for a consuming harness: the union
    # of each declared MCP server's + data source's `needed_secrets`, over only
    # the components that hold on `harness` — a surface never hydrates a secret
    # of a component it doesn't mount — plus the bro's MRO-collected
    # `extra_secrets`. NOT the LLM key — that is added only by surfaces that run
    # the bro as an LLM process (ask / do-task); a claude-code session themed as
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
    # component set as `needed_secrets`, minus anything already in it — a secret
    # that is a hard requirement of any component is never downgraded to
    # best-effort. the host hydrates these via `build_scoped_store(optional=...)`,
    # so an absent one degrades the component instead of failing the launch.
    specs, sources = self._components_for(harness)
    names: set[str] = set()
    for spec in specs:
      names.update(_component_optional_secrets(spec))
    for ds in sources:
      names.update(_component_optional_secrets(ds))
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
    # each surface delivers it per its mode — run() raises, send() and the
    # assistant server reply.
    missing = self.missing_secrets()
    if len(missing) == 0:
      return None
    return f'{self.name} cannot start: missing credentials: {", ".join(missing)}'

  def _start(
    self,
    input: str,
    *,
    interactive: bool,
    observer: Optional[Observer],
    tracker: Optional[Tracker],
    entry_point: str,
    summoner: Optional[dict[str, Any]],
  ) -> tuple[LLM, list[dict], str]:
    # the shared start sequence of run() and send(): lock in observer/tracker —
    # caller-supplied ones win (CLIs use this to force --boring or to pass a
    # LocalFileTracker for dev capture), set on self before _create_llm so the
    # LLM construction path picks them up — then build the LLM, compose the
    # mode prompt, open the trail, and seed the message list.
    self._observer = observer if observer is not None else self._make_observer()
    self._tracker = tracker if tracker is not None else self._make_tracker()
    llm = self._create_llm(interactive=interactive)
    system_prompt = self._system_prompt_for(interactive=interactive)
    trail_id = self._tracker.start_trail(
      bro=self.name,
      llm_spec=self.llm_spec.dump(),
      system_prompt=system_prompt,
      parent=None,
      interactive=interactive,
      entry_point=entry_point,
      summoner=summoner,
    )
    self.trail_id = trail_id if len(trail_id) > 0 else None
    messages = [
      {'role': 'system', 'content': system_prompt},
      {'role': 'user', 'content': input},
    ]
    return llm, messages, trail_id

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
  ) -> str:
    refusal = self._start_refusal()
    if refusal is not None:
      raise BroRaised(refusal)
    llm, messages, trail_id = self._start(
      input,
      interactive=False,
      observer=observer,
      tracker=tracker,
      entry_point='cli:bro_run',
      summoner=_summoner_from_env(),
    )
    channel = self._make_channel()
    if channel is not None:
      channel.started(trail_id)
    end_reason: EndReason = 'terminal'
    result: Optional[str] = None
    try:
      result = await llm.send(messages, request_timeout=request_timeout)
      return result
    except BroRaised as raised:
      end_reason = 'raised'
      result = raised.reason
      raise
    except Exception as error:
      end_reason = 'error'
      result = str(error)
      raise
    finally:
      self._tracker.end_trail(end_reason)
      if channel is not None:
        channel.completed(result, end_reason)
        channel.close()

  async def send(
    self,
    message: str,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    request_timeout: Optional[float] = None,
    entry_point: str = 'send',
  ) -> str:
    if self._llm is None:
      refusal = self._start_refusal()
      if refusal is not None:
        # in-reply report; the LLM stays unbuilt, so a later send re-checks
        return refusal
      # the tracker is locked in on first send (the LLM is constructed once and
      # records one trail); later calls can't swap it. entry_point labels that
      # trail's header — surfaces name themselves (`call`, `process-inbox`).
      self._llm, messages, _ = self._start(
        message,
        interactive=True,
        observer=observer,
        tracker=tracker,
        entry_point=entry_point,
        summoner=None,
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

  @property
  def _service_server(self) -> llm.mcp.MCPServer:
    if self._service_server_cache is None:
      self._service_server_cache = _build_service_server(
        self, include_raise=True, harness='bro', wire='bare'
      )
    return self._service_server_cache

  def _live_mcp_servers(self) -> list[llm.mcp.MCPServer]:
    # specs materialize here, on first tool use — always in a serving process,
    # post-secrets — and are built once: a live server may hold real resources
    # (flow's shared System), so every run through this bro reuses the same set.
    if self._live_mcp is None:
      self._live_mcp = [spec.build() for spec in self._mcp_specs]
      self._live_mcp.extend(ds.as_mcp_server() for ds in self._data_sources)
    return self._live_mcp

  def _mcp_servers_for(self, *, interactive: bool) -> list[llm.mcp.MCPServer]:
    # the in-process LLM builds (always bare wire): the `raise` service tool
    # only makes sense in non-interactive runs — when no human is in the loop to
    # negotiate, the agent needs a way to abort. In interactive sessions the
    # agent describes any blocker in its reply instead. `skill`, however, is
    # needed in both modes, so interactive rebuilds the service server without
    # `raise` rather than dropping it wholesale.
    if interactive:
      return [
        *self._live_mcp_servers(),
        _build_service_server(self, include_raise=False, harness='bro', wire='bare'),
      ]
    return [*self._live_mcp_servers(), self._service_server]

  def claude_bro_mcp_servers(self) -> list[llm.mcp.MCPServer]:
    # the MCP servers a `cw ss --bro` Claude Code session mounts (through
    # mcp_server.py's `bro:<name>` surface): declared servers plus the `skill`
    # tool — without it the `--bro` surface exposes only the declared servers,
    # leaving the bro's skills unreachable there. skills serve the bro branch
    # (`--bare` strips claude's built-ins, so the session drives work through
    # the bro toolset, not Monitor/Bash) over mcp wire names. `raise` mounts
    # only for an unattended session.
    return [
      *self._live_mcp_servers(),
      _build_service_server(
        self, include_raise=_unattended_claude_session(), harness='bro', wire='mcp'
      ),
    ]

  def claude_persona_mcp_servers(self) -> list[llm.mcp.MCPServer]:
    # the MCP servers a cw-session themed as this bro mounts — claude's full
    # harness with the bro as its persona, served through mcp_server.py's
    # `persona:<name>` surface: the declared servers and data sources that hold
    # on the claude harness — an entry gated to the bro harness (the dev
    # toolset, the reference FileSources) never mounts, claude's built-in tools
    # cover it — plus the service server (`banner` and the summon pair; no
    # `skill`, a cw-session gets skills as slash commands; `raise` only for an
    # unattended session).
    specs, sources = self._components_for('claude')
    servers: list[llm.mcp.MCPServer] = [spec.build() for spec in specs]
    servers.extend(ds.as_mcp_server() for ds in sources)
    servers.append(
      _build_service_server(
        self, include_raise=_unattended_claude_session(), harness='claude', wire='mcp'
      )
    )
    return servers

  def _system_prompt_for(self, *, interactive: bool) -> str:
    # the run mode is pinned here, at run start, so the matching session-mode
    # fragment is injected rather than detected by the agent: interactive runs
    # (send(), the assistant server, `call`) are guided, non-interactive ones
    # (run()) unattended (the level files are documented in prompts/CLAUDE.md).
    fragment = mode_fragment(
      'guided' if interactive else 'unattended',
      harness='bro',
      wire='bare',
      creds=credentials.known_names(),
    )
    return f'{self.system_prompt}\n\n{fragment}'

  def _make_observer(self) -> Observer:
    return BoringRenderer(prefix=self.name)

  def _make_tracker(self) -> Tracker:
    return _default_tracker_factory()

  def _make_channel(self) -> Optional[BroChannel]:
    # None (no BROKER_CHANNEL in the environment) keeps the lifecycle emission inert
    return BroChannel.from_env()

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self.llm_spec.create_llm(
      mcp_servers=self._mcp_servers_for(interactive=interactive),
      observer=self._observer,
      tracker=self._tracker,
      # the LLM publishes cumulative usage under the bro's surface identity (the
      # usage file must be self-describing — an in-process run's CW_BRO is the
      # launcher's, not this bro's).
      agent=self.agent,
    )
