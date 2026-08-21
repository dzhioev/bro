import sys
from pathlib import Path


def collect_markdown(classes: list[type], directory_name: str) -> dict[str, Path]:
  found: dict[str, Path] = {}
  for cls in classes:
    module = sys.modules.get(cls.__module__)
    module_file = getattr(module, '__file__', None) if module is not None else None
    if module_file is None:
      continue
    file_path = Path(module_file).resolve()
    if file_path.name != '__init__.py':
      continue
    directory = file_path.parent / directory_name
    if not directory.is_dir():
      continue
    for path in sorted(directory.glob('*.md')):
      found[path.stem] = path
  return found


def _parse_block(lines: list[str], index: int, key: str, source: str) -> tuple[str, int]:
  if index >= len(lines) or len(lines[index].strip()) > 0:
    raise ValueError(
      f'{source}: frontmatter key {key!r} has an empty value; a multi-line value opens with a '
      f'blank line under the key'
    )
  collected: list[str] = []
  index += 1
  while index < len(lines) and len(lines[index].strip()) > 0:
    collected.append(lines[index].strip())
    index += 1
  if len(collected) == 0:
    raise ValueError(f'{source}: frontmatter key {key!r} opens an empty multi-line value')
  return (' '.join(collected), index)


def _parse_fields(lines: list[str], source: str) -> dict[str, str]:
  fields: dict[str, str] = {}
  index = 0
  while index < len(lines):
    line = lines[index]
    index += 1
    if len(line.strip()) == 0:
      continue
    key, separator, value = line.partition(':')
    key = key.strip()
    if len(separator) == 0 or len(key) == 0:
      raise ValueError(f'{source}: frontmatter line is not `key: value`: {line!r}')
    if key in fields:
      raise ValueError(f'{source}: duplicate frontmatter key {key!r}')
    if len(value.strip()) > 0:
      fields[key] = value.strip()
    else:
      fields[key], index = _parse_block(lines, index, key, source)
  return fields


def parse_frontmatter(text: str, source: str) -> tuple[dict[str, str], str]:
  if not text.startswith('---\n'):
    return ({}, text)
  end = text.find('\n---\n', 4)
  if end < 0:
    return ({}, text)
  return (_parse_fields(text[4:end].splitlines(), source), text[end + 5 :])
