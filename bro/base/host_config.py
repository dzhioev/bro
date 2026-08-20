"""the host's launch policy (`~/.bro.json`).

one host serves several projects, and a credential kind may have more than one
instance stored for it; this file records which instance each project reads,
beside the host's own `--llm` preset names:

    {
      "projects": {
        "/home/foo/projects/api": {
          "instances": ["brog+github", "github+acme"]
        },
        "/home/foo/projects/site": {
          "instances": ["brog+"]
        },
        "https://github.com/foo/api.git": {
          "instances": ["brog+github", "github+acme"]
        }
      },
      "llm": {"sharp": "openai:sol:max"}
    }

a project key is the attachment a session names it by: the filesystem path of
the operated repo's root (`~` and symlinks resolved before matching), or a git
URL (normalized before matching), so a repository attached both ways carries an
entry per identity. its value is that project's policy object, carrying
`instances`: the `kind+instance` names the project reads, naming each kind at
most once. the `+` is always written — `kind+` states that the project reads the
kind's own registry entry, which the registry must then give sources of its own.

the file is optional: a host holding one instance per kind needs none. a host
that does declare project entries reads the kinds they name **per project**, so
a launch whose attachment no entry names cannot resolve those kinds at all
(`project_scoped_kinds`) — the kind's own registry entry is one project's
instance, and handing it to another project is a cross-project credential leak.

`llm` maps a preset name to the `--llm` value it stands for, host-wide rather
than per project — it is the operator's own shorthand, which every project they
launch from answers to.

reading is project-agnostic: the caller names the project.
"""

import json
import os
from pathlib import Path
from typing import Optional

from bro.base import configs, credentials
from bro.base.git_url import is_git_url, normalize_git_url

# module-level so tests can point it at a fixture path; read at call time.
HOST_CONFIG_FILE = configs.DEFAULT_HOST_CONFIG

_PROJECTS_KEY = 'projects'
_INSTANCES_KEY = 'instances'
_LLM_KEY = 'llm'


def _read() -> tuple[Path, dict]:
  """the host config's contents, validated whole on every read so a typo in a
  section the caller isn't asking about still surfaces at the next launch.
  Empty when the file does not exist."""
  path = Path(HOST_CONFIG_FILE)
  if not path.is_file():
    return path, {}
  data = json.loads(path.read_text())
  if not isinstance(data, dict):
    raise ValueError(f'{path} must hold a json object')
  unknown = sorted(set(data) - {_PROJECTS_KEY, _LLM_KEY})
  if len(unknown) > 0:
    raise ValueError(f'unknown key(s) in {path}: {", ".join(unknown)}')
  return path, data


def llm_presets() -> dict[str, str]:
  """the host's `--llm` preset names, each mapped to the value it stands for.
  empty when the file declares none (or does not exist)."""
  path, data = _read()
  presets = data.get(_LLM_KEY, {})
  if not isinstance(presets, dict):
    raise ValueError(f'{path}: {_LLM_KEY} must be a json object')
  for name, value in presets.items():
    if not isinstance(value, str) or value == '':
      raise ValueError(f'{path}: {_LLM_KEY} preset {name!r} must be a non-empty string')
  return presets


def project_instances(attachment: str) -> Optional[dict[str, Optional[str]]]:
  """the instance selection the project attached as `attachment` reads on this
  host: kind → the instance backing it, or None where the selection names the
  kind's own entry. None when no entry names the attachment (or the file does
  not exist), which is what `project_scoped_kinds` then withholds."""
  return _selections().get(_attachment_key(attachment))


def project_scoped_kinds() -> frozenset[str]:
  """every credential kind some project entry selects an instance for — the
  kinds this host reads per project rather than host-wide."""
  return frozenset(kind for selection in _selections().values() for kind in selection)


def _selections() -> dict[str, dict[str, Optional[str]]]:
  path, data = _read()
  projects = data.get(_PROJECTS_KEY, {})
  if not isinstance(projects, dict):
    raise ValueError(f'{path}: {_PROJECTS_KEY} must be a json object')
  selections: dict[str, dict[str, Optional[str]]] = {}
  for key, value in projects.items():
    attachment = _attachment_key(key)
    if attachment in selections:
      raise ValueError(f'{path}: two project keys name the same attachment {attachment}')
    selections[attachment] = _project_selection(path, key, value)
  return selections


def _attachment_key(attachment: str) -> str:
  """a project key and an attachment reduced to the identity they match on."""
  if is_git_url(attachment):
    return normalize_git_url(attachment)
  return str(Path(os.path.expanduser(attachment)).resolve())


def _project_selection(path: Path, project: str, value: object) -> dict[str, Optional[str]]:
  if not isinstance(value, dict):
    raise ValueError(f'{path}: project {project!r} must hold a json object')
  unknown = sorted(set(value) - {_INSTANCES_KEY})
  if len(unknown) > 0:
    raise ValueError(f'{path}: project {project!r} has unknown field(s): {", ".join(unknown)}')
  entries = value.get(_INSTANCES_KEY, [])
  if not isinstance(entries, list):
    raise ValueError(f'{path}: project {project!r}: {_INSTANCES_KEY} must be a list')
  selection: dict[str, Optional[str]] = {}
  for entry in entries:
    kind, instance = _parse_selection(path, project, entry)
    if kind in selection:
      raise ValueError(f'{path}: project {project!r} selects kind {kind!r} twice')
    selection[kind] = instance
  return selection


def _parse_selection(path: Path, project: str, entry: object) -> tuple[str, Optional[str]]:
  """one `kind+instance` (or `kind+`) selection, split and validated."""
  where = f'{path}: project {project!r}'
  if not isinstance(entry, str):
    raise ValueError(f'{where}: selection {entry!r} must be a string')
  kind, separator, instance = entry.partition('+')
  if separator == '':
    raise ValueError(
      f'{where}: selection {entry!r} names no instance; write '
      f"'{entry}+<instance>', or '{entry}+' for the kind's own entry"
    )
  credentials.parse_name(kind if instance == '' else entry)
  return kind, instance if instance != '' else None
