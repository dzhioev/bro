import sys
from abc import ABC
from pathlib import Path
from typing import Callable, Self

import llm.llms.chat_gpt
import llm.mcp
from bro.datasources.base import DataSource
from llm.llm import LLM, LLMSpec
from llm.observer import BoringRenderer, NullObserver, Observer

DEFAULT_LLM_SPEC: LLMSpec = llm.llms.chat_gpt.LLMSpec()


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
    lines.append(f'- **{ds.name}** — {ds.summary}')
  lines.append('')
  lines.append(
    "Each source's tools are prefixed with `<name>-`; see the tool listings for details."
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
    "`skill` tool with its name — the tool returns the skill's markdown body, "
    'which you then execute.',
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


def _build_service_server(bro: 'BaseBro') -> llm.mcp.MCPServer:
  tools: list[llm.mcp.Tool] = [
    llm.mcp.FunctionTool(_raise, name='raise', description=_RAISE_DESCRIPTION),
  ]
  if len(bro.skills) > 0:

    def skill(name: str) -> str:
      return bro.get_skill_body(name)

    tools.append(llm.mcp.FunctionTool(skill, name='skill', description=_SKILL_DESCRIPTION))
  return llm.mcp.InProcessMCPServer(tools)


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


class BaseBro(ABC):
  name: str
  description: str
  llm_spec: LLMSpec = DEFAULT_LLM_SPEC
  data_sources: list[DataSource] = []
  mcp_servers: list[McpServerEntry] = []
  # subclasses declare their own `system_prompt = "..."` as a class attribute;
  # `__init__` walks the MRO from base to derived and concatenates each class's
  # own contribution. so `PPPDev(Dev)` only needs to declare what PPPDev adds —
  # Dev's prompt (and Bro's) are picked up automatically. same for
  # `mcp_servers`. inherit directly from BaseBro to opt out of the concrete
  # `Bro`'s shared defaults.
  system_prompt: str = ''

  _llm: LLM | None = None

  def __init__(self, system_prompt: str | None = None):
    mcp_entries: list[McpServerEntry] = []
    prompt_parts: list[str] = []
    for cls in reversed(type(self).__mro__):
      raw_mcp = cls.__dict__.get('mcp_servers')
      if raw_mcp is not None:
        mcp_entries.extend(raw_mcp)
      raw_prompt = cls.__dict__.get('system_prompt')
      if isinstance(raw_prompt, str) and len(raw_prompt) > 0:
        prompt_parts.append(raw_prompt)
    self._declared_mcp: list[llm.mcp.MCPServer] = [_materialize(e) for e in mcp_entries]
    self._mcp_servers: list[llm.mcp.MCPServer] = list(self._declared_mcp)
    for ds in self.data_sources:
      self._mcp_servers.append(ds.as_mcp_server())
    self._service_server: llm.mcp.MCPServer = _build_service_server(self)
    self._llm = None
    # default to no-op; BaseBro.run() swaps in a real observer per invocation so the
    # LLM construction path picks it up via self._observer.
    self._observer: Observer = NullObserver()
    # explicit `system_prompt=...` arg overrides MRO collection — escape hatch
    # for callers that need a dynamic prompt (e.g. PM injects current time).
    if system_prompt is not None:
      prompt_parts = [system_prompt] if len(system_prompt) > 0 else []
    shared = _load_shared_prompts()
    parts = []
    if len(shared) > 0:
      parts.append(shared)
    parts.extend(prompt_parts)
    if len(self.data_sources) > 0:
      parts.append(_render_data_sources(self.data_sources))
    descriptions = self.skill_descriptions()
    if len(descriptions) > 0:
      parts.append(_render_skills(descriptions))
    self.system_prompt = '\n\n'.join(parts)

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

  async def run(self, input: str, observer: Observer | None = None) -> str:
    # caller-supplied observer wins (CLIs use this to force --boring); otherwise
    # _make_observer() picks the default. set on self before _create_llm so the
    # LLM construction path can pick it up.
    self._observer = observer if observer is not None else self._make_observer()
    llm = self._create_llm(interactive=False)
    messages = [
      {'role': 'system', 'content': self._system_prompt_for(interactive=False)},
      {'role': 'user', 'content': input},
    ]
    return await llm.send(messages)

  async def send(self, message: str, observer: Observer | None = None) -> str:
    if self._llm is None:
      # observer is locked in on first send (the LLM is constructed once and
      # holds onto whatever observer was set on self at that moment); later
      # calls can't swap it. Mirrors BaseBro.run().
      self._observer = observer if observer is not None else self._make_observer()
      self._llm = self._create_llm(interactive=True)
      messages = [
        {'role': 'system', 'content': self._system_prompt_for(interactive=True)},
        {'role': 'user', 'content': message},
      ]
    else:
      messages = [{'role': 'user', 'content': message}]
    return await self._llm.send(messages)

  def _mcp_servers_for(self, *, interactive: bool) -> list[llm.mcp.MCPServer]:
    # the `raise` service tool only makes sense in non-interactive runs — when no
    # human is in the loop to negotiate, the agent needs a way to abort. In
    # interactive sessions the agent can just describe any blocker in its reply.
    if interactive:
      return list(self._mcp_servers)
    return [*self._mcp_servers, self._service_server]

  def _system_prompt_for(self, *, interactive: bool) -> str:
    note = _INTERACTIVE_NOTE if interactive else _NON_INTERACTIVE_NOTE
    return f'{self.system_prompt}\n\n{note}'

  def _make_observer(self) -> Observer:
    return BoringRenderer(prefix=self.name)

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self.llm_spec.create_llm(
      mcp_servers=self._mcp_servers_for(interactive=interactive),
      observer=self._observer,
    )
