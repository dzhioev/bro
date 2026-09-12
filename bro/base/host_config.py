"""The host's launch policy (`~/.bro.json`).

One host may serve several projects, bros, and command-line tools that read
separate stored instances of one credential kind.
Selections live outside repositories and merge from general to specific:

    {
      "defaults": {"creds": ["trails+write"]},
      "user": {
        "creds": ["github+dev"],
        "tools": {"bro.trails.rewind": {"creds": ["trails+analyst"]}}
      },
      "projects": {
        "https://github.com/foo/api.git": {
          "creds": ["brog+github", "github+dev"],
          "bros": {
            "eyebro": {"creds": ["trails+review"], "grant": ["github+reviewer"]}
          }
        },
        "/home/foo/projects/api": {"creds": ["aws+laptop"]}
      },
      "llm": {"sharp": "openai:sol:max"},
      "summon-depth": 4
    }

Every `creds` entry is `kind+instance`, the instance left empty (`kind+`) to
select the kind's empty instance.
A list may name each kind once.
`defaults`, project entries, and `bros` entries may carry unified `grant` and
`revoke` lists: credential names, `@bro` targets, and `:permit` leaves.
An instance-spelled credential grant selects the instance as `creds` does.
An entry names a credential kind in `creds` or in `grant`, not both.
The grammar is installation-independent, so unknown credential and bro names
remain valid until a launch resolves them against its installation.

`defaults` is the root both branches extend: `user` for a command the operator
runs outside any session, `projects` for a managed session.
A project key is an identity a session names the repository by: the filesystem
path of its root (`~` and symlinks resolved before matching), or a normalized
git URL.
A launch carries both where it has both — a checkout's path and the origin URL
naming it portably — and every entry either one matches applies, so the URL
entry holds what follows the repository across machines and the path entry
names only what one machine changes.
A `user.tools` key is one CLI's canonical console-script name — its import path
with the underscores dashed — rather than the bare alias, which several
distributions may each publish.
Launch selection and scope precedence runs defaults, the URL entry, the path entry,
then each of their `bros` layers in that same order;
a command's credential selection is its `user.tools` entry, `user`, then defaults.
A kind no layer selects reads its empty instance.
The returned layer map attributes every explicit selection, and the returned
grant set carries the credential kinds left granted by the matching scope layers.

The file is optional.
`llm` remains the host-wide table of `--llm` preset names.
`summon-depth` overrides the repository's summon depth for every launch.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import configs, credentials
from bro.base.git_url import is_git_url, normalize_git_url
from bro.base.scope import ScopeLayer, split_scope_overrides, validate_scope_layer

# Module-level so tests can point it at a fixture path; read at call time.
HOST_CONFIG_FILE = configs.DEFAULT_HOST_CONFIG

DEFAULTS_LAYER = 'defaults'
USER_LAYER = 'user'
PROJECT_URL_LAYER = 'project-url'
PROJECT_PATH_LAYER = 'project-path'
PROJECT_URL_BRO_LAYER = 'project-url-bro'
PROJECT_PATH_BRO_LAYER = 'project-path-bro'
TOOL_LAYER = 'tool'
BRO_LAYERS = frozenset({PROJECT_URL_BRO_LAYER, PROJECT_PATH_BRO_LAYER})

_DEFAULTS_KEY = 'defaults'
_USER_KEY = 'user'
_PROJECTS_KEY = 'projects'
_TOOLS_KEY = 'tools'
_CREDS_KEY = 'creds'
_GRANT_KEY = 'grant'
_REVOKE_KEY = 'revoke'
_BROS_KEY = 'bros'
_LLM_KEY = 'llm'
_SUMMON_DEPTH_KEY = 'summon-depth'
_RETIRED_INSTANCES_KEY = 'instances'


@dataclass(frozen=True)
class Attachment:
  """The identities a launch matches `projects` entries by: the operated
  repository's checkout path, the git URL it is reachable at, or both."""

  path: Optional[str] = None
  url: Optional[str] = None

  def __post_init__(self) -> None:
    if self.path is None and self.url is None:
      raise ValueError('an attachment names a checkout path, a git URL, or both')
    if self.path is not None and (self.path == '' or is_git_url(self.path)):
      raise ValueError(f'attachment path must be a filesystem path, not {self.path!r}')
    if self.url is not None and not is_git_url(self.url):
      raise ValueError(f'attachment url must be a git URL, not {self.url!r}')


