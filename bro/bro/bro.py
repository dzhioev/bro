import os
import sys
from abc import ABC
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Optional, Self

import llm.llms.chat_gpt
import llm.mcp
from base import credentials
from bro.channel import BroChannel
from bro.datasources.base import DataSource
from llm.llm import LLM, LLMSpec
from llm.observer import BoringRenderer, NullObserver, Observer
from llm.tracker import EndReason, HTTPTracker, NullTracker, Tracker
from prompts import get_prompt

DEFAULT_LLM_SPEC: LLMSpec = llm.llms.chat_gpt.LLMSpec()


_TRAILS_DISABLED_ENV = 'TRAILS_DISABLED'


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
    cfg = credentials.get_json('trails')
  except credentials.SecretNotFound as e:
    raise RuntimeError(
      'trails: secret not found; run setup/bootstrap_trails.sh to enable '
      'recording, or pass tracker=NullTracker() to skip explicitly'
    ) from e
  return HTTPTracker(cfg['base_url'], cfg['token'])


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


# bro-facing tool-name rule, appended to a bro's system prompt when it has any
# namespaced tools or skills. this is the bro-native half of the per-harness
# rule — a bro LLM run resolves `::` to `ns__tool` (no `mcp__`). the Claude-Code
# half is prompts/tool_names.md (`ns::tool` → `mcp__ns__tool`): non-bro `cw ss`
# sessions get it via cw/system_prompt.py, and `cw ss --bro` sessions via
# `claude_system_prompt`, which composes it in place of this block.
_TOOL_NAMES_BLOCK = (
  '## Tool names\n'
  '\n'
  "Your tools are namespaced. `namespace::tool` is a tool's canonical name; it "
  'can appear anywhere — a skill, a doc, a tool description, a user message. In '
  'your tool list it is `namespace__tool` (replace `::` with `__`); call that '
  'wire name directly.'
)


def _load_claude_tool_names() -> str:
  # a `cw ss --bro` session mounts each namespace as an MCP server, so its wire
  # names carry the `mcp__` prefix — the bro-native block above would teach the
  # wrong form there.
  return get_prompt('tool_names.md').strip()


def _render_data_sources(sources: list[DataSource]) -> str:
  lines = ['## Data sources', '', 'You have access to the following read-only data sources:', '']
  for ds in sources:
    # resolve any `has_cred` blocks in the summary against live credential
    # availability (e.g. a source advertising query-focused fetch only when its
    # LLM key is present), validated against the source's declared secrets.
    declared = set(ds.needed_secrets) | set(ds.optional_secrets)
    summary = llm.mcp.render_has_cred(ds.summary, credentials.available, declared)
    lines.append(f'- **{ds.name}** — {summary}')
  lines.append('')
  lines.append(
    "Each source's tools live in its own `<name>-source` namespace — e.g. "
    '`wikipedia-source::search`, `current-time-source::get_time`. See the tool '
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
  ]
  for name, desc in skills:
    lines.append(f'- **{name}** — {_first_sentence(desc)}')
  return '\n'.join(lines)


class BroRaised(llm.mcp.ToolControlSignal):
  """raised by the `raise` service tool to abort a Bro run."""

  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


def _raise(reason: str) -> str:
  raise BroRaised(reason)


_RAISE_DESCRIPTION = (
  'abort the run because the request cannot be fulfilled. Call this when '
  'required credentials or API keys are missing, no appropriate tool or data '
  'source is available, the request contains contradictory constraints, the '
  'input is unclear or cannot be understood (gibberish, ambiguous, or missing '
  'the context needed to act), or any other blocker prevents completing the '
  'task. Do NOT reply with a clarifying question — there is no follow-up turn; '
  'raise instead. Pass a clear, specific reason — it surfaces to the caller as '
  'the failure cause.'
)


_SKILL_DESCRIPTION = (
  'load a named skill and execute its body. pass `name` matching one of the '
  'skills listed under `## Available skills` in your system prompt. returns the '
  "skill's markdown body — follow its steps. fails if the name is unknown."
)


