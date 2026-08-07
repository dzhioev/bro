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


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
  if not text.startswith('---\n'):
    return ({}, text)
  end = text.find('\n---\n', 4)
  if end < 0:
    return ({}, text)
  frontmatter: dict[str, str] = {}
  for line in text[4:end].splitlines():
    if ':' not in line:
      continue
    key, _, value = line.partition(':')
    frontmatter[key.strip()] = value.strip()
  return (frontmatter, text[end + 5 :])