@dataclass(frozen=True)
class CredentialSelection:
  """A merged selection, its provenance, and its ordered scope layers."""

  instances: dict[str, str]
  layers: dict[str, str]
  grants: frozenset[str] = frozenset()
  scope_layers: tuple[ScopeLayer, ...] = ()


@dataclass(frozen=True)
class _ScopeEntry:
  selection: dict[str, str]
  scope: ScopeLayer


@dataclass(frozen=True)
class _Project:
  entry: _ScopeEntry
  bros: dict[str, _ScopeEntry]


@dataclass(frozen=True)
class _Match:
  layer: str
  bro_layer: str
  project: _Project


@dataclass(frozen=True)
class _User:
  credentials: dict[str, str]
  tools: dict[str, dict[str, str]]


@dataclass(frozen=True)
class _Config:
  defaults: _ScopeEntry
  user: _User
  projects: dict[str, _Project]
  llm: dict[str, str]
  summon_depth: Optional[int]


def _read() -> _Config:
  path = Path(HOST_CONFIG_FILE)
  if not path.is_file():
    return _Config(_ScopeEntry({}, ScopeLayer()), _User({}, {}), {}, {}, None)
  try:
    data = json.loads(path.read_text())
  except json.JSONDecodeError as error:
    raise ValueError(f'{path} is not valid json') from error
  if not isinstance(data, dict):
    raise ValueError(f'{path} must hold a json object')
  unknown = sorted(
    set(data) - {_DEFAULTS_KEY, _USER_KEY, _PROJECTS_KEY, _LLM_KEY, _SUMMON_DEPTH_KEY}
  )
  if _TOOLS_KEY in unknown:
    raise ValueError(f'{path}: top-level {_TOOLS_KEY!r} is retired; nest it under {_USER_KEY!r}')
  if len(unknown) > 0:
    raise ValueError(f'unknown key(s) in {path}: {", ".join(unknown)}')
  defaults = _scope_entry(f'{path}: {_DEFAULTS_KEY}', data.get(_DEFAULTS_KEY, {}))
  user = _user(path, data.get(_USER_KEY, {}))
  projects = _projects(path, data.get(_PROJECTS_KEY, {}))
  llm = _llm(path, data.get(_LLM_KEY, {}))
  summon_depth = (
    _positive_integer(path, _SUMMON_DEPTH_KEY, data[_SUMMON_DEPTH_KEY])
    if _SUMMON_DEPTH_KEY in data
    else None
  )
  return _Config(defaults, user, projects, llm, summon_depth)


def llm_presets() -> dict[str, str]:
  """The host's `--llm` preset names, mapped to the values they stand for."""
  config = _read()
  return dict(config.llm)


def summon_depth(project_depth: Optional[int] = None) -> int:
  """The host override, then the project value, then the framework default."""
  config = _read()
  if config.summon_depth is not None:
    return config.summon_depth
  if project_depth is None:
    return configs.DEFAULT_SUMMON_DEPTH
  if not isinstance(project_depth, int) or isinstance(project_depth, bool) or project_depth <= 0:
    raise ValueError('project summon depth must be a positive integer')
  return project_depth


def project_selection(attachment: Optional[Attachment]) -> CredentialSelection:
  """Merge defaults and the matching projects, without a per-bro layer."""
  config = _read()
  entries = [(DEFAULTS_LAYER, config.defaults)]
  entries.extend((match.layer, match.project.entry) for match in _matches(config, attachment))
  return _merged(entries)


def launch_selection(attachment: Optional[Attachment], bro: str) -> CredentialSelection:
  """Merge defaults, the matching projects, and their `bro` layers."""
  if not isinstance(bro, str) or bro == '':
    raise ValueError('bro name must be a non-empty string')
  config = _read()
  matches = _matches(config, attachment)
  entries = [(DEFAULTS_LAYER, config.defaults)]
  entries.extend((match.layer, match.project.entry) for match in matches)
  entries.extend(
    (match.bro_layer, match.project.bros[bro]) for match in matches if bro in match.project.bros
  )
  return _merged(entries)