def _build_service_server(bro: 'BaseBro', *, include_raise: bool) -> llm.mcp.MCPServer:
  # `raise` only makes sense non-interactively (a caller to abort to); `skill` is
  # needed in both modes. interactive callers pass include_raise=False.
  tools: list[llm.mcp.Tool] = []
  if include_raise:
    tools.append(llm.mcp.FunctionTool(_raise, name='raise', description=_RAISE_DESCRIPTION))
  if len(bro.skills) > 0:

    def skill(name: str) -> str:
      return bro.get_skill_body(name)

    tools.append(llm.mcp.FunctionTool(skill, name='skill', description=_SKILL_DESCRIPTION))
  return llm.mcp.InProcessMCPServer('bro', tools)


_NON_INTERACTIVE_NOTE = (
  'You are running in non-interactive mode — this is a one-shot invocation '
  'with no follow-up turn. If you cannot fulfill the request (missing '
  'credentials, no appropriate tool or data source, contradictory '
  'constraints, the input is unclear or cannot be understood, or any other '
  'blocker), call the `raise` tool with a clear reason instead of producing '
  'a partial or speculative answer or asking a clarifying question — there '
  'is no one to answer it.'
)

_INTERACTIVE_NOTE = (
  'You are running in interactive mode — there is a human on the other end '
  'of the conversation and your reply will be followed by further turns. '
  'When the request is unclear, ambiguous, or missing context, ask a '
  'clarifying question instead of guessing. There is no `raise` tool here; '
  'just describe any blocker in your reply.'
)


McpServerEntry = llm.mcp.MCPServer | Callable[[], llm.mcp.MCPServer]


def _materialize(entry: McpServerEntry) -> llm.mcp.MCPServer:
  return entry if isinstance(entry, llm.mcp.MCPServer) else entry()


def _component_needed_secrets(obj: llm.mcp.MCPServer | DataSource) -> set[str]:
  # a component declares its credentials as a class attribute
  # (`needed_secrets = (...)`) or a computed instance property (flow's server
  # derives it from the tools it holds); both surface through a plain instance
  # read. no real component extends a non-empty base's declaration, so an MRO
  # union would be identical.
  return set(obj.needed_secrets)


def _component_optional_secrets(obj: llm.mcp.MCPServer | DataSource) -> set[str]:
  # mirror of `_component_needed_secrets` for the best-effort tier (`optional_secrets`).
  return set(obj.optional_secrets)


