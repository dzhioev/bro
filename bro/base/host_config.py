"""The host's credential selection policy (`~/.bro.json`).

One host may serve several projects, bros, and command-line tools that read
separate stored instances of one credential kind.
Selections live outside repositories and merge from general to specific:

    {
      "defaults": {"creds": ["github+dev", "trails+write"]},
      "projects": {
        "/home/foo/projects/api": {
          "creds": ["brog+github", "github+dev"],
          "bros": {"bro-eyebro": {"creds": ["github+reviewer"]}}
        },
        "https://github.com/foo/api.git": {
          "creds": ["brog+github", "github+dev"]
        }
      },
      "tools": {"rewind": {"creds": ["trails+analyst"]}},
      "llm": {"sharp": "openai:sol:max"}
    }

Every `creds` entry is `kind+instance`, or `kind+` to select the kind's bare
`creds/<kind>.cred` material.
A list may name each kind once.
The grammar is installation-independent, so selections for unknown kinds remain
valid and are carried in the returned mappings.

A project key is the attachment a session names it by: the filesystem path of
the operated repository's root (`~` and symlinks resolved before matching), or
a normalized git URL.
A repository attached both ways therefore carries an entry per identity.
Launch selection precedence is project-bro, project, defaults, then bare store
material; tool selection precedence is tool, defaults, then bare material.
The returned layer map attributes every explicit selection.

A detached launch or an attachment no project entry names refuses kinds selected
by any project or project-bro layer unless defaults selects the kind.
Tool selections never cause a launch refusal.

The file is optional.
`llm` remains the host-wide table of `--llm` preset names.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import configs, credentials
from bro.base.git_url import is_git_url, normalize_git_url

# Module-level so tests can point it at a fixture path; read at call time.
HOST_CONFIG_FILE = configs.DEFAULT_HOST_CONFIG

DEFAULTS_LAYER = 'defaults'
PROJECT_LAYER = 'project'
PROJECT_BRO_LAYER = 'project-bro'
TOOL_LAYER = 'tool'

_DEFAULTS_KEY = 'defaults'
_PROJECTS_KEY = 'projects'
_TOOLS_KEY = 'tools'
_CREDS_KEY = 'creds'
_BROS_KEY = 'bros'
_LLM_KEY = 'llm'
_RETIRED_INSTANCES_KEY = 'instances'


@dataclass(frozen=True)
class UnboundKinds:
  """The kinds a selection refuses, with the attachment key that named no
  project entry, or None when the launch named no attachment at all."""

  kinds: frozenset[str] = frozenset()
  attachment: Optional[str] = None


NOTHING_UNBOUND = UnboundKinds()


@dataclass(frozen=True)
class CredentialSelection:
  """A merged kind-to-instance selection and its host-config provenance."""

  instances: dict[str, Optional[str]]
  layers: dict[str, str]
  unbound: UnboundKinds = NOTHING_UNBOUND


@dataclass(frozen=True)
class _Project:
  credentials: dict[str, Optional[str]]
  bros: dict[str, dict[str, Optional[str]]]


@dataclass(frozen=True)
class _Config:
  defaults: dict[str, Optional[str]]
  projects: dict[str, _Project]
  tools: dict[str, dict[str, Optional[str]]]
  llm: dict[str, str]


def _read() -> _Config:
  path = Path(HOST_CONFIG_FILE)
  if not path.is_file():
    return _Config({}, {}, {}, {})
  try:
    data = json.loads(path.read_text())
  except json.JSONDecodeError as error:
    raise ValueError(f'{path} is not valid json') from error
  if not isinstance(data, dict):
    raise ValueError(f'{path} must hold a json object')
  unknown = sorted(set(data) - {_DEFAULTS_KEY, _PROJECTS_KEY, _TOOLS_KEY, _LLM_KEY})
  if len(unknown) > 0:
    raise ValueError(f'unknown key(s) in {path}: {", ".join(unknown)}')
  defaults = _selection_object(path, _DEFAULTS_KEY, data.get(_DEFAULTS_KEY, {}))
  projects = _projects(path, data.get(_PROJECTS_KEY, {}))
  tools = _tools(path, data.get(_TOOLS_KEY, {}))
  llm = _llm(path, data.get(_LLM_KEY, {}))
  return _Config(defaults, projects, tools, llm)


def llm_presets() -> dict[str, str]:
  """The host's `--llm` preset names, mapped to the values they stand for."""
  config = _read()
  return dict(config.llm)


