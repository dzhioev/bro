"""MCP tools for the dev Bro: file ops, shell, and search.

Each tool wraps a primitive a Claude Code session would normally reach as a
built-in (Read / Write / Edit / Bash / Grep / Glob). Exposing them via MCP
keeps the Bro abstraction declarative — the dev Bro picks the tool set, the
LLM (chat_gpt) reaches them through the same ToolRegistry that wraps
`flow.MCPServer()` or `infra.MCPServer()`. Same shape as `infra/mcp.py`.

Shared behaviour (output `limit`, skipped-content markers, fat-finger clamp)
lives in sibling `REFERENCE.md` so per-tool `describe()` text stays terse and the
LLM can call `read_reference` once to learn the rules. Add new shared concepts
there, not in each tool's description.
"""

import subprocess
from pathlib import Path
from typing import Optional

from base import spawn
from llm.mcp import FunctionTool, InProcessMCPServer, Tool, describe

# default cap on output lines. Callers can pass `limit=N` to extend, up to
# MAX_LIMIT (silently clamped beyond). The byte budget is `limit * _BYTES_PER_LINE`,
# so the default is ~15 KB and the ceiling is ~300 KB — well under OpenAI's
# 10 MB per-tool-output limit and cheap on input tokens across the agent loop.
DEFAULT_LIMIT = 100
MAX_LIMIT = 2000
_BYTES_PER_LINE = 150

# default wall-clock cap for the shell-out tools (bash, grep). On expiry the whole
# process group is killed and the tool returns a TIMED OUT result; callers can raise
# their `timeout_seconds` to retry. See REFERENCE.md.
DEFAULT_TIMEOUT_SECONDS = 45

_REFERENCE_PATH = Path(__file__).parent / 'REFERENCE.md'


def _require_regular_file(path: Path) -> None:
  # the file-op tools read/write in-process, so they can't be killed by a timeout
  # the way the shell-out tools can. A FIFO or device would hang `open()`/`read_text`
  # forever (or, for `/dev/zero`, run away before any cap could fire), so reject
  # anything that isn't a regular file up front.
  if path.exists() and not path.is_file():
    raise ValueError(
      f'{path} is not a regular file; refusing to open a FIFO, device, socket, or '
      'directory (the read or write could block forever)'
    )


def read_reference() -> str:
  return _REFERENCE_PATH.read_text()


describe(
  read_reference,
  'return the dev tools reference: shared rules for the output `limit`, the '
  'skipped-content markers, the fat-finger clamp, and any other shared '
  'behaviour. call once at the start of a session before relying on the '
  'per-tool descriptions, which intentionally point here for the details.',
)


def _format_size(n: int) -> str:
  if n >= 1_000_000:
    return f'{n / 1_000_000:.1f} MB'
  if n >= 1_000:
    return f'{n / 1_000:.1f} KB'
  return f'{n} B'


def _marker(side: str, lines: int, byte_count: int, *, note: str = '') -> str:
  segs: list[str] = []
  if lines > 0:
    segs.append(f'{lines:,} lines')
  if byte_count > 0:
    segs.append(_format_size(byte_count))
  if len(segs) == 0:
    # nothing was actually skipped — the marker exists only to surface `note`
    # (e.g., a clamp warning). Drop the "skipped X: 0" framing entirely.
    return f'[...{note}...]'
  body = ' / '.join(segs)
  suffix = f' — {note}' if len(note) > 0 else ''
  return f'[...skipped {side}: {body}{suffix}...]'


def _clamp(limit: int) -> tuple[int, str]:
  if limit > MAX_LIMIT:
    return MAX_LIMIT, f'limit {limit:,} clamped to {MAX_LIMIT:,}'
  if limit < 1:
    return 1, f'limit {limit} clamped to 1'
  return limit, ''


