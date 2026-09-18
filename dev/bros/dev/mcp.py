"""MCP file and search tools for the dev Bro.

Each tool wraps a file or search primitive a Claude Code session would normally
reach as a built-in. Exposing them via MCP keeps the Bro abstraction
declarative: the dev Bro picks the toolset and the LLM reaches it through the
same `ToolRegistry` used by every MCP provider.

Shared output-limit and marker behaviour lives in sibling `REFERENCE.md` so
per-tool descriptions stay terse. Add new shared concepts there, not in each
tool's description.
"""

import subprocess
from pathlib import Path
from typing import Optional

from bro.base import spawn
from bro.base.text_window import DEFAULT_LIMIT, apply_limit, numbered_window
from bro.mcp import Toolset

# default wall-clock cap for grep. On expiry its whole process group is killed;
# callers can raise `timeout_seconds` to retry. See REFERENCE.md.
DEFAULT_TIMEOUT_SECONDS = 45

_REFERENCE_PATH = Path(__file__).parent / 'REFERENCE.md'

toolset = Toolset('dev')


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


@toolset.tool(
  'return the dev tools reference: shared rules for the output `limit`, the '
  'skipped-content markers, the fat-finger clamp, and any other shared '
  'behaviour. call once at the start of a session before relying on the '
  'per-tool descriptions, which intentionally point here for the details.'
)
def read_reference() -> str:
  return _REFERENCE_PATH.read_text()


@toolset.tool(
  'read a file and return its contents prefixed with 1-based line numbers '
  '(cat -n style). offset is the 0-based line index to start from. '
  'limit: see read_reference for the shared output cap policy.'
)
def read_file(file_path: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
  path = Path(file_path)
  _require_regular_file(path)
  return numbered_window(path.read_text(), offset, limit)


@toolset.tool(
  'overwrite the file at file_path with content. parent directories are created if '
  'missing. use for new files or full rewrites; use edit_file for incremental changes.'
)
def write_file(file_path: str, content: str) -> str:
  path = Path(file_path)
  _require_regular_file(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content)
  return f'wrote {len(content)} chars to {file_path}'


@toolset.tool(
  'replace old_string with new_string in the file. by default requires old_string '
  'to be unique (errors otherwise). with replace_all=True, replaces every occurrence. '
  'errors if old_string is not found.'
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


@toolset.tool(
  'recursively search for pattern (extended regex) in files under path. glob filters '
  'which files to match (e.g. "*.py"). case_insensitive lowers the comparison. '
  'limit and timeout_seconds: see read_reference for the shared output cap and '
  'timeout policies. backed by GNU grep — gitignore is NOT honored; pass a glob or '
  'narrower path to scope.'
)
async def grep(
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
    process = await spawn.run_async(command, timeout=timeout_seconds)
  except subprocess.TimeoutExpired:
    return (
      f'TIMED OUT after {timeout_seconds}s — killed. Re-run with a larger '
      'timeout_seconds if the search needs more time.'
    )
  if process.returncode == 1:
    return 'no matches'
  if process.returncode != 0:
    return f'grep exit {process.returncode}: {process.stderr.strip()}'
  return apply_limit(process.stdout, limit, keep='head')


@toolset.tool(
  'list files matching the glob pattern (e.g. "**/*.py", "src/*.ts"). path defaults '
  'to cwd. results sorted by mtime, newest first. limit: see read_reference for the '
  'shared output cap policy.'
)
def glob(pattern: str, path: Optional[str] = None, limit: int = DEFAULT_LIMIT) -> str:
  base = Path(path) if path is not None else Path.cwd()
  if not base.is_absolute():
    base = base.resolve()
  matches = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
  if len(matches) == 0:
    return 'no matches'
  return apply_limit('\n'.join(str(p) for p in matches), limit, keep='head')