def tool_selection(
  command: Optional[str], *, invoked_as: Optional[str] = None
) -> CredentialSelection:
  """Merge defaults, the user layer, and the entry for `command`.

  `command` is the canonical console-script name of the running CLI, None for a
  process that is none. `invoked_as` is the name it was started under, and a
  `user.tools` entry keyed by that alias instead of by `command` fails the read.
  """
  if command is not None and (not isinstance(command, str) or command == ''):
    raise ValueError('command name must be a non-empty string')
  config = _read()
  layers = [(DEFAULTS_LAYER, config.defaults.selection), (USER_LAYER, config.user.credentials)]
  if command is not None:
    if invoked_as is not None and invoked_as != command and invoked_as in config.user.tools:
      raise ValueError(
        f'{HOST_CONFIG_FILE}: {_USER_KEY}.{_TOOLS_KEY} names {invoked_as!r}, an alias of '
        f'{command!r}; key it by the canonical name'
      )
    tool_credentials = config.user.tools.get(command)
    if tool_credentials is not None:
      layers.append((TOOL_LAYER, tool_credentials))
  return _merged(layers)


def _matches(config: _Config, attachment: Optional[Attachment]) -> list[_Match]:
  """The entries the attachment's identities name, least specific first."""
  if attachment is None:
    return []
  identities = [
    (attachment.url, PROJECT_URL_LAYER, PROJECT_URL_BRO_LAYER, normalize_git_url),
    (attachment.path, PROJECT_PATH_LAYER, PROJECT_PATH_BRO_LAYER, _path_key),
  ]
  matches = []
  for identity, layer, bro_layer, reduce_key in identities:
    project = None if identity is None else config.projects.get(reduce_key(identity))
    if project is not None:
      matches.append(_Match(layer, bro_layer, project))
  return matches


def _merged(
  layers: Sequence[tuple[str, dict[str, str] | _ScopeEntry]],
) -> CredentialSelection:
  instances: dict[str, str] = {}
  sources: dict[str, str] = {}
  scope_layers: list[ScopeLayer] = []
  granted_credentials: set[str] = set()
  for layer, entry in layers:
    selection = entry.selection if isinstance(entry, _ScopeEntry) else entry
    instances.update(selection)
    sources.update(dict.fromkeys(selection, layer))
    if not isinstance(entry, _ScopeEntry):
      continue
    if entry.scope != ScopeLayer():
      scope_layers.append(entry.scope)
    grant_credentials, _, _ = split_scope_overrides(entry.scope.grant)
    revoke_credentials, _, _ = split_scope_overrides(entry.scope.revoke)
    granted_credentials.update(credentials.parse_name(value)[0] for value in grant_credentials)
    granted_credentials.difference_update(
      credentials.parse_name(value)[0] for value in revoke_credentials
    )
  return CredentialSelection(
    instances,
    sources,
    frozenset(granted_credentials),
    tuple(scope_layers),
  )


def _projects(path: Path, value: object) -> dict[str, _Project]:
  if not isinstance(value, dict):
    raise ValueError(f'{path}: {_PROJECTS_KEY} must be a json object')
  projects: dict[str, _Project] = {}
  for key, entry in value.items():
    identity = _project_key(key)
    if identity in projects:
      raise ValueError(f'{path}: two project keys name the same identity {identity}')
    projects[identity] = _project(path, key, entry)
  return projects


def _project(path: Path, project: str, value: object) -> _Project:
  where = f'{path}: project {project!r}'
  if not isinstance(value, dict):
    raise ValueError(f'{where} must hold a json object')
  _reject_unknown_fields(value, {_CREDS_KEY, _GRANT_KEY, _REVOKE_KEY, _BROS_KEY}, where)
  project_entry = _scope_entry(
    where, {key: entry for key, entry in value.items() if key != _BROS_KEY}
  )
  bros = value.get(_BROS_KEY, {})
  if not isinstance(bros, dict):
    raise ValueError(f'{where}: {_BROS_KEY} must be a json object')
  parsed_bros: dict[str, _ScopeEntry] = {}
  for bro, bro_entry in bros.items():
    if bro == '':
      raise ValueError(f'{where}: bro name must not be empty')
    parsed_bros[bro] = _scope_entry(f'{where}: bro {bro!r}', bro_entry)
  return _Project(project_entry, parsed_bros)


def _scope_entry(where: str, value: object) -> _ScopeEntry:
  if not isinstance(value, dict):
    raise ValueError(f'{where} must hold a json object')
  _reject_unknown_fields(value, {_CREDS_KEY, _GRANT_KEY, _REVOKE_KEY}, where)
  selection = _selection_entries(where, value.get(_CREDS_KEY, []))
  grant = _scope_values(where, value.get(_GRANT_KEY, []), _GRANT_KEY)
  revoke = _scope_values(where, value.get(_REVOKE_KEY, []), _REVOKE_KEY)
  scope = ScopeLayer(grant, revoke)
  try:
    validate_scope_layer(scope, allow_credential_instances=True)
  except ValueError as error:
    raise ValueError(f'{where}: {error}') from error
  grant_credentials, _, _ = split_scope_overrides(grant)
  for granted in grant_credentials:
    kind, instance = credentials.parse_name(granted)
    if kind in selection:
      raise ValueError(f'{where} names kind {kind!r} in both {_CREDS_KEY} and {_GRANT_KEY}')
    if instance is not None:
      selection[kind] = instance
  return _ScopeEntry(selection, scope)