def project_selection(attachment: Optional[str]) -> CredentialSelection:
  """Merge defaults and the matching project, without a per-bro layer."""
  config = _read()
  project = _matching_project(config, attachment)
  layers = [(DEFAULTS_LAYER, config.defaults)]
  if project is not None:
    layers.append((PROJECT_LAYER, project.credentials))
  return _merged(layers, unbound=_unbound(config, project, attachment))


def launch_selection(attachment: Optional[str], bro: str) -> CredentialSelection:
  """Merge defaults, a matching project, and that project's `bro` layer."""
  if not isinstance(bro, str) or bro == '':
    raise ValueError('bro name must be a non-empty string')
  config = _read()
  project = _matching_project(config, attachment)
  layers = [(DEFAULTS_LAYER, config.defaults)]
  if project is not None:
    layers.append((PROJECT_LAYER, project.credentials))
    bro_credentials = project.bros.get(bro)
    if bro_credentials is not None:
      layers.append((PROJECT_BRO_LAYER, bro_credentials))
  return _merged(layers, unbound=_unbound(config, project, attachment))


def tool_selection(cli_name: Optional[str]) -> CredentialSelection:
  """Merge defaults and the entry for `cli_name`; None selects defaults only."""
  if cli_name is not None and (not isinstance(cli_name, str) or cli_name == ''):
    raise ValueError('CLI name must be a non-empty string')
  config = _read()
  layers = [(DEFAULTS_LAYER, config.defaults)]
  if cli_name is not None:
    tool_credentials = config.tools.get(cli_name)
    if tool_credentials is not None:
      layers.append((TOOL_LAYER, tool_credentials))
  return _merged(layers)


def _matching_project(config: _Config, attachment: Optional[str]) -> Optional[_Project]:
  if attachment is None:
    return None
  return config.projects.get(_attachment_key(attachment))


def _unbound(
  config: _Config, project: Optional[_Project], attachment: Optional[str]
) -> UnboundKinds:
  if project is not None:
    return NOTHING_UNBOUND
  project_kinds = {
    kind
    for candidate in config.projects.values()
    for selection in (candidate.credentials, *candidate.bros.values())
    for kind in selection
  }
  kinds = frozenset(project_kinds - config.defaults.keys())
  if len(kinds) == 0:
    return NOTHING_UNBOUND
  return UnboundKinds(kinds, None if attachment is None else _attachment_key(attachment))


def _merged(
  layers: list[tuple[str, dict[str, Optional[str]]]],
  *,
  unbound: UnboundKinds = NOTHING_UNBOUND,
) -> CredentialSelection:
  instances: dict[str, Optional[str]] = {}
  sources: dict[str, str] = {}
  for layer, selection in layers:
    instances.update(selection)
    sources.update(dict.fromkeys(selection, layer))
  return CredentialSelection(instances, sources, unbound)


def _projects(path: Path, value: object) -> dict[str, _Project]:
  if not isinstance(value, dict):
    raise ValueError(f'{path}: {_PROJECTS_KEY} must be a json object')
  projects: dict[str, _Project] = {}
  for key, entry in value.items():
    attachment = _attachment_key(key)
    if attachment in projects:
      raise ValueError(f'{path}: two project keys name the same attachment {attachment}')
    projects[attachment] = _project(path, key, entry)
  return projects


