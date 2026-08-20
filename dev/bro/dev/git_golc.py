#!/usr/bin/env python3
"""show `git gol`-style log with per-commit Claude Code credit usage.

Two passes: pass 1 reads each commit body's footer (the one emitted by
bro.workflow.commit_footer) for its per-model output tokens; pass 2 renders
`git log --graph --color=always` with a `CREDITS:<full-sha>` sentinel that we
substitute line-by-line with a fixed-width credits column.

The footer carries four token classes; the credits column shows **output** only —
the clearest per-commit glance at generated work (the cheap, volume-dominating
cache-read lives in the full footer, not here). Commits with no parseable footer
render `—`.

Repo-local — wired by `bro.dev.install` via `git config --local alias.golc`.
The footer format is owned by `bro.llm.usage`; the small parser here is a
deliberate dependency-free duplicate. The alias resolves its console command
from PATH, which is the pinned runtime inside a managed session.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Optional

# footer parser (format owned by bro.llm.usage); the agents part is a
# surface identity list — 'Claude Code <version>' or a bro identity like 'bro//dev'
_FOOTER_RE = re.compile(
  r'^>\s*created with\s+(?P<agents>.+?)\s*\|\s*(?P<tokens>.+?)\s*$',
  re.MULTILINE,
)
_PART_RE = re.compile(
  r'^(?P<model>.*?):\s*'
  r'↑\s*\(\s*(?P<input>[\d\']+)\s+(?P<cache_write>[\d\']+)\s+(?P<cache_read>[\d\']+)\s*\)\s*'
  r'↓\s*(?P<output>[\d\']+)$'
)


def _parse_footer(commit_msg: str) -> Optional[dict[str, int]]:
  """returns {model family: output tokens} for the credits column, or None."""
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  per_model: dict[str, int] = {}
  for chunk in m.group('tokens').split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    model = pm.group('model').strip()
    per_model[model] = per_model.get(model, 0) + int(pm.group('output').replace("'", ''))
  return per_model if len(per_model) > 0 else None


def round_credits(n: int) -> str:
  """1234 -> '1.2K', 18432 -> '18K', 500000 -> '500K', 1234567 -> '1.2M'.

  Integer when the scaled value is >= 5; otherwise one decimal. Promotes one
  unit up when rounding would produce '1000K' / '1000M'.
  """
  units = (('B', 1_000_000_000), ('M', 1_000_000), ('K', 1_000))
  for i, (unit, divisor) in enumerate(units):
    if n >= divisor:
      scaled = n / divisor
      if scaled >= 5:
        rounded = round(scaled)
        if rounded >= 1000 and i > 0:
          next_unit, next_divisor = units[i - 1]
          return f'{n / next_divisor:.1f}{next_unit}'
        return f'{rounded}{unit}'
      return f'{scaled:.1f}{unit}'
  return str(n)


def _model_initial(label: str) -> str:
  s = label.strip()
  return s[0].upper() if len(s) > 0 else '?'


def _format_credits(per_model: dict[str, int]) -> str:
  return ' '.join(
    f'{_model_initial(m)}:{round_credits(per_model[m])}' for m in sorted(per_model.keys())
  )


def _collect_credits(git_args: list[str]) -> dict[str, str]:
  """sha -> credits text (output per model, or `—` when no footer)."""
  out = subprocess.check_output(['git', 'log', '--format=%H%x1f%B%x1e', *git_args], text=True)
  credits: dict[str, str] = {}
  for record in out.split('\x1e'):
    record = record.strip()
    if len(record) == 0:
      continue
    sha, _, body = record.partition('\x1f')
    parsed = _parse_footer(body)
    credits[sha] = _format_credits(parsed) if parsed is not None else '—'
  return credits


_ANSI = r'(?:\x1b\[[0-9;]*m)?'
_SENTINEL_RE = re.compile(rf'CREDITS:{_ANSI}([0-9a-f]{{40}}){_ANSI}')


def _render(git_args: list[str], use_color: bool) -> str:
  credits = _collect_credits(git_args)
  if len(credits) == 0:
    return ''
  width = max(len(text) for text in credits.values())
  color_args = ['--color=always'] if use_color else ['--color=never']
  log_format = 'tformat:%C(auto)%h CREDITS:%H%d %s'
  out = subprocess.check_output(
    [
      'git',
      'log',
      '--graph',
      *color_args,
      '--decorate',
      '--date=format:%Y-%m-%dT%H:%M:%S',
      f'--format={log_format}',
      *git_args,
    ],
    text=True,
  )

  def _replace(m: re.Match[str]) -> str:
    text = credits.get(m.group(1), '—')
    return f'{text:<{width}}'

  return _SENTINEL_RE.sub(_replace, out)


def _maybe_page(text: str) -> None:
  if not sys.stdout.isatty():
    sys.stdout.write(text)
    return
  pager = shutil.which('less')
  if pager is None:
    sys.stdout.write(text)
    return
  env = os.environ.copy()
  env.setdefault('LESS', 'FRX')
  process = subprocess.Popen([pager, '-R'], stdin=subprocess.PIPE, env=env)
  assert process.stdin is not None
  try:
    process.stdin.write(text.encode())
    process.stdin.close()
    process.wait()
  except BrokenPipeError:
    pass


def main(argv: list[str]) -> Optional[int]:
  args = argv[1:]
  if len(args) == 0:
    args = ['HEAD']
  _maybe_page(_render(args, use_color=sys.stdout.isatty()))
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
