import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Self

from pydantic import BaseModel, ConfigDict, model_validator

import bro.llm.mcp as llm_mcp
import bro.mcp as mcp
from bro.base import credentials
from bro.base.condition import Entry, Iff, StringVariable, Variables, When, select
from bro.procedures import parse_frontmatter

if TYPE_CHECKING:
  from bro.bro import BaseBro

NAMESPACE = 'spell'
CAST_SECRET = 'mu'
_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
_CHOICE_PATTERN = re.compile(r'[A-Za-z0-9_.-]+')
_CHOICE_RESERVED_NAMES = frozenset(
  {'harness', 'creds', 'may_summon', 'talk', 'hold', 'features', 'true', 'false'}
)


@dataclass(frozen=True)
class Parameter:
  name: str
  description: str
  required: bool
  choices: tuple[str, ...] = ()
  default: str | None = None

  def schema(self) -> dict[str, Any]:
    schema: dict[str, Any] = {'type': 'string', 'description': self.description}
    if self.choices:
      schema['enum'] = list(self.choices)
    if self.default is not None:
      schema['default'] = self.default
    return schema

  def bind(self, arguments: Mapping[str, Any]) -> str | None:
    if self.name not in arguments:
      if self.required:
        raise ValueError(f'missing required spell argument {self.name!r}')
      return self.default
    value = arguments[self.name]
    if not isinstance(value, str):
      raise ValueError(f'spell argument {self.name!r} must be a string')
    if self.choices and value not in self.choices:
      raise ValueError(
        f'spell argument {self.name!r}: {value!r} is outside choices {self.choices!r}'
      )
    return value


@dataclass(frozen=True)
class Spell:
  name: str
  path: Path
  description: str
  parameters: tuple[Parameter, ...]
  body: str


@dataclass(frozen=True)
class SpellCall:
  spell: Spell
  arguments: dict[str, str]
  defaulted: frozenset[str]


