"""MCP tools for the dev Bro: file ops, shell, and search.

Each tool wraps a primitive a Claude Code session would normally reach as a
built-in (Read / Write / Edit / Bash / Grep / Glob). Exposing them via MCP
keeps the Bro abstraction declarative — the dev Bro picks the tool set, the
LLM (chat_gpt) reaches them through the same ToolRegistry that wraps
`flow.MCPServer()` or `infra.MCPServer()`. Same shape as `infra/mcp.py`.
"""

import subprocess
from pathlib import Path

from llm.mcp import FunctionTool, InProcessMCPServer, Tool, describe

_MAX_OUTPUT_LINES = 400


def _truncate(s: str, max_lines: int = _MAX_OUTPUT_LINES) -> str:
  lines = s.splitlines(keepends=True)
  if len(lines) <= max_lines:
    return s
  dropped = len(lines) - max_lines
  return f'[...{dropped} earlier lines truncated...]\n' + ''.join(lines[-max_lines:])


def read_file(file_path: str, offset: int = 0, limit: int | None = None) -> str:
  path = Path(file_path)
  with path.open() as f:
    lines = f.readlines()
  if offset > 0:
    lines = lines[offset:]
  if limit is not None:
    lines = lines[:limit]
  start = offset + 1
  return ''.join(f'{i:>5}\t{line}' for i, line in enumerate(lines, start=start))


describe(
  read_file,
  'read a file and return its contents prefixed with 1-based line numbers '
  '(cat -n style). offset is the 0-based line index to start from; limit caps the '
  'number of lines returned.',
)


def write_file(file_path: str, content: str) -> str:
  path = Path(file_path)
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


def bash(command: str, timeout_seconds: int = 120) -> str:
  try:
    proc = subprocess.run(
      ['bash', '-c', command],
      capture_output=True,
      text=True,
      timeout=timeout_seconds,
    )
  except subprocess.TimeoutExpired:
    return f'TIMED OUT after {timeout_seconds}s: command did not complete'
  combined = proc.stdout
  if len(proc.stderr) > 0:
    combined = f'{combined}\n--- stderr ---\n{proc.stderr}' if len(combined) > 0 else proc.stderr
  return f'exit_code: {proc.returncode}\n{_truncate(combined)}'


describe(
  bash,
  'run a bash command, capture stdout and stderr, and return exit code + combined output. '
  'output truncated to last 400 lines if longer. timeout_seconds caps the run (default 120s). '
  'use for shell work (git, sed, awk, find, …) that has no dedicated tool.',
)


def grep(
  pattern: str,
  path: str = '.',
  glob: str | None = None,
  case_insensitive: bool = False,
  head_limit: int | None = None,
) -> str:
  cmd = ['grep', '-rnE']
  if case_insensitive:
    cmd.append('-i')
  if glob is not None:
    cmd.extend(['--include', glob])
  cmd.extend(['--', pattern, path])
  proc = subprocess.run(cmd, capture_output=True, text=True)
  if proc.returncode == 1:
    return 'no matches'
  if proc.returncode != 0:
    return f'grep exit {proc.returncode}: {proc.stderr.strip()}'
  lines = proc.stdout.splitlines()
  if head_limit is not None and len(lines) > head_limit:
    return '\n'.join(lines[:head_limit]) + f'\n[truncated: showing first {head_limit}]'
  return '\n'.join(lines)


describe(
  grep,
  'recursively search for pattern (extended regex) in files under path. glob filters which '
  'files to match (e.g. "*.py"). case_insensitive lowers the comparison; head_limit caps the '
  'number of result lines. backed by GNU grep — gitignore is NOT honored (unlike Claude '
  "Code's ripgrep-backed Grep); pass a glob or a narrower path to scope.",
)


def glob(pattern: str, path: str | None = None) -> str:
  base = Path(path) if path is not None else Path.cwd()
  if not base.is_absolute():
    base = base.resolve()
  matches = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
  if len(matches) == 0:
    return 'no matches'
  return '\n'.join(str(p) for p in matches)


describe(
  glob,
  'list files matching the glob pattern (e.g. "**/*.py", "src/*.ts"). path defaults '
  'to cwd. results sorted by mtime, newest first.',
)


_TOOL_FUNCTIONS = [read_file, write_file, edit_file, bash, grep, glob]
TOOLS: list[Tool] = [FunctionTool(fn) for fn in _TOOL_FUNCTIONS]


class MCPServer(InProcessMCPServer):
  """dev MCP server, in-process.

  no args → all dev tools. With names → only those tools, validated at construction.
  """

  def __init__(self, *tool_names: str):
    if len(tool_names) == 0:
      super().__init__(TOOLS)
      return
    by_name = {t.name: t for t in TOOLS}
    unknown = [n for n in tool_names if n not in by_name]
    if len(unknown) > 0:
      raise ValueError(f'unknown dev tools: {unknown}; available: {sorted(by_name)}')
    super().__init__([by_name[n] for n in tool_names])
