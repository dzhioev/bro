"""Parser-free file operations."""

import re
from pathlib import Path
from typing import Any, Optional

from bro.files.registry import installed_type_names, registry
from bro.files.text import sanitize, validate_strings
from bro.files.types import FileType


def _field_names(schema: Any) -> set[str]:
  if isinstance(schema, dict):
    names = set(schema.get('properties', {}))
    for child in schema.values():
      names.update(_field_names(child))
    return names
  if isinstance(schema, list):
    return set().union(*(_field_names(child) for child in schema))
  return set()


def validate_manual(file_type: FileType, text: str) -> None:
  models = [file_type.details, *(view.content for view in file_type.views)]
  fields = set().union(
    *(
      _field_names(model.model_json_schema(by_alias=False, mode='serialization'))
      for model in models
    )
  )
  missing = sorted(
    field for field in fields if re.search(rf'(?<!\w){re.escape(field)}(?!\w)', text) is None
  )
  if missing:
    raise ValueError(f'file type {file_type.name!r} manual omits fields: {", ".join(missing)}')


def read_reference(type: Optional[str] = None) -> str:
  if type is not None:
    file_type = registry.get(type)
    text = file_type.manual.read_text(encoding='utf-8')
    text = sanitize(text).text
    validate_strings(text)
    validate_manual(file_type, text)
    return text
  shared = Path(__file__).with_name('reference.md').read_text(encoding='utf-8')
  names = installed_type_names()
  installed = '\n'.join(f'- {name}' for name in names) if names else '(none)'
  return shared.rstrip() + '\n\n## Installed file types\n\n' + installed + '\n'
