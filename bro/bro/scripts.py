import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Self

from pydantic import BaseModel, ConfigDict, model_validator

import llm.mcp
from base import credentials
from base.text_window import window
from bro.procedures import collect_markdown, parse_frontmatter

if TYPE_CHECKING:
  from bro.bro import BaseBro

NAMESPACE = 'at'
SKILL_TOOL_NAME = 'skill'
DISPATCHER_SECRET = 'openai'
WINDOW_LIMIT = 1000
_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


@dataclass(frozen=True)
class Parameter:
  name: str
  description: str
  required: bool


@dataclass(frozen=True)
class Script:
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

  script: Optional[str]
  arguments: Optional[list[_ArgumentPair]]
  error: Optional[str]

  @model_validator(mode='after')
  def validate_result_shape(self) -> Self:
    if self.error is not None:
      if self.script is not None or self.arguments is not None:
        raise ValueError('an error interpretation cannot also contain an executable call')
    elif self.script is None or self.arguments is None:
      raise ValueError('an executable interpretation requires script and arguments')
    return self


def collect_scripts(classes: list[type]) -> dict[str, Path]:
  return collect_markdown(classes, 'scripts')


def _validate_name(kind: str, name: str, path: Path) -> None:
  if _NAME_PATTERN.fullmatch(name) is None:
    raise ValueError(
      f'script {path}: {kind} {name!r} must contain only ASCII letters, digits, "_", or "-"'
    )


def _parse_parameters(raw: str, path: Path) -> tuple[Parameter, ...]:
  try:
    value = json.loads(raw)
  except json.JSONDecodeError as error:
    raise ValueError(f'script {path}: parameters must be a one-line JSON object') from error
  if not isinstance(value, dict):
    raise ValueError(f'script {path}: parameters must be a JSON object')

  parameters: list[Parameter] = []
  names: set[str] = set()
  for declared_name, description in value.items():
    if not isinstance(declared_name, str) or not isinstance(description, str):
      raise ValueError(f'script {path}: parameter names and descriptions must be strings')
    required = not declared_name.endswith('?')
    name = declared_name if required else declared_name[:-1]
    _validate_name('parameter name', name, path)
    if name == 'offset':
      raise ValueError(f'script {path}: parameter name "offset" is reserved for output paging')
    if name in names:
      raise ValueError(f'script {path}: duplicate parameter name {name!r}')
    names.add(name)
    parameters.append(Parameter(name, description, required))
  return tuple(parameters)


def load_script(name: str, path: Path) -> Script:
  _validate_name('filename stem', name, path)
  if name in (NAMESPACE, SKILL_TOOL_NAME):
    raise ValueError(f'script {path}: filename stem {name!r} is reserved by the scripts server')

  frontmatter, body = parse_frontmatter(path.read_text())
  declared_name = frontmatter.get('name')
  if declared_name is not None and declared_name != name:
    raise ValueError(
      f'script {path}: frontmatter name={declared_name!r} disagrees with filename '
      f'stem {name!r}; filename is canonical'
    )
  raw_parameters = frontmatter.get('parameters')
  parameters = () if raw_parameters is None else _parse_parameters(raw_parameters, path)
  return Script(name, path, frontmatter.get('description', ''), parameters, body)


def dispatcher_available() -> bool:
  return credentials.available(DISPATCHER_SECRET)


def _canonical_name(script: Script) -> str:
  return f'@::{script.name}'


def _dispatcher_roster(scripts: list[Script]) -> list[dict[str, Any]]:
  return [
    {
      'script': _canonical_name(script),
      'description': script.description,
      'parameters': {
        'type': 'object',
        'properties': {
          parameter.name: {'type': 'string', 'description': parameter.description}
          for parameter in script.parameters
        },
        'required': [parameter.name for parameter in script.parameters if parameter.required],
        'additionalProperties': False,
      },
    }
    for script in scripts
  ]


def _validated_result(interpretation: _Interpretation, scripts: list[Script]) -> dict[str, Any]:
  if interpretation.error is not None:
    if len(interpretation.error.strip()) == 0:
      raise ValueError('script dispatcher returned an empty error')
    return {'error': interpretation.error}

  assert interpretation.script is not None
  assert interpretation.arguments is not None
  scripts_by_name = {_canonical_name(script): script for script in scripts}
  script = scripts_by_name.get(interpretation.script)
  if script is None:
    raise ValueError(f'script dispatcher selected unknown script {interpretation.script!r}')

  arguments: dict[str, str] = {}
  parameters_by_name = {parameter.name: parameter for parameter in script.parameters}
  for pair in interpretation.arguments:
    if pair.name in arguments:
      raise ValueError(f'script dispatcher returned duplicate argument {pair.name!r}')
    if pair.name not in parameters_by_name:
      raise ValueError(
        f'script dispatcher returned unknown argument {pair.name!r} for {interpretation.script}'
      )
    arguments[pair.name] = pair.value
  missing = [
    parameter.name
    for parameter in script.parameters
    if parameter.required and parameter.name not in arguments
  ]
  if len(missing) > 0:
    raise ValueError(
      f'script dispatcher omitted required arguments for {interpretation.script}: {missing}'
    )
  return {'script': interpretation.script, 'arguments': arguments}


