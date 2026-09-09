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
