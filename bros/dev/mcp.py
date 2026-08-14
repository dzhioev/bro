"""MCP tools for the dev Bro: file ops, shell, search, and background jobs.

Each tool wraps a primitive a Claude Code session would normally reach as a
built-in (Read / Write / Edit / Bash / Grep / Glob / run_in_background +
TaskOutput / TaskStop). Exposing them via MCP keeps the Bro abstraction
declarative: the dev Bro picks the toolset and the LLM reaches it through the
same `ToolRegistry` used by every MCP provider.

Shared behaviour (output `limit`, skipped-content markers, fat-finger clamp,
the background-job model) lives in sibling `REFERENCE.md` so per-tool
`describe()` text stays terse and the LLM can call `read_reference` once to
learn the rules. Add new shared concepts there, not in each tool's description.
"""

import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Optional

from bro.base import spawn
from bro.base.offload import off_loop
from bro.base.text_window import DEFAULT_LIMIT, apply_limit, numbered_window
from bro.llm.mcp import Context, Toolset
from bros.dev import jobs

# default wall-clock cap for the shell-out tools (bash, grep). On expiry the whole
# process group is killed and the tool returns a TIMED OUT result; callers can raise
# their `timeout_seconds` to retry. See REFERENCE.md.
DEFAULT_TIMEOUT_SECONDS = 45

# default window a watch blocks for when the job has no pending output — short so
# an unparameterized call can't stall an interactive surface; iterative watchers
# pass an explicitly large value. See REFERENCE.md.
DEFAULT_WAIT_SECONDS = 10

_REFERENCE_PATH = Path(__file__).parent / 'REFERENCE.md'

toolset = Toolset('dev', state=jobs.Registry, close=jobs.Registry.close)


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
  'run a bash command, capture stdout and stderr, and return exit code + combined '
  'output. bash keeps the tail (shell diagnostics live at the end). '
  '{{iff #tools contains read_reference}}limit and timeout_seconds: see read_reference '
  'for the shared output cap and timeout policies.{{else}}output beyond `limit` is '
  'trimmed to the tail with a skipped-content marker; on timeout_seconds expiry the '
  'whole process group is killed and the tool returns TIMED OUT.{{end}} '
  'use for shell work (git, sed, awk, find, …) that has no dedicated tool.'
)
async def bash(
  command: str, limit: int = DEFAULT_LIMIT, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
) -> str:
  try:
    process = await spawn.run_async(['bash', '-c', command], timeout=timeout_seconds)
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
  capped = apply_limit(combined, limit, keep='tail')
  return (
    f'exit_code: {process.returncode}\n{capped}'
    if len(capped) > 0
    else f'exit_code: {process.returncode}'
  )


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


@toolset.tool(
  'start command as a background job (bash -c, stdout+stderr merged into one '
  'chronological stream, spooled continuously so the process never blocks on unread '
  'output) and return its job id immediately. No timeout — the job runs until it '
  'exits or is killed. Read output with watch; terminate with kill. See '
  'read_reference for the full background-job rules.'
)
def job(context: Context[jobs.Registry], command: str) -> str:
  started = context.state.start(command)
  return f'started {started.id} (pid {started.process.pid})'


@toolset.tool(
  'read new output from a background job, oldest-first from the per-job cursor; '
  'every return opens with a state line (running / exited (code N)). Blocks up to '
  'wait_seconds when nothing is pending (0 = non-blocking poll); tail=true waits '
  'for exit instead and returns the last limit lines. Exclusive per job — a '
  'concurrent watch on the same job fails immediately. limit: shared output cap. '
  'See read_reference for the full background-job rules.'
)
async def watch(
  context: Context[jobs.Registry],
  job_id: str,
  wait_seconds: float = DEFAULT_WAIT_SECONDS,
  limit: int = DEFAULT_LIMIT,
  tail: bool = False,
) -> str:
  target = context.state.get(job_id)
  # the wait blocks; run it off-loop so concurrent tool calls — other jobs'
  # watches included — stay serviceable. an interrupted watch is woken so the
  # abandoned thread drops its claim on the job instead of holding it for the
  # rest of the window.
  woken = threading.Event()
  try:
    return await off_loop(
      target.watch, wait_seconds=wait_seconds, limit=limit, tail=tail, woken=woken
    )
  except asyncio.CancelledError:
    target.wake(woken)
    raise


@toolset.tool(
  'terminate a background job: SIGTERM its whole process group, escalating to '
  'SIGKILL after a short grace. The record and spooled output stay readable via '
  'watch for a final collect. Reports when the job had already exited.'
)
async def kill(context: Context[jobs.Registry], job_id: str) -> str:
  target = context.state.get(job_id)
  return await off_loop(target.kill)