class BaseBro(ABC):
  name: str
  description: str
  llm_spec: LLMSpec = DEFAULT_LLM_SPEC
  data_sources: ClassVar[list[DataSource]] = []
  mcp_servers: ClassVar[list[McpServerEntry]] = []
  # credentials no component expresses — the escape hatch for a bro's environment
  # needs (ppp-dev → `github`; devoops → `aws`). MRO-walked and unioned like
  # `mcp_servers`, so a subclass declares only what it adds. folded into
  # `needed_secrets()`.
  extra_secrets: tuple[str, ...] = ()
  # whether the bro does docker work (building/pushing images for deploys) and so
  # needs the host docker socket. an explicit capability, inherited normally. the
  # host grants `/var/run/docker.sock` to a `--bro`/`ask` container only when this
  # is set (claude code sessions get it unconditionally); see cw/secrets.py.
  needs_docker: bool = False
  # subclasses declare their own `system_prompt = "..."` as a class attribute;
  # `__init__` walks the MRO from base to derived and concatenates each class's
  # own contribution. so `PPPDev(Dev)` only needs to declare what PPPDev adds —
  # Dev's prompt (and Bro's) are picked up automatically. same for
  # `mcp_servers`. inherit directly from BaseBro to opt out of the concrete
  # `Bro`'s shared defaults.
  system_prompt: str = ''
  # the bro's own class prompts (MRO-concatenated); set in __init__
  persona: str
  # `system_prompt` with the Claude-Code tool-name rule in place of the
  # bro-native one; set in __init__, consumed by `cw ss --bro`
  claude_system_prompt: str

  _llm: Optional[LLM] = None

  def __init__(self, system_prompt: Optional[str] = None):
    mcp_entries: list[McpServerEntry] = []
    prompt_parts: list[str] = []
    extra_secret_names: list[str] = []
    for cls in reversed(type(self).__mro__):
      raw_mcp = cls.__dict__.get('mcp_servers')
      if raw_mcp is not None:
        mcp_entries.extend(raw_mcp)
      raw_prompt = cls.__dict__.get('system_prompt')
      if isinstance(raw_prompt, str) and len(raw_prompt) > 0:
        prompt_parts.append(raw_prompt)
      raw_extra = cls.__dict__.get('extra_secrets')
      if raw_extra is not None:
        extra_secret_names.extend(raw_extra)
    self._extra_secrets: tuple[str, ...] = tuple(extra_secret_names)
    self._declared_mcp: list[llm.mcp.MCPServer] = [_materialize(e) for e in mcp_entries]
    self._mcp_servers: list[llm.mcp.MCPServer] = list(self._declared_mcp)
    for ds in self.data_sources:
      self._mcp_servers.append(ds.as_mcp_server())
    self._service_server: llm.mcp.MCPServer = _build_service_server(self, include_raise=True)
    self._llm = None
    # default to no-op; BaseBro.run() swaps in a real observer per invocation so the
    # LLM construction path picks it up via self._observer.
    self._observer: Observer = NullObserver()
    # sibling of _observer — the tracker records the run for offline analysis
    # rather than rendering it to stderr. swapped in BaseBro.run() / .send() the
    # same way _observer is.
    self._tracker: Tracker = NullTracker()
    # explicit `system_prompt=...` arg overrides MRO collection — escape hatch
    # for callers that need a dynamic prompt (e.g. PM injects current time).
    if system_prompt is not None:
      prompt_parts = [system_prompt] if len(system_prompt) > 0 else []
    # the bro's own persona: MRO-concatenated class system_prompt(s) without the
    # shared / data-source / skills blocks. injected into dive-in Claude Code
    # sessions (cw/system_prompt.py) so they carry the bro's policies outside --bro mode.
    self.persona = '\n\n'.join(prompt_parts)
    shared = _load_shared_prompts()
    descriptions = self.skill_descriptions()

    def compose(tool_names_block: str) -> str:
      parts = []
      if len(shared) > 0:
        parts.append(shared)
      parts.extend(prompt_parts)
      # the namespaced-tool convention only matters once the bro actually has
      # tools or skills (which reference tools by their `ns::tool` name).
      if len(self._mcp_servers) > 0 or len(descriptions) > 0:
        parts.append(tool_names_block)
      if len(self.data_sources) > 0:
        parts.append(_render_data_sources(self.data_sources))
      if len(descriptions) > 0:
        parts.append(_render_skills(descriptions))
      return '\n\n'.join(parts)

    self.system_prompt = compose(_TOOL_NAMES_BLOCK)
    # the same prompt with the Claude-Code tool-name rule — what a `cw ss --bro`
    # session passes as --system-prompt (cw/bro.py:_bro_launch).
    self.claude_system_prompt = compose(_load_claude_tool_names())

  @property
  def skills(self) -> dict[str, Path]:
    # walks <pkg>/skills/*.md along the MRO (base→derived); derived classes
    # override parents on name collision. computed on each access — the FS walk
    # is cheap and avoids stale state if a skill file is added at runtime.
    return _collect_skills(list(reversed(type(self).__mro__)))

  def get_skill_body(self, name: str) -> str:
    # return the markdown body of the named skill with frontmatter stripped.
    # raises KeyError if the name is not one of `self.skills`.
    skills = self.skills
    path = skills.get(name)
    if path is None:
      available = ', '.join(sorted(skills)) if len(skills) > 0 else '(none)'
      raise KeyError(f'no skill named {name!r}; available: {available}')
    _, body = _load_skill(name, path)
    return body.strip()

  def skill_descriptions(self) -> list[tuple[str, str]]:
    # return (name, description) pairs for each available skill, in the same
    # order as `self.skills`. description comes from the frontmatter; empty
    # string if missing.
    result: list[tuple[str, str]] = []
    for name, path in self.skills.items():
      fm, _ = _load_skill(name, path)
      result.append((name, fm.get('description', '')))
    return result

  def needed_secrets(self) -> tuple[str, ...]:
    # the bro's component credential manifest: the union of each declared MCP
    # server's + data source's `needed_secrets` (each walked along its own MRO)
    # and the bro's MRO-collected `extra_secrets`. NOT the LLM key — that is added
    # only by surfaces that run the bro as an LLM process (ask / do-task); a
    # claude-code session themed as the bro uses its own auth, not the bro's spec.
    # the host hydrates the per-surface set into a scoped store; a secret used but
    # not declared surfaces as SecretNotFound — an under-declaration to fix.
    names: set[str] = set()
    for server in self._declared_mcp:
      names.update(_component_needed_secrets(server))
    for ds in self.data_sources:
      names.update(_component_needed_secrets(ds))
    names.update(self._extra_secrets)
    return tuple(sorted(names))

  def optional_secrets(self) -> tuple[str, ...]:
    # the bro's best-effort credential tier: the union of each declared MCP
    # server's + data source's `optional_secrets`, minus anything already in
    # `needed_secrets()` — a secret that is a hard requirement of any component
    # is never downgraded to best-effort. the host hydrates these via
    # `build_scoped_store(optional=...)`, so an absent one degrades the
    # component instead of failing the launch.
    names: set[str] = set()
    for server in self._declared_mcp:
      names.update(_component_optional_secrets(server))
    for ds in self.data_sources:
      names.update(_component_optional_secrets(ds))
    return tuple(sorted(names - set(self.needed_secrets())))

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
    # caller-supplied observer / tracker win (CLIs use this to force --boring
    # or to pass a LocalFileTracker for dev capture); otherwise _make_observer()
    # / _make_tracker() pick the defaults. set on self before _create_llm so the
    # LLM construction path can pick them up.
    self._observer = observer if observer is not None else self._make_observer()
    self._tracker = tracker if tracker is not None else self._make_tracker()
    llm = self._create_llm(interactive=False)
    system_prompt = self._system_prompt_for(interactive=False)
    channel = self._make_channel()
    trail_id = self._tracker.start_trail(
      bro=self.name,
      llm_spec=self.llm_spec.dump(),
      system_prompt=system_prompt,
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    if channel is not None:
      channel.started(trail_id)
    messages = [
      {'role': 'system', 'content': system_prompt},
      {'role': 'user', 'content': input},
    ]
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
  ) -> str:
    if self._llm is None:
      # observer / tracker are locked in on first send (the LLM is constructed
      # once and holds onto whatever was set on self at that moment); later
      # calls can't swap them. Mirrors BaseBro.run().
      self._observer = observer if observer is not None else self._make_observer()
      self._tracker = tracker if tracker is not None else self._make_tracker()
      self._llm = self._create_llm(interactive=True)
      system_prompt = self._system_prompt_for(interactive=True)
      self._tracker.start_trail(
        bro=self.name,
        llm_spec=self.llm_spec.dump(),
        system_prompt=system_prompt,
        parent=None,
        interactive=True,
        entry_point='http',
      )
      messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': message},
      ]
    else:
      messages = [{'role': 'user', 'content': message}]
    return await self._llm.send(messages, request_timeout=request_timeout)

  def _mcp_servers_for(self, *, interactive: bool) -> list[llm.mcp.MCPServer]:
    # the `raise` service tool only makes sense in non-interactive runs — when no
    # human is in the loop to negotiate, the agent needs a way to abort. In
    # interactive sessions the agent describes any blocker in its reply instead.
    # `skill`, however, is needed in both modes, so interactive rebuilds the
    # service server without `raise` rather than dropping it wholesale.
    if interactive:
      return [*self._mcp_servers, _build_service_server(self, include_raise=False)]
    return [*self._mcp_servers, self._service_server]

  def claude_bro_mcp_servers(self) -> list[llm.mcp.MCPServer]:
    # the MCP servers a `cw ss --bro` Claude Code session mounts (through
    # mcp_server.py's `bro:<name>` surface): the same interactive set `send()`
    # gets — declared servers plus the `skill` tool, no `raise` (a human drives
    # the session). without it the `--bro` surface exposes only the declared
    # servers, leaving the bro's skills unreachable there.
    return self._mcp_servers_for(interactive=True)

  def _system_prompt_for(self, *, interactive: bool) -> str:
    note = _INTERACTIVE_NOTE if interactive else _NON_INTERACTIVE_NOTE
    return f'{self.system_prompt}\n\n{note}'

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
    )
