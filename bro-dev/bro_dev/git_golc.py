#!/usr/bin/env python3
"""show `git gol`-style log with per-commit Claude Code credit usage.

Two passes: pass 1 sums per-model tokens from each commit body's footer (the one
emitted by setup/claude_commit_footer.py); pass 2 renders `git log --graph
--color=always` with a `CREDITS:<full-sha>` sentinel that we substitute
line-by-line with a fixed-width credits column.

Repo-local — wired by setup/setup_repo.sh via `git config --local alias.golc`.
The footer format is owned by setup/claude_commit_footer.py; the small parser
here is a duplicate of usage_report.py's (sharing would require making setup/ a
Python package, which is out of scope).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

_FOOTER_RE = re.compile(
  r'^>\s*created with Claude Code\s+\S+\s+\((.+?);\s*session:\s*(\S+?)\)\s*$',
  re.MULTILINE,
)
_PART_RE = re.compile(r'^(.*?):\s*([\d,]+)$')


def _parse_footer(commit_msg: str) -> dict[str, int] | None:
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  per_model: dict[str, int] = {}
  for chunk in m.group(1).split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    per_model[pm.group(1).strip()] = per_model.get(pm.group(1).strip(), 0) + int(
      pm.group(2).replace(',', '')
    )
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
  width = max(len(c) for c in credits.values())
  color_args = ['--color=always'] if use_color else ['--color=never']
  fmt = 'tformat:%C(auto)%h CREDITS:%H%d %s'
  out = subprocess.check_output(
    [
      'git',
      'log',
      '--graph',
      *color_args,
      '--decorate',
      '--date=format:%Y-%m-%dT%H:%M:%S',
      f'--format={fmt}',
      *git_args,
    ],
    text=True,
  )
  return _SENTINEL_RE.sub(lambda m: f'{credits.get(m.group(1), "—"):<{width}}', out)


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
  proc = subprocess.Popen([pager, '-R'], stdin=subprocess.PIPE, env=env)
  assert proc.stdin is not None
  try:
    proc.stdin.write(text.encode())
    proc.stdin.close()
    proc.wait()
  except BrokenPipeError:
    pass


def main(argv: list[str] | None = None) -> int:
  args = (argv if argv is not None else sys.argv)[1:]
  if len(args) == 0:
    args = ['HEAD']
  _maybe_page(_render(args, use_color=sys.stdout.isatty()))
  return 0


if __name__ == '__main__':
  sys.exit(main())
