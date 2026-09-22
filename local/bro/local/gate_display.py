"""How the test gate reports: plain lines for a pipe, a live table for a terminal.

The gate drives a display through stage, step, and line events;
only the display decides what a reader sees of them.
"""

import contextlib
import re
import subprocess
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Optional, Protocol, TextIO

import rich.console
import rich.live
import rich.progress_bar
import rich.spinner
import rich.table
import rich.text

from bro.base.ansi import Colors

# pytest closes each progress line on the share of the run it has reported
PROGRESS = re.compile(r'\[\s*(\d+)%\]')


def duration(seconds: float) -> str:
  whole = round(seconds)
  if whole < 60:
    return f'{whole}s'
  minutes, rest = divmod(whole, 60)
  return f'{minutes}m{rest:02d}s'


class Display(Protocol):
  color: bool

  @property
  def step_text(self) -> Optional[str]: ...

  def running(self) -> contextlib.AbstractContextManager[None]: ...

  def stage_started(self, name: str, scope: str) -> None: ...

  def step(self, text: str) -> None: ...

  def line(self, text: str) -> None: ...

  def stage_ended(self, verdict: str, seconds: float) -> None: ...

  def skipped(self, name: str, reason: str) -> None: ...

  def failure(self, stage: str, step: str, error: subprocess.CalledProcessError) -> None: ...

  def verdict(self, verdicts: Sequence[tuple[str, str]]) -> None: ...


def _failure_header(stage: str, step: str, error: subprocess.CalledProcessError) -> str:
  return f'=== {stage}: {step} exited with {error.returncode} ==='


def _verdict_line(verdicts: Sequence[tuple[str, str]]) -> str:
  return 'gate: ' + ' | '.join(f'{name} {verdict}' for name, verdict in verdicts)


class Plain:
  """a line per stage start and end;
  a command's output only when verbose, otherwise only once its command failed."""

  def __init__(self, stream: TextIO, *, color: bool, verbose: bool) -> None:
    self._stream = stream
    self._colors = Colors(color)
    self._verbose = verbose
    self.color = color
    self._stage: Optional[str] = None
    self.step_text: Optional[str] = None

  @contextlib.contextmanager
  def running(self) -> Iterator[None]:
    yield

  def stage_started(self, name: str, scope: str) -> None:
    self._stage = name
    self._stream.write(f'{name}: {scope}\n')

  def step(self, text: str) -> None:
    self.step_text = text
    if self._verbose:
      self._stream.write(f'{self._stage}: {text}\n')

  def line(self, text: str) -> None:
    if self._verbose:
      self._stream.write(text)

  def stage_ended(self, verdict: str, seconds: float) -> None:
    colors = self._colors
    shade = colors.green if verdict == 'ok' else colors.red
    self._stream.write(f'{self._stage}: {shade}{verdict}{colors.reset} in {duration(seconds)}\n')
    self._stage = None
    self.step_text = None

  def skipped(self, name: str, reason: str) -> None:
    self._stream.write(f'skipping the {name} stage ({reason})\n')

  def failure(self, stage: str, step: str, error: subprocess.CalledProcessError) -> None:
    self._stream.write(f'\n{_failure_header(stage, step, error)}\n')
    if not self._verbose:
      self._stream.write(error.output)

  def verdict(self, verdicts: Sequence[tuple[str, str]]) -> None:
    self._stream.write(f'\n{_verdict_line(verdicts)}\n')


@dataclass
class _Row:
  name: str
  scope: str
  step: str = ''
  verdict: Optional[str] = None
  started: float = field(default_factory=time.monotonic)
  seconds: float = 0
  percent: Optional[int] = None
  spinner: rich.spinner.Spinner = field(default_factory=lambda: rich.spinner.Spinner('dots'))

  def status(self) -> rich.console.RenderableType:
    if self.verdict is None:
      return self.spinner
    style = {'ok': 'green', 'FAILED': 'bold red'}.get(self.verdict, 'dim')
    return rich.text.Text(self.verdict, style=style)

  def elapsed(self) -> str:
    if self.verdict == 'skipped':
      return ''
    seconds = time.monotonic() - self.started if self.verdict is None else self.seconds
    return duration(seconds)

  def progress(self) -> rich.console.RenderableType:
    if self.percent is None:
      return ''
    return rich.progress_bar.ProgressBar(total=100, completed=self.percent, width=24)


class Live:
  """a table redrawn in place: one row per stage with its status, step, elapsed time,
  and a bar over the progress pytest reports."""

  def __init__(self, console: rich.console.Console, *, color: bool) -> None:
    self._console = console
    self.color = color
    self._rows: list[_Row] = []
    self._live = rich.live.Live(self, console=console, refresh_per_second=10)

  @property
  def step_text(self) -> Optional[str]:
    return self._rows[-1].step if len(self._rows) > 0 else None

  @contextlib.contextmanager
  def running(self) -> Iterator[None]:
    with self._live:
      yield

  def _current(self) -> _Row:
    row = self._rows[-1]
    assert row.verdict is None, f'no stage is running; the last one ended {row.verdict}'
    return row

  def stage_started(self, name: str, scope: str) -> None:
    self._rows.append(_Row(name, scope))

  def step(self, text: str) -> None:
    row = self._current()
    row.step = text
    row.percent = None

  def line(self, text: str) -> None:
    match = PROGRESS.search(text)
    if match is not None:
      self._current().percent = int(match.group(1))

  def stage_ended(self, verdict: str, seconds: float) -> None:
    row = self._current()
    row.verdict = verdict
    row.seconds = seconds
    row.percent = None

  def skipped(self, name: str, reason: str) -> None:
    self._rows.append(_Row(name, reason, verdict='skipped'))

  def failure(self, stage: str, step: str, error: subprocess.CalledProcessError) -> None:
    self._console.print(f'\n{_failure_header(stage, step, error)}', markup=False, highlight=False)
    # the output carries the command's own color escapes, which rich's rendering strips
    self._console.file.write(error.output)

  def verdict(self, verdicts: Sequence[tuple[str, str]]) -> None:
    self._console.print(f'\n{_verdict_line(verdicts)}', markup=False, highlight=False)

  def __rich__(self) -> rich.table.Table:
    table = rich.table.Table.grid(padding=(0, 2))
    for row in self._rows:
      detail = row.step if row.verdict is None else row.scope
      table.add_row(row.status(), row.name, detail, row.elapsed(), row.progress())
    return table


def choose(stream: TextIO, *, color: bool, verbose: bool) -> Display:
  """the display for `stream`: the live table when it is a terminal nothing streams over."""
  if stream.isatty() and not verbose:
    return Live(rich.console.Console(file=stream, no_color=not color), color=color)
  return Plain(stream, color=color, verbose=verbose)