def _apply_limit(
  content: str,
  limit: int,
  *,
  keep: str = 'head',
  skipped_before_lines: int = 0,
  skipped_before_bytes: int = 0,
) -> str:
  """cap content to `limit` lines and `limit * _BYTES_PER_LINE` bytes, keeping
  the head or tail. Wraps the kept slice with `[...skipped before/after...]`
  markers reporting what was dropped at each end (including any prior offset
  surfaced via skipped_before_*)."""
  effective, clamp_note = _clamp(limit)
  byte_budget = effective * _BYTES_PER_LINE
  lines = content.splitlines(keepends=True)
  total_lines = len(lines)
  total_bytes = len(content)

  source = list(reversed(lines)) if keep == 'tail' else lines
  kept: list[str] = []
  kept_bytes = 0
  for line in source:
    if len(kept) >= effective or kept_bytes + len(line) > byte_budget:
      break
    kept.append(line)
    kept_bytes += len(line)
  if keep == 'tail':
    kept.reverse()

  dropped_lines = total_lines - len(kept)
  dropped_bytes = total_bytes - kept_bytes

  if keep == 'head':
    before_lines, before_bytes = skipped_before_lines, skipped_before_bytes
    after_lines, after_bytes = dropped_lines, dropped_bytes
    before_note, after_note = '', clamp_note
  else:
    before_lines = skipped_before_lines + dropped_lines
    before_bytes = skipped_before_bytes + dropped_bytes
    after_lines = after_bytes = 0
    before_note, after_note = clamp_note, ''

  pieces: list[str] = []
  if before_lines > 0 or before_bytes > 0 or len(before_note) > 0:
    pieces.append(_marker('before', before_lines, before_bytes, note=before_note))
  body = ''.join(kept).rstrip('\n')
  if len(body) > 0:
    pieces.append(body)
  if after_lines > 0 or after_bytes > 0 or len(after_note) > 0:
    pieces.append(_marker('after', after_lines, after_bytes, note=after_note))
  return '\n'.join(pieces)


