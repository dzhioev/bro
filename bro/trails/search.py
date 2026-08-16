import re
from typing import Optional

from bro.base.ansi import Colors


def grep_lines(
  name: str,
  text: str,
  regex: re.Pattern[str],
  colors: Colors,
  before: int = 0,
  after: int = 0,
) -> list[str]:
  def highlight(match: re.Match[str]) -> str:
    return f'{colors.bold}{colors.red}{match.group(0)}{colors.reset}'

  lines = text.splitlines()
  match_indexes = {index for index, line in enumerate(lines) if regex.search(line) is not None}
  shown: set[int] = set()
  for index in match_indexes:
    shown.update(range(max(index - before, 0), min(index + after + 1, len(lines))))

  has_context = before > 0 or after > 0
  output: list[str] = []
  previous: Optional[int] = None
  for index in sorted(shown):
    if has_context and previous is not None and index > previous + 1:
      output.append(f'{colors.cyan}--{colors.reset}')
    previous = index
    line = lines[index]
    if index in match_indexes:
      separator = f'{colors.cyan}:{colors.reset}'
      if colors.enabled:
        line = regex.sub(highlight, line)
    else:
      separator = f'{colors.cyan}-{colors.reset}'
    output.append(
      f'{colors.magenta}{name}{colors.reset}{separator}'
      f'{colors.green}{index + 1}{colors.reset}{separator}{line}'
    )
  return output