class _ChoiceDeclaration(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  description: str
  choices: list[str]
  default: str | None = None


class _ArgumentPair(BaseModel):
  model_config = ConfigDict(extra='forbid')

  name: str
  value: str


class _Interpretation(BaseModel):
  model_config = ConfigDict(extra='forbid')

  spell: Optional[str]
  arguments: Optional[list[_ArgumentPair]]
  error: Optional[str]

  @model_validator(mode='after')
  def validate_result_shape(self) -> Self:
    if self.error is not None:
      if self.spell is not None or self.arguments is not None:
        raise ValueError('an error interpretation cannot also contain an executable call')
    elif self.spell is None:
      raise ValueError('an executable interpretation requires a spell')
    elif self.arguments is None:
      self.arguments = []
    return self


@dataclass(frozen=True)
class SpellFile:
  """a spell's markdown file and the spells it runs in its own session.
  the file's stem is the spell's name. `requires` names spells in the
  declaration grammar of a bro's `spells` (plain names, `when`, `iff`): a bro
  offering this spell must offer every requirement that resolves."""

  path: Path
  requires: tuple[Entry[str], ...] = ()

  def __post_init__(self) -> None:
    if self.path.suffix != '.md':
      raise ValueError(f'spell file {self.path} is not a markdown file')
    if not self.path.is_file():
      raise ValueError(f'spell file {self.path} does not exist')
    _validate_requires(self)

  @property
  def name(self) -> str:
    return self.path.stem


@dataclass(frozen=True)
class SpellDeclaration:
  """one class's `spells` declaration: the class that wrote it and its entries."""

  owner: str
  entries: tuple[Entry[SpellFile], ...]


def _validate_requires(spell: SpellFile) -> None:
  for item in _items(spell.requires):
    if not isinstance(item, str):
      raise TypeError(f'spell {spell.name!r} requires {item!r}, not a spell name')


def _items(entries: Iterable[Entry[Any]]) -> list[Any]:
  """every value a declarative list can resolve to, whatever its conditions."""
  items: list[Any] = []
  for entry in entries:
    if isinstance(entry, When):
      items.append(entry.item)
    elif isinstance(entry, Iff):
      items.extend(item for _, item in entry.branches)
      if entry.otherwise is not None:
        items.append(entry.otherwise[0])
    else:
      items.append(entry)
  return items


def declared_files(owner: str, entries: Iterable[Entry[SpellFile]]) -> list[SpellFile]:
  """every spell file a class's `spells` declaration can resolve to, failing
  for an entry that is not a `SpellFile`."""
  files: list[SpellFile] = []
  for item in _items(entries):
    if not isinstance(item, SpellFile):
      raise TypeError(f'{owner}.spells: {item!r} is not a SpellFile')
    files.append(item)
  return files


def select_spells(
  declarations: Iterable[SpellDeclaration], variables: Variables
) -> dict[str, SpellFile]:
  """the spells the declarations offer under `variables`, by name:
  `declarations` run base class first and a derived class's file replaces a
  parent's of the same name. a spell whose resolved requirement is not
  offered fails."""
  offered: dict[str, SpellFile] = {}
  for declaration in declarations:
    declared: dict[str, SpellFile] = {}
    for spell in select(declaration.entries, variables):
      if spell.name in declared:
        raise ValueError(
          f'{declaration.owner}.spells: {spell.path.name!r} repeats spell {spell.name!r}'
        )
      declared[spell.name] = spell
    offered.update(declared)
  for spell in offered.values():
    for required in select(spell.requires, variables):
      if required not in offered:
        raise ValueError(f'spell {spell.name!r} requires spell {required!r}, which is not offered')
  return offered


def _validate_name(kind: str, name: str, path: Path) -> None:
  if _NAME_PATTERN.fullmatch(name) is None:
    raise ValueError(
      f'spell {path}: {kind} {name!r} must contain only ASCII letters, digits, "_", or "-"'
    )


def _parse_parameters(raw: str, path: Path) -> tuple[Parameter, ...]:
  try:
    value = json.loads(raw)
  except json.JSONDecodeError as error:
    raise ValueError(f'spell {path}: parameters must be a one-line JSON object') from error
  if not isinstance(value, dict):
    raise ValueError(f'spell {path}: parameters must be a JSON object')

  parameters: list[Parameter] = []
  names: set[str] = set()
  for declared_name, declaration in value.items():
    required = not declared_name.endswith('?')
    name = declared_name if required else declared_name[:-1]
    _validate_name('parameter name', name, path)
    if name in names:
      raise ValueError(f'spell {path}: duplicate parameter name {name!r}')
    names.add(name)
    if isinstance(declaration, str):
      parameters.append(Parameter(name, declaration, required))
      continue
    if not isinstance(declaration, dict):
      raise ValueError(f'spell {path}: parameter descriptions must be strings or choice objects')
    if name in _CHOICE_RESERVED_NAMES:
      raise ValueError(f'spell {path}: choice parameter {name!r} conflicts with a rendering fact')
    choice = _ChoiceDeclaration.model_validate(declaration)
    if not choice.choices or any(
      _CHOICE_PATTERN.fullmatch(item) is None for item in choice.choices
    ):
      raise ValueError(f'spell {path}: choices must be nonempty literals matching [A-Za-z0-9_.-]+')
    if len(set(choice.choices)) != len(choice.choices):
      raise ValueError(f'spell {path}: repeated choice for parameter {name!r}')
    has_default = 'default' in choice.model_fields_set
    if required and has_default:
      raise ValueError(f'spell {path}: required choice {name!r} must not have a default')
    if not required and not has_default:
      raise ValueError(f'spell {path}: optional choice {name!r} requires a default')
    if has_default and choice.default not in choice.choices:
      raise ValueError(f'spell {path}: default for {name!r} is outside its choices')
    parameters.append(
      Parameter(name, choice.description, required, tuple(choice.choices), choice.default)
    )
  return tuple(parameters)


def load_spell(name: str, path: Path) -> Spell:
  _validate_name('filename stem', name, path)
  frontmatter, body = parse_frontmatter(path.read_text(), f'spell {path}')
  declared_name = frontmatter.get('name')
  if declared_name is not None and declared_name != name:
    raise ValueError(
      f'spell {path}: frontmatter name={declared_name!r} disagrees with filename '
      f'stem {name!r}; filename is canonical'
    )
  description = frontmatter.get('description')
  if description is None:
    raise ValueError(f'spell {path}: frontmatter declares no description')
  raw_parameters = frontmatter.get('parameters')
  parameters = () if raw_parameters is None else _parse_parameters(raw_parameters, path)
  return Spell(name, path, description, parameters, body)


def cast_available() -> bool:
  return credentials.available(CAST_SECRET)


def _canonical_name(spell: Spell) -> str:
  return f'spell::{spell.name}'


def _cast_roster(spells: list[Spell]) -> list[dict[str, Any]]:
  return [
    {
      'spell': _canonical_name(spell),
      'description': spell.description,
      'parameters': {
        'type': 'object',
        'properties': {parameter.name: parameter.schema() for parameter in spell.parameters},
        'required': [parameter.name for parameter in spell.parameters if parameter.required],
        'additionalProperties': False,
      },
    }
    for spell in spells
  ]


def _render_spell_call(
  bro: 'BaseBro',
  call: SpellCall,
  *,
  harness: mcp.HarnessLike,
  hold: str,
) -> str:
  choices = {
    parameter.name: call.arguments[parameter.name]
    for parameter in call.spell.parameters
    if parameter.choices
  }
  body = bro.get_spell_body(call.spell.name, harness=harness, hold=hold, choices=choices)
  return call_text(call.spell, body, call.arguments, defaulted=call.defaulted)


def call_text(
  spell: Spell,
  body: str,
  arguments: Mapping[str, Any],
  *,
  defaulted: frozenset[str] = frozenset(),
) -> str:
  """the instructions a call of `spell` returns: its rendered `body`, then a
  `# Arguments` section listing passed values and marked defaults."""
  passed = [
    f'{parameter.name}: {arguments[parameter.name]}'
    + (' (default)' if parameter.name in defaulted else '')
    for parameter in spell.parameters
    if parameter.name in arguments
  ]
  if len(passed) == 0:
    return body
  return f'{body}\n\n# Arguments\n\n' + '\n'.join(passed)


def _bind_call(spell: Spell, arguments: Mapping[str, Any]) -> SpellCall:
  unknown = set(arguments) - {parameter.name for parameter in spell.parameters}
  if unknown:
    raise ValueError(f'unknown arguments for spell {spell.name!r}: {sorted(unknown)}')
  bound: dict[str, str] = {}
  defaulted: set[str] = set()
  for parameter in spell.parameters:
    value = parameter.bind(arguments)
    if value is not None:
      bound[parameter.name] = value
      if parameter.name not in arguments:
        defaulted.add(parameter.name)
  return SpellCall(spell, bound, frozenset(defaulted))


def choice_variables(spell: Spell, choices: Mapping[str, str]) -> dict[str, StringVariable]:
  parameters = {parameter.name: parameter for parameter in spell.parameters if parameter.choices}
  unknown = set(choices) - parameters.keys()
  if unknown:
    raise ValueError(f'unknown choices for spell {spell.name!r}: {sorted(unknown)}')
  variables: dict[str, StringVariable] = {}
  for parameter in parameters.values():
    value = parameter.bind(choices)
    assert value is not None
    variables[parameter.name] = StringVariable(value, domain=frozenset(parameter.choices))
  return variables


def _validated_call(interpretation: _Interpretation, spells: list[Spell]) -> SpellCall:
  assert interpretation.spell is not None
  assert interpretation.arguments is not None
  spells_by_name = {_canonical_name(spell): spell for spell in spells}
  spell = spells_by_name.get(interpretation.spell)
  if spell is None:
    raise ValueError(f'spell interpreter selected unknown spell {interpretation.spell!r}')

  arguments: dict[str, str] = {}
  for pair in interpretation.arguments:
    if pair.name in arguments:
      raise ValueError(f'spell interpreter returned duplicate argument {pair.name!r}')
    arguments[pair.name] = pair.value
  return _bind_call(spell, arguments)


async def interpret(command: str, spells: list[Spell]) -> SpellCall | str:
  """the bound call a free-form command resolves to among `spells`, or the
  interpreter's reason it resolves to none."""
  from bro.llm.mu import mu
  from bro.llm.wire import Text
  from bro.prompts import get_prompt

  request = {'command': command, 'spells': _cast_roster(spells)}
  interpretation = await mu.aio(
    get_prompt('spell_dispatch.prompt'),
    _Interpretation,
    Text(json.dumps(request, indent=4)),
    role='dispatch',
  )
  if interpretation.error is not None:
    if len(interpretation.error.strip()) == 0:
      raise ValueError('spell interpreter returned an empty error')
    return interpretation.error
  return _validated_call(interpretation, spells)


async def _interpret(
  command: str,
  spells: list[Spell],
  bro: 'BaseBro',
  harness: mcp.HarnessLike,
  hold: str,
) -> dict[str, Any] | str:
  resolved = await interpret(command, spells)
  if isinstance(resolved, str):
    return {'error': resolved}
  rendered = _render_spell_call(bro, resolved, harness=harness, hold=hold)
  return f'spell: {_canonical_name(resolved.spell)}\n\n{rendered}'


class SpellTool(llm_mcp.Tool):
  def __init__(
    self,
    bro: 'BaseBro',
    spell: Spell,
    *,
    harness: mcp.HarnessLike,
    hold: str,
  ):
    self._bro = bro
    self._spell = spell
    self._harness: mcp.HarnessLike = harness
    self._hold = hold
    properties: dict[str, dict[str, Any]] = {
      parameter.name: parameter.schema() for parameter in spell.parameters
    }
    self._parameters = {
      'type': 'object',
      'properties': properties,
      'required': [parameter.name for parameter in spell.parameters if parameter.required],
      'additionalProperties': False,
    }

  @property
  def name(self) -> str:
    return self._spell.name

  @property
  def description(self) -> str:
    return self._spell.description

  @property
  def parameters(self) -> dict[str, Any]:
    return self._parameters

  async def call(self, arguments: dict[str, Any]) -> str:
    call = _bind_call(self._spell, arguments)
    return _render_spell_call(self._bro, call, harness=self._harness, hold=self._hold)


class SkillTool(llm_mcp.Tool):
  @property
  def name(self) -> str:
    return 'skill'

  @property
  def description(self) -> str:
    return (
      'load a named third-party skill and return its instructions. an unavailable skill returns '
      'an empty body.'
    )

  @property
  def parameters(self) -> dict[str, Any]:
    return {
      'type': 'object',
      'properties': {
        'name': {'type': 'string', 'description': 'name of the third-party skill to load'}
      },
      'required': ['name'],
      'additionalProperties': False,
    }

  async def call(self, arguments: dict[str, Any]) -> str:
    if set(arguments) != {'name'}:
      raise ValueError('skill loader requires exactly one "name" argument')
    name = arguments['name']
    if not isinstance(name, str) or len(name.strip()) == 0:
      raise ValueError('skill loader argument "name" must be a non-empty string')
    return ''


class CastTool(llm_mcp.Tool):
  def __init__(
    self,
    bro: 'BaseBro',
    spells: list[Spell],
    *,
    harness: mcp.HarnessLike,
    hold: str,
  ):
    self._bro = bro
    self._spells = spells
    self._harness: mcp.HarnessLike = harness
    self._hold = hold

  @property
  def name(self) -> str:
    return 'cast'

  @property
  def description(self) -> str:
    return (
      "interpret a free-form spell command. returns the resolved spell's instructions to "
      'execute — a `spell: spell::<name>` line, the spell body, and the interpreted arguments — '
      'or `{error: reason}` when no spell applies, the command is ambiguous, or a required '
      'argument is missing.'
    )

  @property
  def parameters(self) -> dict[str, Any]:
    return {
      'type': 'object',
      'properties': {
        'command': {'type': 'string', 'description': 'free-form spell command to interpret'}
      },
      'required': ['command'],
      'additionalProperties': False,
    }

  async def call(self, arguments: dict[str, Any]) -> dict[str, Any] | str:
    if set(arguments) != {'command'}:
      raise ValueError('spell interpreter requires exactly one "command" argument')
    command = arguments['command']
    if not isinstance(command, str) or len(command.strip()) == 0:
      raise ValueError('spell interpreter argument "command" must be a non-empty string')
    return await _interpret(command, self._spells, self._bro, self._harness, self._hold)


def _load_bro_spells(bro: 'BaseBro') -> list[Spell]:
  return [load_spell(name, path) for name, path in bro.spell_paths.items()]


def build_cast_tool(bro: 'BaseBro', *, harness: mcp.HarnessLike, hold: str) -> llm_mcp.Tool:
  return CastTool(bro, _load_bro_spells(bro), harness=harness, hold=hold)


def build_skill_tool() -> llm_mcp.Tool:
  return SkillTool()


def build_spell_server(bro: 'BaseBro', *, harness: mcp.HarnessLike, hold: str) -> llm_mcp.MCPServer:
  tools: list[llm_mcp.Tool] = [
    SpellTool(bro, spell, harness=harness, hold=hold) for spell in _load_bro_spells(bro)
  ]
  server = llm_mcp.InProcessMCPServer(NAMESPACE, tools)
  server.tool_universe = tuple(tool.name for tool in tools)
  return server
