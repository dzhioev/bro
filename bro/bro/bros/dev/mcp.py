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
from base.text_window import DEFAULT_LIMIT, apply_limit, numbered_window
from llm.mcp import FunctionTool, InProcessMCPServer, Tool, describe

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


def read_file(file_path: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
  path = Path(file_path)
  _require_regular_file(path)
  return numbered_window(path.read_text(), offset, limit)


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
    proc = spawn.run(
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
  combined = proc.stdout
  if len(proc.stderr) > 0:
    combined = f'{combined}\n--- stderr ---\n{proc.stderr}' if len(combined) > 0 else proc.stderr
  capped = apply_limit(combined, limit, keep='tail')
  return (
    f'exit_code: {proc.returncode}\n{capped}'
    if len(capped) > 0
    else f'exit_code: {proc.returncode}'
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
  cmd = ['grep', '-rnE', '-D', 'skip']
  if case_insensitive:
    cmd.append('-i')
  if glob is not None:
    cmd.extend(['--include', glob])
  cmd.extend(['--', pattern, path])
  try:
    proc = spawn.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
  except subprocess.TimeoutExpired:
    return (
      f'TIMED OUT after {timeout_seconds}s — killed. Re-run with a larger '
      'timeout_seconds if the search needs more time.'
    )
  if proc.returncode == 1:
    return 'no matches'
  if proc.returncode != 0:
    return f'grep exit {proc.returncode}: {proc.stderr.strip()}'
  return apply_limit(proc.stdout, limit, keep='head')


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
  return apply_limit('\n'.join(str(p) for p in matches), limit, keep='head')


describe(
  glob,
  'list files matching the glob pattern (e.g. "**/*.py", "src/*.ts"). path defaults '
  'to cwd. results sorted by mtime, newest first. limit: see read_reference for the '
  'shared output cap policy.',
)


_TOOL_FUNCTIONS = [read_reference, read_file, write_file, edit_file, bash, grep, glob]
TOOLS: list[Tool] = [FunctionTool(fn) for fn in _TOOL_FUNCTIONS]


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
