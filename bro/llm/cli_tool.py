"""an allow-listed CLI command served as a generated tool.

The tool's signature is derived from the command's own argument declarations
(`bro.base.args.command_signature`) when the server is built, so what a tool
advertises is what the command it will run actually accepts. A call spells a
fixed argv — the program and its subcommands come from the declaration, never
from the model, and no shell interprets it — so a tool reaches the one command
it was declared for and nothing else. Whether that command is read-only is the
declaration's business; nothing here constrains it. Beside the command's own
arguments, every tool takes a window over the command's output.
"""

import subprocess
from typing import Any

from bro.base import spawn
from bro.base.args import Argument, CommandSignature, command_signature
from bro.base.text_window import BYTE_LIMIT, DEFAULT_LIMIT, MAX_LIMIT, format_size
from bro.llm.mcp import InProcessMCPServer, Tool

NAMESPACE = 'cli'

TIMEOUT_SECONDS = 45

OFFSET = 'output_offset'
LIMIT = 'output_limit'

_WINDOW_SCHEMA: dict[str, dict[str, Any]] = {
  OFFSET: {
    'type': 'integer',
    'minimum': 0,
    'description': 'output lines to skip before the window (default 0)',
  },
  LIMIT: {
    'type': 'integer',
    'description': (
      f'max output lines to return (default {DEFAULT_LIMIT}); values above {MAX_LIMIT:,} '
      f'are clamped, with the clamp announced inline, and a window also stops at '
      f'{format_size(BYTE_LIMIT)}'
    ),
  },
}


def _description(signature: CommandSignature, arguments: tuple[Argument, ...]) -> str:
  spelled = ' '.join(signature.command)
  narrowing = ', or narrow it with the command arguments' if len(arguments) > 0 else ''
  return (
    f'{signature.description}\n\n'
    f'runs `{spelled}` and returns its exit code with the command output (stderr '
    f'under a `--- stderr ---` divider). Output past the `{LIMIT}` window is trimmed '
    f'with a skipped-content marker; page through it with `{OFFSET}`{narrowing}. '
    f'The command is killed after {TIMEOUT_SECONDS}s.'
  )


def _window_value(values: dict[str, Any], name: str, default: int) -> int:
  value = values.get(name)
  if value is None:
    return default
  if not isinstance(value, int) or isinstance(value, bool):
    raise ValueError(f'{name!r} takes an integer, got {value!r}')
  return value


def _parameter_schema(argument: Argument) -> dict[str, Any]:
  if argument.kind == 'flag':
    return {'type': 'boolean', 'description': argument.help}
  value: dict[str, Any] = {'type': argument.value_type}
  if len(argument.choices) > 0:
    value['enum'] = list(argument.choices)
  if argument.kind == 'list':
    return {'type': 'array', 'items': value, 'description': argument.help}
  return {**value, 'description': argument.help}


def _option_argv(option: str, value: str) -> list[str]:
  # a long option carries its value attached, so a value that starts with a dash
  # cannot be read as the next option; a short-only option has no attached form.
  if option.startswith('--'):
    return [f'{option}={value}']
  return [option, value]


class _CommandTool(Tool):
  def __init__(self, name: str, signature: CommandSignature, arguments: tuple[Argument, ...]):
    reserved = [argument.name for argument in arguments if argument.name in _WINDOW_SCHEMA]
    if len(reserved) > 0:
      raise ValueError(
        f'{" ".join(signature.command)!r} exposes {", ".join(reserved)}, which the tool '
        'reserves for its output window; withhold it from the exposure'
      )
    self._command = signature.command
    self._arguments = arguments
    self._name = name
    self._description = _description(signature, arguments)
    self._parameters = {
      'type': 'object',
      'properties': {
        **{argument.name: _parameter_schema(argument) for argument in arguments},
        **_WINDOW_SCHEMA,
      },
      'required': [argument.name for argument in arguments if argument.required],
    }

  @property
  def name(self) -> str:
    return self._name

  @property
  def description(self) -> str:
    return self._description

  @property
  def parameters(self) -> dict[str, Any]:
    return self._parameters

  def _argv(self, values: dict[str, Any]) -> list[str]:
    known = {argument.name: argument for argument in self._arguments}
    unknown = sorted(set(values) - set(known) - set(_WINDOW_SCHEMA))
    if len(unknown) > 0:
      raise ValueError(
        f'unknown arguments: {", ".join(unknown)}; known: {", ".join([*known, *_WINDOW_SCHEMA])}'
      )
    options: list[str] = []
    positionals: list[str] = []
    for argument in self._arguments:
      if argument.name not in values or values[argument.name] is None:
        if argument.required:
          raise ValueError(f'missing required argument {argument.name!r}')
        continue
      value = values[argument.name]
      if argument.kind == 'flag':
        if not isinstance(value, bool):
          raise ValueError(f'{argument.name!r} is a flag; pass a boolean, got {value!r}')
        if value:
          assert argument.option is not None  # a positional cannot be flag-shaped
          options.append(argument.option)
        continue
      if isinstance(value, list) and argument.kind != 'list':
        raise ValueError(f'{argument.name!r} takes one value, got a list')
      for item in value if isinstance(value, list) else [value]:
        text = str(item)
        if len(argument.choices) > 0 and text not in argument.choices:
          raise ValueError(
            f'{argument.name!r} must be one of {", ".join(argument.choices)}, got {text!r}'
          )
        if argument.option is None:
          positionals.append(text)
        else:
          options.extend(_option_argv(argument.option, text))
    # `--` closes the option list, so a positional value that looks like an option
    # reaches the command as the value it is.
    separator = ['--'] if len(positionals) > 0 else []
    return [*self._command, *options, *separator, *positionals]

  async def call(self, arguments: dict[str, Any]) -> str:
    argv = self._argv(arguments)
    offset = _window_value(arguments, OFFSET, 0)
    if offset < 0:
      raise ValueError(f'{OFFSET!r} must be non-negative, got {offset}')
    limit = _window_value(arguments, LIMIT, DEFAULT_LIMIT)
    try:
      process = await spawn.run_async(argv, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
      return f'TIMED OUT after {TIMEOUT_SECONDS}s — killed.'
    return spawn.format_result(process, offset=offset, limit=limit)


def _exposed(signature: CommandSignature, names: tuple[str, ...]) -> tuple[Argument, ...]:
  if len(names) == 0:
    return signature.arguments
  declared = {argument.name: argument for argument in signature.arguments}
  unknown = [name for name in names if name not in declared]
  if len(unknown) > 0:
    spelled = ' '.join(signature.command)
    raise ValueError(
      f'{spelled!r} has no arguments {unknown}; declares: {", ".join(declared) or "(none)"}'
    )
  withheld = [
    argument.name
    for argument in signature.arguments
    if argument.required and argument.name not in names
  ]
  if len(withheld) > 0:
    spelled = ' '.join(signature.command)
    raise ValueError(f'{spelled!r} requires {", ".join(withheld)}; the exposure cannot omit them')
  # declaration order, not the order the names were listed in: positionals reach
  # the command in the order it declares them.
  exposed = set(names)
  return tuple(argument for argument in signature.arguments if argument.name in exposed)


def build_server(
  command: tuple[str, ...], arguments: tuple[str, ...], name: str
) -> InProcessMCPServer:
  signature = command_signature(command)
  return InProcessMCPServer(
    NAMESPACE, [_CommandTool(name, signature, _exposed(signature, arguments))]
  )
