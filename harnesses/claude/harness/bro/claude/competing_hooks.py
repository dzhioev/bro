"""The parts of a session's own Claude configuration that could take a call around a tool's gate.

Claude applies the `updatedInput` of every `PreToolUse` hook a call matches, and
the last rewrite wins, so another hook on a gated tool could rewrite a call
after its gate.
`find` lists them wherever the pinned Claude Code loads them for a session:
the project's settings and local settings in its working directory;
the frontmatter of the skills, commands, and agents in the session's Claude
folder, in the `.claude` folder of the working directory and of each directory
above it, and in every `.claude` folder below it, which Claude loads once it
touches a file there;
and the hooks of every installed plugin the user, project, or local settings
enable, with the frontmatter of that plugin's own skills, commands, and agents.
A candidate the scan cannot read or parse is listed too.
Managed settings are the organization's policy, which outranks a persona's,
and stay out of it.
"""

import json
import os
import re
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import yaml

# a matcher Claude reads as a list of tool names rather than as a pattern
_NAME_LIST = re.compile(r'[a-zA-Z0-9_|, -]+')
_FRONTMATTER = re.compile(r'---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)', re.DOTALL)
# Claude's second reading of frontmatter its YAML parser refused: a top-level
# `key: value` whose value holds one of these is read as a double-quoted string
_PLAIN_FIELD = re.compile(r'([a-zA-Z_-]+):\s+(.+)')
_QUOTED_VALUE = re.compile(r'[{}[\]*&#!|>%@`]|: ')
_COMPONENT_KINDS = ('skills', 'commands', 'agents')


class _Unreadable(Exception):
  """a candidate the scan cannot read or parse."""


def _matches(matcher: Any, tools: tuple[str, ...]) -> bool:
  """whether Claude applies a hook group with `matcher` to one of `tools`."""
  if matcher is None or matcher == '' or matcher == '*':
    return True
  if not isinstance(matcher, str):
    raise _Unreadable(f'a matcher that is not a string: {matcher!r}')
  if _NAME_LIST.fullmatch(matcher) is not None:
    names = {name.strip() for name in re.split(r'[|,]', matcher)}
    return any(tool in names for tool in tools)
  try:
    pattern = re.compile(matcher)
  except re.error as error:
    raise _Unreadable(f'a matcher pattern the scan cannot read: {matcher!r}') from error
  return any(pattern.search(tool) is not None for tool in tools)


def _gate_hooks(hooks: Any, tools: tuple[str, ...]) -> list[str]:
  """each `PreToolUse` hook group of a `hooks` block that runs on one of `tools`."""
  if not isinstance(hooks, dict):
    raise _Unreadable('a hooks block that is not a mapping')
  groups = hooks.get('PreToolUse', [])
  if not isinstance(groups, list):
    raise _Unreadable('PreToolUse hooks that are not a list')
  found = []
  for group in groups:
    if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
      raise _Unreadable('a PreToolUse hook group without a list of hooks')
    matcher = group.get('matcher')
    if len(group['hooks']) > 0 and _matches(matcher, tools):
      label = 'every tool' if matcher in (None, '', '*') else repr(matcher)
      found.append(f'a PreToolUse hook matching {label}')
  return found


def _json_object(path: Path) -> dict[str, Any]:
  """`path`'s JSON object, empty where the file does not exist."""
  if not path.exists():
    return {}
  try:
    value = json.loads(path.read_text())
  except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
    raise _Unreadable(str(error)) from error
  if not isinstance(value, dict):
    raise _Unreadable('not a JSON object')
  return value


def _enabled_plugins(settings: dict[str, Any]) -> dict[str, Any]:
  enabled = settings.get('enabledPlugins', {})
  if not isinstance(enabled, dict):
    raise _Unreadable('enabledPlugins that is not a mapping')
  return enabled


def _quote_plain_fields(text: str) -> str:
  lines = []
  for line in text.split('\n'):
    field = _PLAIN_FIELD.fullmatch(line)
    if field is not None and _QUOTED_VALUE.search(field.group(2)) is not None:
      key, value = field.groups()
      if not (value[0] == value[-1] and value[0] in '"\'') and not _is_list(value):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        line = f'{key}: "{escaped}"'
    lines.append(line)
  return re.sub(r'^\t+', lambda tabs: '  ' * len(tabs.group()), '\n'.join(lines), flags=re.M)


def _is_list(value: str) -> bool:
  if not (value.startswith('[') and value.endswith(']')):
    return False
  try:
    return isinstance(yaml.safe_load(value), list)
  except yaml.YAMLError:
    return False


def _frontmatter_findings(path: Path, tools: tuple[str, ...]) -> list[str]:
  try:
    text = path.read_text()
  except (OSError, UnicodeDecodeError) as error:
    raise _Unreadable(str(error)) from error
  frontmatter = _FRONTMATTER.match(text)
  if frontmatter is None:
    return []
  try:
    fields = yaml.safe_load(frontmatter.group(1))
  except yaml.YAMLError:
    try:
      fields = yaml.safe_load(_quote_plain_fields(frontmatter.group(1)))
    except yaml.YAMLError as error:
      raise _Unreadable(f'frontmatter that is not YAML: {error}') from error
  if not isinstance(fields, dict) or fields.get('hooks') is None:
    return []
  return _gate_hooks(fields['hooks'], tools)