def _project(path: Path, project: str, value: object) -> _Project:
  where = f'{path}: project {project!r}'
  if not isinstance(value, dict):
    raise ValueError(f'{where} must hold a json object')
  _reject_unknown_fields(value, {_CREDS_KEY, _BROS_KEY}, where)
  selection = _selection_entries(where, value.get(_CREDS_KEY, []))
  bros = value.get(_BROS_KEY, {})
  if not isinstance(bros, dict):
    raise ValueError(f'{where}: {_BROS_KEY} must be a json object')
  parsed_bros: dict[str, dict[str, Optional[str]]] = {}
  for bro, entry in bros.items():
    if bro == '':
      raise ValueError(f'{where}: bro name must not be empty')
    parsed_bros[bro] = _selection_object(path, f'{where}: bro {bro!r}', entry)
  return _Project(selection, parsed_bros)


def _tools(path: Path, value: object) -> dict[str, dict[str, Optional[str]]]:
  if not isinstance(value, dict):
    raise ValueError(f'{path}: {_TOOLS_KEY} must be a json object')
  tools: dict[str, dict[str, Optional[str]]] = {}
  for name, entry in value.items():
    if name == '':
      raise ValueError(f'{path}: tool name must not be empty')
    tools[name] = _selection_object(path, f'tool {name!r}', entry)
  return tools


def _selection_object(path: Path, subject: str, value: object) -> dict[str, Optional[str]]:
  where = f'{path}: {subject}' if not subject.startswith(f'{path}:') else subject
  if not isinstance(value, dict):
    raise ValueError(f'{where} must hold a json object')
  _reject_unknown_fields(value, {_CREDS_KEY}, where)
  return _selection_entries(where, value.get(_CREDS_KEY, []))


def _reject_unknown_fields(value: dict, allowed: set[str], where: str) -> None:
  unknown = sorted(set(value) - allowed)
  if _RETIRED_INSTANCES_KEY in unknown:
    raise ValueError(f'{where}: {_RETIRED_INSTANCES_KEY!r} is retired; use {_CREDS_KEY!r}')
  if len(unknown) > 0:
    raise ValueError(f'{where} has unknown field(s): {", ".join(unknown)}')


def _selection_entries(where: str, entries: object) -> dict[str, Optional[str]]:
  if not isinstance(entries, list):
    raise ValueError(f'{where}: {_CREDS_KEY} must be a list')
  selection: dict[str, Optional[str]] = {}
  for entry in entries:
    kind, instance = _parse_selection(where, entry)
    if kind in selection:
      raise ValueError(f'{where} selects kind {kind!r} twice')
    selection[kind] = instance
  return selection


def _parse_selection(where: str, entry: object) -> tuple[str, Optional[str]]:
  if not isinstance(entry, str):
    raise ValueError(f'{where}: selection {entry!r} must be a string')
  kind, separator, instance = entry.partition('+')
  if separator == '':
    raise ValueError(
      f'{where}: selection {entry!r} names no instance; write '
      f"'{entry}+<instance>', or '{entry}+' for the kind's bare material"
    )
  credentials.parse_name(kind if instance == '' else entry)
  return kind, instance if instance != '' else None


def _attachment_key(attachment: str) -> str:
  """Reduce a project key and attachment to the identity they match on."""
  if not isinstance(attachment, str) or attachment == '':
    raise ValueError('project attachment must be a non-empty string')
  if is_git_url(attachment):
    return normalize_git_url(attachment)
  return str(Path(os.path.expanduser(attachment)).resolve())


def _llm(path: Path, value: object) -> dict[str, str]:
  if not isinstance(value, dict):
    raise ValueError(f'{path}: {_LLM_KEY} must be a json object')
  for name, recipe in value.items():
    if not isinstance(recipe, str) or recipe == '':
      raise ValueError(f'{path}: {_LLM_KEY} preset {name!r} must be a non-empty string')
  return dict(value)