def _interpret(command: str, scripts: list[Script]) -> dict[str, Any]:
  from llm.mu import JSON, mu
  from prompts import get_prompt

  request = {'command': command, 'scripts': _dispatcher_roster(scripts)}
  interpretation = mu(
    get_prompt('script_dispatch.prompt'),
    _Interpretation,
    JSON(request),
    reasoning_effort='low',
  )
  return _validated_result(interpretation, scripts)


class ScriptTool(llm.mcp.Tool):
  def __init__(
    self,
    bro: 'BaseBro',
    script: Script,
    *,
    harness: llm.mcp.Harness,
    wire: llm.mcp.Wire,
  ):
    self._bro = bro
    self._script = script
    self._harness: llm.mcp.Harness = harness
    self._wire: llm.mcp.Wire = wire
    properties: dict[str, dict[str, Any]] = {
      parameter.name: {'type': 'string', 'description': parameter.description}
      for parameter in script.parameters
    }
    properties['offset'] = {
      'type': 'integer',
      'description': '0-based line offset for paging through the script body',
      'default': 0,
    }
    self._parameters = {
      'type': 'object',
      'properties': properties,
      'required': [parameter.name for parameter in script.parameters if parameter.required],
      'additionalProperties': False,
    }

  @property
  def name(self) -> str:
    return self._script.name

  @property
  def description(self) -> str:
    return self._script.description

  @property
  def parameters(self) -> dict[str, Any]:
    return self._parameters

  async def call(self, arguments: dict[str, Any]) -> str:
    parameter_names = {parameter.name for parameter in self._script.parameters}
    unknown = set(arguments) - parameter_names - {'offset'}
    if len(unknown) > 0:
      raise ValueError(f'unknown arguments for script {self.name!r}: {sorted(unknown)}')
    missing = [
      parameter.name
      for parameter in self._script.parameters
      if parameter.required and parameter.name not in arguments
    ]
    if len(missing) > 0:
      raise ValueError(f'missing required arguments for script {self.name!r}: {missing}')
    for parameter in self._script.parameters:
      if parameter.name in arguments and not isinstance(arguments[parameter.name], str):
        raise ValueError(f'script argument {parameter.name!r} must be a string')

    offset = arguments.get('offset', 0)
    if isinstance(offset, bool) or not isinstance(offset, int):
      raise ValueError('script argument "offset" must be an integer')
    body = self._bro.get_script_body(self.name, harness=self._harness, wire=self._wire)
    passed = [
      f'{parameter.name}: {arguments[parameter.name]}'
      for parameter in self._script.parameters
      if parameter.name in arguments
    ]
    if len(passed) > 0:
      body = f'{body}\n\n# Arguments\n\n' + '\n'.join(passed)
    return window(body, offset=offset, limit=WINDOW_LIMIT)


class SkillTool(llm.mcp.Tool):
  @property
  def name(self) -> str:
    return SKILL_TOOL_NAME

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


class DispatcherTool(llm.mcp.Tool):
  def __init__(self, scripts: list[Script]):
    self._scripts = scripts

  @property
  def name(self) -> str:
    return NAMESPACE

  @property
  def description(self) -> str:
    return (
      'interpret a free-form script command. returns an executable '
      '`{script: "@::…", arguments: {name: value}}` call, or `{error: reason}` when no '
      'script applies, the command is ambiguous, or a required argument is missing.'
    )

  @property
  def parameters(self) -> dict[str, Any]:
    return {
      'type': 'object',
      'properties': {
        'command': {'type': 'string', 'description': 'free-form script command to interpret'}
      },
      'required': ['command'],
      'additionalProperties': False,
    }

  async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
    if set(arguments) != {'command'}:
      raise ValueError('script dispatcher requires exactly one "command" argument')
    command = arguments['command']
    if not isinstance(command, str) or len(command.strip()) == 0:
      raise ValueError('script dispatcher argument "command" must be a non-empty string')
    return await asyncio.to_thread(_interpret, command, self._scripts)


def build_server(
  bro: 'BaseBro', *, harness: llm.mcp.Harness, wire: llm.mcp.Wire
) -> llm.mcp.MCPServer:
  scripts = [load_script(name, path) for name, path in bro.scripts.items()]
  tools: list[llm.mcp.Tool] = [
    ScriptTool(bro, script, harness=harness, wire=wire) for script in scripts
  ]
  if len(scripts) > 0 and dispatcher_available():
    tools.append(DispatcherTool(scripts))
  if harness == 'bro':
    tools.append(SkillTool())
  server = llm.mcp.InProcessMCPServer(NAMESPACE, tools)
  server.tool_universe = tuple(tool.name for tool in tools)
  return server