def _scope_values(where: str, values: object, key: str) -> tuple[str, ...]:
  if not isinstance(values, list):
    raise ValueError(f'{where}: {key} must be a list')
  for value in values:
    if not isinstance(value, str):
      raise ValueError(f'{where}: {key} {value!r} must be a string')
    if value == '':
      raise ValueError(f'{where}: {key} must not contain an empty string')
  return tuple(values)


def _user(path: Path, value: object) -> _User:
  where = f'{path}: {_USER_KEY}'
  if not isinstance(value, dict):
    raise ValueError(f'{where} must hold a json object')
  _reject_unknown_fields(value, {_CREDS_KEY, _TOOLS_KEY}, where)
  tools = value.get(_TOOLS_KEY, {})
  if not isinstance(tools, dict):
    raise ValueError(f'{where}: {_TOOLS_KEY} must be a json object')
  parsed_tools: dict[str, dict[str, str]] = {}
  for name, entry in tools.items():
    if name == '':
      raise ValueError(f'{where}: command name must not be empty')
    parsed_tools[name] = _selection_object(path, f'{where}: command {name!r}', entry)
  return _User(_selection_entries(where, value.get(_CREDS_KEY, [])), parsed_tools)


def _selection_object(path: Path, subject: str, value: object) -> dict[str, str]:
  where = f'{path}: {subject}' if not subject.startswith(f'{path}:') else subject
  if not isinstance(value, dict):
    raise ValueError(f'{where} must hold a json object')
  _reject_unknown_fields(value, {_CREDS_KEY}, where)
  return _selection_entries(where, value.get(_CREDS_KEY, []))


def _positive_integer(path: Path, name: str, value: object) -> int:
  if type(value) is not int or value <= 0:
    raise ValueError(f'{path}: {name} must be a positive integer')
  return value


def _reject_unknown_fields(value: dict, allowed: set[str], where: str) -> None:
  unknown = sorted(set(value) - allowed)
  if _RETIRED_INSTANCES_KEY in unknown:
    raise ValueError(f'{where}: {_RETIRED_INSTANCES_KEY!r} is retired; use {_CREDS_KEY!r}')
  if len(unknown) > 0:
    raise ValueError(f'{where} has unknown field(s): {", ".join(unknown)}')


def _selection_entries(where: str, entries: object) -> dict[str, str]:
  if not isinstance(entries, list):
    raise ValueError(f'{where}: {_CREDS_KEY} must be a list')
  selection: dict[str, str] = {}
  for entry in entries:
    kind, instance = _parse_selection(where, entry)
    if kind in selection:
      raise ValueError(f'{where} selects kind {kind!r} twice')
    selection[kind] = instance
  return selection


def _parse_selection(where: str, entry: object) -> tuple[str, str]:
  if not isinstance(entry, str):
    raise ValueError(f'{where}: selection {entry!r} must be a string')
  kind, separator, instance = entry.partition('+')
  if separator == '':
    raise ValueError(
      f'{where}: selection {entry!r} names no instance; write '
      f"'{entry}+<instance>', or '{entry}+' for the kind's empty instance"
    )
  credentials.parse_name(entry)
  return kind, instance


def _project_key(key: str) -> str:
  """Reduce a `projects` key to the identity an attachment matches it on."""
  if not isinstance(key, str) or key == '':
    raise ValueError('project key must be a non-empty string')
  return normalize_git_url(key) if is_git_url(key) else _path_key(key)


def _path_key(path: str) -> str:
  return str(Path(os.path.expanduser(path)).resolve())


def _llm(path: Path, value: object) -> dict[str, str]:
  if not isinstance(value, dict):
    raise ValueError(f'{path}: {_LLM_KEY} must be a json object')
  for name, recipe in value.items():
    if not isinstance(recipe, str) or recipe == '':
      raise ValueError(f'{path}: {_LLM_KEY} preset {name!r} must be a non-empty string')
  return dict(value)
