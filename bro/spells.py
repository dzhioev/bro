import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Self

from pydantic import BaseModel, ConfigDict, model_validator

import bro.llm.mcp as llm_mcp
import bro.mcp as mcp
from bro.base import credentials
from bro.base.text_window import window
from bro.procedures import collect_markdown, parse_frontmatter

if TYPE_CHECKING:
  from bro.bro import BaseBro

NAMESPACE = 'spell'
CAST_SECRET = 'openai'
WINDOW_LIMIT = 1000
_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


@dataclass(frozen=True)
class Parameter:
  name: str
  description: str
  required: bool


@dataclass(frozen=True)
class Spell:
  name: str
  path: Path
  description: str
  parameters: tuple[Parameter, ...]
  body: str


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
    elif self.spell is None or self.arguments is None:
      raise ValueError('an executable interpretation requires spell and arguments')
    return self


def collect_spells(classes: list[type]) -> dict[str, Path]:
  return collect_markdown(classes, 'spells')


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
  for declared_name, description in value.items():
    if not isinstance(declared_name, str) or not isinstance(description, str):
      raise ValueError(f'spell {path}: parameter names and descriptions must be strings')
    required = not declared_name.endswith('?')
    name = declared_name if required else declared_name[:-1]
    _validate_name('parameter name', name, path)
    if name == 'offset':
      raise ValueError(f'spell {path}: parameter name "offset" is reserved for output paging')
    if name in names:
      raise ValueError(f'spell {path}: duplicate parameter name {name!r}')
    names.add(name)
    parameters.append(Parameter(name, description, required))
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
        'properties': {
          parameter.name: {'type': 'string', 'description': parameter.description}
          for parameter in spell.parameters
        },
        'required': [parameter.name for parameter in spell.parameters if parameter.required],
        'additionalProperties': False,
      },
    }
    for spell in spells
  ]


def _render_spell_call(
  bro: 'BaseBro',
  spell: Spell,
  arguments: dict[str, Any],
  *,
  harness: mcp.Harness,
  wire: mcp.Wire,
  offset: int = 0,
) -> str:
  body = bro.get_spell_body(spell.name, harness=harness, wire=wire)
  passed = [
    f'{parameter.name}: {arguments[parameter.name]}'
    for parameter in spell.parameters
    if parameter.name in arguments
  ]
  if len(passed) > 0:
    body = f'{body}\n\n# Arguments\n\n' + '\n'.join(passed)
  return window(body, offset=offset, limit=WINDOW_LIMIT)


def _validated_call(
  interpretation: _Interpretation, spells: list[Spell]
) -> tuple[Spell, dict[str, str]]:
  assert interpretation.spell is not None
  assert interpretation.arguments is not None
  spells_by_name = {_canonical_name(spell): spell for spell in spells}
  spell = spells_by_name.get(interpretation.spell)
  if spell is None:
    raise ValueError(f'spell interpreter selected unknown spell {interpretation.spell!r}')

  arguments: dict[str, str] = {}
  parameters_by_name = {parameter.name: parameter for parameter in spell.parameters}
  for pair in interpretation.arguments:
    if pair.name in arguments:
      raise ValueError(f'spell interpreter returned duplicate argument {pair.name!r}')
    if pair.name not in parameters_by_name:
      raise ValueError(
        f'spell interpreter returned unknown argument {pair.name!r} for {interpretation.spell}'
      )
    arguments[pair.name] = pair.value
  missing = [
    parameter.name
    for parameter in spell.parameters
    if parameter.required and parameter.name not in arguments
  ]
  if len(missing) > 0:
    raise ValueError(
      f'spell interpreter omitted required arguments for {interpretation.spell}: {missing}'
    )
  return spell, arguments


async def _interpret(
  command: str,
  spells: list[Spell],
  bro: 'BaseBro',
  harness: mcp.Harness,
  wire: mcp.Wire,
) -> dict[str, Any] | str:
  from bro.llm.mu import JSON, mu
  from bro.prompts import get_prompt

  request = {'command': command, 'spells': _cast_roster(spells)}
  interpretation = await mu.aio(
    get_prompt('spell_dispatch.prompt'),
    _Interpretation,
    JSON(request),
    model='gpt-5.6-luna',
    reasoning_effort='low',
  )
  if interpretation.error is not None:
    if len(interpretation.error.strip()) == 0:
      raise ValueError('spell interpreter returned an empty error')
    return {'error': interpretation.error}
  spell, arguments = _validated_call(interpretation, spells)
  rendered = _render_spell_call(bro, spell, arguments, harness=harness, wire=wire)
  return f'spell: {_canonical_name(spell)}\n\n{rendered}'


class SpellTool(llm_mcp.Tool):
  def __init__(
    self,
    bro: 'BaseBro',
    spell: Spell,
    *,
    harness: mcp.Harness,
    wire: mcp.Wire,
  ):
    self._bro = bro
    self._spell = spell
    self._harness: mcp.Harness = harness
    self._wire: mcp.Wire = wire
    properties: dict[str, dict[str, Any]] = {
      parameter.name: {'type': 'string', 'description': parameter.description}
      for parameter in spell.parameters
    }
    properties['offset'] = {
      'type': 'integer',
      'description': '0-based line offset for paging through the spell body',
      'default': 0,
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
    parameter_names = {parameter.name for parameter in self._spell.parameters}
    unknown = set(arguments) - parameter_names - {'offset'}
    if len(unknown) > 0:
      raise ValueError(f'unknown arguments for spell {self.name!r}: {sorted(unknown)}')
    missing = [
      parameter.name
      for parameter in self._spell.parameters
      if parameter.required and parameter.name not in arguments
    ]
    if len(missing) > 0:
      raise ValueError(f'missing required arguments for spell {self.name!r}: {missing}')
    for parameter in self._spell.parameters:
      if parameter.name in arguments and not isinstance(arguments[parameter.name], str):
        raise ValueError(f'spell argument {parameter.name!r} must be a string')

    offset = arguments.get('offset', 0)
    if isinstance(offset, bool) or not isinstance(offset, int):
      raise ValueError('spell argument "offset" must be an integer')
    return _render_spell_call(
      self._bro, self._spell, arguments, harness=self._harness, wire=self._wire, offset=offset
    )


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
    harness: mcp.Harness,
    wire: mcp.Wire,
  ):
    self._bro = bro
    self._spells = spells
    self._harness: mcp.Harness = harness
    self._wire: mcp.Wire = wire

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
    return await _interpret(command, self._spells, self._bro, self._harness, self._wire)


def _load_bro_spells(bro: 'BaseBro') -> list[Spell]:
  return [load_spell(name, path) for name, path in bro.spells.items()]


def build_cast_tool(bro: 'BaseBro', *, harness: mcp.Harness, wire: mcp.Wire) -> llm_mcp.Tool:
  return CastTool(bro, _load_bro_spells(bro), harness=harness, wire=wire)


def build_skill_tool() -> llm_mcp.Tool:
  return SkillTool()


def build_spell_server(
  bro: 'BaseBro', *, harness: mcp.Harness, wire: mcp.Wire
) -> llm_mcp.MCPServer:
  tools: list[llm_mcp.Tool] = [
    SpellTool(bro, spell, harness=harness, wire=wire) for spell in _load_bro_spells(bro)
  ]
  server = llm_mcp.InProcessMCPServer(NAMESPACE, tools)
  server.tool_universe = tuple(tool.name for tool in tools)
  return server