def _component_files(path: Path, kind: str) -> Iterator[Path]:
  """the files Claude reads as `kind` components at `path`: each skill's
  `SKILL.md`, and every markdown file of commands and agents."""
  if path.is_file():
    yield path
    return
  for root, _, names in os.walk(path):
    for name in sorted(names):
      if (name == 'SKILL.md') if kind == 'skills' else name.endswith('.md'):
        yield Path(root) / name


def _claude_folders(project: Path) -> Iterator[Path]:
  """the `.claude` folders of `project`, of each directory above it, and below it."""
  for directory in (project, *project.parents):
    yield directory / '.claude'
  for root, directories, _ in os.walk(project):
    if Path(root) != project and '.claude' in directories:
      yield Path(root) / '.claude'


def _manifest_paths(root: Path, manifest: dict[str, Any], field: str) -> list[Path]:
  value = manifest.get(field, [])
  paths = [value] if isinstance(value, str) else value
  if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
    raise _Unreadable(f'a manifest {field} that is not a path or a list of paths')
  return [root / path for path in paths]


def _installed_roots(path: Path, keys: list[str]) -> list[Path]:
  """the install paths `path`, the installed-plugin record, holds for `keys`."""
  plugins = _json_object(path).get('plugins', {})
  if not isinstance(plugins, dict):
    raise _Unreadable('a plugins record that is not a mapping')
  roots = []
  for key in keys:
    for entry in plugins.get(key, []):
      if not isinstance(entry, dict) or not isinstance(entry.get('installPath'), str):
        raise _Unreadable(f'an installation of {key} without an install path')
      roots.append(Path(entry['installPath']))
  return roots


def _settings(path: Path, tools: tuple[str, ...]) -> tuple[list[str], dict[str, Any]]:
  """a project settings file's findings, and the plugins it enables or disables."""
  settings = _json_object(path)
  return _gate_hooks(settings.get('hooks', {}), tools), _enabled_plugins(settings)


def _hooks_file(path: Path, tools: tuple[str, ...]) -> list[str]:
  return _gate_hooks(_json_object(path).get('hooks', {}), tools)


class _Scan:
  """the findings of one scan for hooks on `tools`, a candidate it cannot read
  among them."""

  def __init__(self, tools: tuple[str, ...]) -> None:
    self.tools = tools
    self.found: list[str] = []

  def read[T](self, path: Path, compute: Callable[[], T], unread: T) -> T:
    """`compute`'s result, or `unread` once the scan records it cannot read `path`."""
    try:
      return compute()
    except _Unreadable as error:
      self.found.append(f'{path}: cannot be read: {error}')
      return unread

  def report(self, path: Path, findings: list[str]) -> None:
    self.found.extend(f'{path}: {finding}' for finding in findings)

  def components(self, path: Path, kind: str) -> None:
    if path.exists():
      for file in _component_files(path, kind):
        self.report(file, self.read(file, partial(_frontmatter_findings, file, self.tools), []))

  def plugin(self, root: Path) -> None:
    manifest_path = root / '.claude-plugin' / 'plugin.json'
    manifest = self.read(manifest_path, partial(_json_object, manifest_path), {})

    def listed(field: str) -> list[Path]:
      return self.read(manifest_path, partial(_manifest_paths, root, manifest, field), [])

    hooks = manifest.get('hooks')
    hooks_files = [root / 'hooks' / 'hooks.json']
    if isinstance(hooks, dict):
      self.report(
        manifest_path, self.read(manifest_path, partial(_gate_hooks, hooks, self.tools), [])
      )
    else:
      hooks_files.extend(listed('hooks'))
    for path in hooks_files:
      if path.exists():
        self.report(path, self.read(path, partial(_hooks_file, path, self.tools), []))
    for kind in _COMPONENT_KINDS:
      for path in (root / kind, *listed(kind)):
        self.components(path, kind)


def find(project: Path, config: Path, tools: tuple[str, ...]) -> list[str]:
  """each place the Claude configuration of a session working in `project`, with
  `config` as its Claude folder, carries a hook that could rewrite a call of one
  of `tools` after its gate, as one line naming the file and what it carries."""
  scan = _Scan(tools)
  user_settings = config / 'settings.json'
  enabled = scan.read(user_settings, partial(_settings, user_settings, tools), ([], {}))[1]
  for name in ('settings.json', 'settings.local.json'):
    path = project / '.claude' / name
    findings, plugins = scan.read(path, partial(_settings, path, tools), ([], {}))
    scan.report(path, findings)
    enabled = {**enabled, **plugins}

  for folder in (config, *_claude_folders(project)):
    for kind in _COMPONENT_KINDS:
      scan.components(folder / kind, kind)

  keys = [key for key, value in enabled.items() if value]
  installed = config / 'plugins' / 'installed_plugins.json'
  for root in scan.read(installed, partial(_installed_roots, installed, keys), []):
    scan.plugin(root)
  return scan.found
