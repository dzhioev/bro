import io
import subprocess

import rich.console

from bro.local import gate_display


def failed(output: str) -> subprocess.CalledProcessError:
  return subprocess.CalledProcessError(1, ('probe',), output=output)


def test_a_duration_reads_in_seconds_under_a_minute_and_in_minutes_past_it():
  assert gate_display.duration(3.4) == '3s'
  assert gate_display.duration(65) == '1m05s'


def test_the_quiet_display_shows_a_command_output_only_once_its_command_failed():
  stream = io.StringIO()
  display = gate_display.Plain(stream, color=False, verbose=False)
  display.stage_started('unit', 'pytest over the whole roster')
  display.step('pytest, 3 modules in the worker pool')
  display.line('...      [100%]\n')
  display.stage_ended('ok', 2)
  display.stage_started('types', 'pyright')
  display.step('pyright')
  display.line('x.py:1: error\n')
  display.stage_ended('FAILED', 61)
  display.skipped('llm', 'opt-in; run it with --only llm')
  display.failure('types', 'pyright', failed('x.py:1: error\n'))
  display.verdict([('unit', 'ok'), ('types', 'FAILED')])

  assert stream.getvalue() == (
    'unit: pytest over the whole roster\n'
    'unit: ok in 2s\n'
    'types: pyright\n'
    'types: FAILED in 1m01s\n'
    'skipping the llm stage (opt-in; run it with --only llm)\n'
    '\n=== types: pyright exited with 1 ===\n'
    'x.py:1: error\n'
    '\ngate: unit ok | types FAILED\n'
  )


def test_the_verbose_display_streams_under_each_step_and_replays_no_failure():
  stream = io.StringIO()
  display = gate_display.Plain(stream, color=False, verbose=True)
  display.stage_started('unit', 'pytest over the whole roster')
  display.step('pytest, 3 modules in the worker pool')
  display.line('F        [100%]\n')
  display.stage_ended('FAILED', 2)
  display.failure('unit', 'pytest, 3 modules in the worker pool', failed('F        [100%]\n'))

  assert stream.getvalue() == (
    'unit: pytest over the whole roster\n'
    'unit: pytest, 3 modules in the worker pool\n'
    'F        [100%]\n'
    'unit: FAILED in 2s\n'
    '\n=== unit: pytest, 3 modules in the worker pool exited with 1 ===\n'
  )


def test_the_display_names_the_step_a_failure_lands_in():
  display = gate_display.Plain(io.StringIO(), color=False, verbose=False)
  display.stage_started('lint', 'ruff')
  assert display.step_text is None

  display.step('ruff check')
  assert display.step_text == 'ruff check'

  display.stage_ended('ok', 1)
  assert display.step_text is None


def _terminal() -> tuple[rich.console.Console, io.StringIO]:
  output = io.StringIO()
  return rich.console.Console(file=output, force_terminal=False, width=120, no_color=True), output


def test_the_live_display_draws_a_row_per_stage():
  console, output = _terminal()
  display = gate_display.Live(console, color=False)
  display.stage_started('unit', 'pytest over the whole roster')
  display.step('pytest, 3 modules in the worker pool')
  display.stage_ended('ok', 65)
  display.skipped('llm', 'opt-in; run it with --only llm')
  display.stage_started('types', 'pyright')
  display.step('pyright')
  console.print(display)

  rendered = output.getvalue()
  assert 'ok' in rendered and 'pytest over the whole roster' in rendered and '1m05s' in rendered
  assert 'skipped' in rendered and 'opt-in; run it with --only llm' in rendered
  assert 'types' in rendered and 'pyright' in rendered
  assert display.step_text == 'pyright'


def test_the_live_display_bars_the_progress_pytest_reports_within_a_step():
  console, output = _terminal()
  display = gate_display.Live(console, color=False)
  display.stage_started('unit', 'pytest over the whole roster')
  display.step('pytest, 3 modules in the worker pool')
  console.print(display)
  assert '━' not in output.getvalue()

  display.line('.......  [ 42%]\n')
  console.print(display)
  assert '━' in output.getvalue()

  output.truncate(0)
  output.seek(0)
  display.step('pytest, 1 single-process module')
  console.print(display)
  assert '━' not in output.getvalue()


def test_the_live_display_prints_failures_and_the_verdict_below_the_table():
  console, output = _terminal()
  display = gate_display.Live(console, color=False)
  display.stage_started('types', 'pyright')
  display.step('pyright')
  display.stage_ended('FAILED', 1)
  display.failure('types', 'pyright', failed('x.py:1: error\n'))
  display.verdict([('types', 'FAILED')])

  assert output.getvalue().endswith(
    '=== types: pyright exited with 1 ===\nx.py:1: error\n\ngate: types FAILED\n'
  )


class _Terminal(io.StringIO):
  def isatty(self) -> bool:
    return True


def test_a_terminal_gets_the_live_table_unless_output_streams_over_it():
  assert isinstance(gate_display.choose(_Terminal(), color=True, verbose=False), gate_display.Live)
  assert isinstance(gate_display.choose(_Terminal(), color=True, verbose=True), gate_display.Plain)
  assert isinstance(
    gate_display.choose(io.StringIO(), color=False, verbose=False), gate_display.Plain
  )