def read_file(file_path: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
  path = Path(file_path)
  _require_regular_file(path)
  with path.open() as f:
    all_lines = f.readlines()
  before_count = min(max(offset, 0), len(all_lines))
  before_bytes = sum(len(line) for line in all_lines[:before_count])
  visible = all_lines[before_count:]
  numbered = ''.join(f'{i:>5}\t{line}' for i, line in enumerate(visible, start=before_count + 1))
  return _apply_limit(
    numbered,
    limit,
    keep='head',
    skipped_before_lines=before_count,
    skipped_before_bytes=before_bytes,
  )


describe(
  read_file,
  'read a file and return its contents prefixed with 1-based line numbers '
  '(cat -n style). offset is the 0-based line index to start from. '
  'limit: see read_reference for the shared output cap policy.',
)


def write_file(file_path: str, content: str) -> str:
  path = Path(file_path)
  _require_regular_file(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content)
  return f'wrote {len(content)} chars to {file_path}'


describe(
  write_file,
  'overwrite the file at file_path with content. parent directories are created if '
  'missing. use for new files or full rewrites; use edit_file for incremental changes.',
)


def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
  path = Path(file_path)
  _require_regular_file(path)
  text = path.read_text()
  count = text.count(old_string)
  if count == 0:
    raise ValueError(f'old_string not found in {file_path!r}')
  if count > 1 and not replace_all:
    raise ValueError(
      f'old_string occurs {count} times in {file_path!r}; pass replace_all=True or '
      'expand old_string with more context to make it unique'
    )
  path.write_text(text.replace(old_string, new_string))
  return f'replaced {count} occurrence(s) of old_string in {file_path}'


describe(
  edit_file,
  'replace old_string with new_string in the file. by default requires old_string '
  'to be unique (errors otherwise). with replace_all=True, replaces every occurrence. '
  'errors if old_string is not found.',
)


def bash(
  command: str, limit: int = DEFAULT_LIMIT, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
) -> str:
  try:
    process = spawn.run(
      ['bash', '-c', command],
      capture_output=True,
      text=True,
      timeout=timeout_seconds,
    )
  except subprocess.TimeoutExpired:
    return (
      f'TIMED OUT after {timeout_seconds}s — killed. Re-run with a larger '
      'timeout_seconds if the command needs more time.'
    )
  combined = process.stdout
  if len(process.stderr) > 0:
    combined = (
      f'{combined}\n--- stderr ---\n{process.stderr}' if len(combined) > 0 else process.stderr
    )
  capped = _apply_limit(combined, limit, keep='tail')
  return (
    f'exit_code: {process.returncode}\n{capped}'
    if len(capped) > 0
    else f'exit_code: {process.returncode}'
  )


describe(
  bash,
  'run a bash command, capture stdout and stderr, and return exit code + combined '
  'output. bash keeps the tail (shell diagnostics live at the end). limit and '
  'timeout_seconds: see read_reference for the shared output cap and timeout '
  'policies. use for shell work (git, sed, awk, find, …) that has no dedicated tool.',
)


def grep(
  pattern: str,
  path: str = '.',
  glob: Optional[str] = None,
  case_insensitive: bool = False,
  limit: int = DEFAULT_LIMIT,
  timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
  # -D skip: never read a device, FIFO, or socket (recursion or named directly), so
  # the tool can't block forever on a pipe — the timeout is only a huge-tree backstop.
  command = ['grep', '-rnE', '-D', 'skip']
  if case_insensitive:
    command.append('-i')
  if glob is not None:
    command.extend(['--include', glob])
  command.extend(['--', pattern, path])
  try:
    process = spawn.run(command, capture_output=True, text=True, timeout=timeout_seconds)
  except subprocess.TimeoutExpired:
    return (
      f'TIMED OUT after {timeout_seconds}s — killed. Re-run with a larger '
      'timeout_seconds if the search needs more time.'
    )
  if process.returncode == 1:
    return 'no matches'
  if process.returncode != 0:
    return f'grep exit {process.returncode}: {process.stderr.strip()}'
  return _apply_limit(process.stdout, limit, keep='head')


describe(
  grep,
  'recursively search for pattern (extended regex) in files under path. glob filters '
  'which files to match (e.g. "*.py"). case_insensitive lowers the comparison. '
  'limit and timeout_seconds: see read_reference for the shared output cap and '
  'timeout policies. backed by GNU grep — gitignore is NOT honored (unlike Claude '
  "Code's ripgrep-backed Grep); pass a glob or narrower path to scope.",
)


def glob(pattern: str, path: Optional[str] = None, limit: int = DEFAULT_LIMIT) -> str:
  base = Path(path) if path is not None else Path.cwd()
  if not base.is_absolute():
    base = base.resolve()
  matches = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
  if len(matches) == 0:
    return 'no matches'
  return _apply_limit('\n'.join(str(p) for p in matches), limit, keep='head')


describe(
  glob,
  'list files matching the glob pattern (e.g. "**/*.py", "src/*.ts"). path defaults '
  'to cwd. results sorted by mtime, newest first. limit: see read_reference for the '
  'shared output cap policy.',
)


_TOOL_FUNCTIONS = [read_reference, read_file, write_file, edit_file, bash, grep, glob]
TOOLS: list[Tool] = [FunctionTool(function) for function in _TOOL_FUNCTIONS]


class MCPServer(InProcessMCPServer):
  """dev MCP server, in-process.

  no args → all dev tools. With names → only those tools, validated at construction.
  """

  def __init__(self, *tool_names: str):
    if len(tool_names) == 0:
      super().__init__('dev', TOOLS)
      return
    by_name = {t.name: t for t in TOOLS}
    unknown = [n for n in tool_names if n not in by_name]
    if len(unknown) > 0:
      raise ValueError(f'unknown dev tools: {unknown}; available: {sorted(by_name)}')
    super().__init__('dev', [by_name[n] for n in tool_names])
