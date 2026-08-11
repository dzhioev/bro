"""the host's per-project launch policy (`~/.bro.json`).

one host serves several projects, and a credential kind may have more than one
instance stored for it (`bro/setup/CLAUDE.md`, "Configuration"). this file
records which instance each project reads:

    {"projects": {"~/projects/api": "brog+github",
                  "~/projects/site": ["github+acme", "brog+"]}}

a project key is the filesystem path of the operated repo's root (`~` and
symlinks resolved before matching); its value is one selection or a list of
them, each naming a kind and the instance backing it there. the `+` is written
even when no instance follows it: `brog+` states that the project reads the
kind's own registry entry, and fails where the registry gives the kind none of
its own. the file is optional — a host holding one instance per kind needs none,
and a project without an entry reads each kind's own default.

reading is project-agnostic: the caller names the project.
"""

import json
import os
from pathlib import Path
from typing import Optional

from bro.base import configs, credentials

# module-level so tests can point it at a fixture path; read at call time.
HOST_CONFIG_FILE = configs.DEFAULT_HOST_CONFIG

_PROJECTS_KEY = 'projects'


def project_instances(project: Path) -> dict[str, Optional[str]]:
  """the instance selection `project` reads on this host: kind → the instance
  backing it, or None where the selection names the kind's own entry. empty when
  the file has no entry for the project (or does not exist).

  the whole file is validated on every read, so a typo in another project's
  entry surfaces at the next launch rather than lying in wait."""
  path = Path(HOST_CONFIG_FILE)
  if not path.is_file():
    return {}
  data = json.loads(path.read_text())
  if not isinstance(data, dict):
    raise ValueError(f'{path} must hold a json object')
  unknown = sorted(set(data) - {_PROJECTS_KEY})
  if len(unknown) > 0:
    raise ValueError(f'unknown key(s) in {path}: {", ".join(unknown)}')
  projects = data.get(_PROJECTS_KEY, {})
  if not isinstance(projects, dict):
    raise ValueError(f'{path}: {_PROJECTS_KEY} must be a json object')
  selections = {
    _resolve_path(key): _project_selection(path, key, value) for key, value in projects.items()
  }
  return selections.get(_resolve_path(str(project)), {})


def _resolve_path(path: str) -> Path:
  return Path(os.path.expanduser(path)).resolve()


def _project_selection(path: Path, project: str, value: object) -> dict[str, Optional[str]]:
  entries = [value] if isinstance(value, str) else value
  if not isinstance(entries, list):
    raise ValueError(f'{path}: project {project!r} must hold a selection or a list of them')
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
