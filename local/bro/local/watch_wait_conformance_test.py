"""conformance: an unattended session whose only remaining work is a watch's
next line ends its turn to wait for it, and that line wakes it.

A session that keeps its turn alive instead, sleeping or polling until the line
shows up, spends a model call per poll on the silence and holds the run in a
turn nothing needs. One poll already in the context is what tips a model into
that loop, so a session that has checked once must still end its turn after."""

import secrets
from pathlib import Path

import pytest

from bro.local.conformance_test_helper import data_home, harness_recipes, run_unattended

_PROBE = 'bro-watch-probe'
_DELAY_SECONDS = 45
_SEED = 'sleep 5'
# one reflexive look while waiting is noise; a loop of them is the failure
_SPARE_CALLS = 1


def _tool(message: dict) -> str:
  return message['tool_name'].removeprefix('mcp__')


class _Wait:
  """how a session waited for `line`: the tool calls it made between the last
  watch it started before the line woke it and that wake, whether it had ended
  its turn when the line came, and the messages from the wake on."""

  def __init__(self, messages: list[dict], line: str):
    woken = next(
      index
      for index, message in enumerate(messages)
      if message['type'] == 'notification'
      and isinstance(message.get('content'), str)
      and line in message['content']
    )
    watched = max(
      index
      for index, message in enumerate(messages[:woken])
      if message['type'] == 'tool_call' and _tool(message) == 'bro__watch'
    )
    last_call = max(
      index for index, message in enumerate(messages[:woken]) if message['type'] == 'llm_call'
    )
    self.calls = [
      message for message in messages[watched + 1 : woken] if message['type'] == 'tool_call'
    ]
    # a line that came while a tool ran reached the model inside its turn
    self.ended_turn = all(
      message['type'] not in ('tool_call', 'tool_result')
      for message in messages[last_call + 1 : woken]
    )
    self.first_watch = next(
      index
      for index, message in enumerate(messages)
      if message['type'] == 'tool_call' and _tool(message) == 'bro__watch'
    )
    self.woken = woken
    self.after_wake = messages[woken:]


def _answered(after_wake: list[dict], line: str) -> bool:
  return any(
    message['type'] == 'assistant' and line in message['content'] for message in after_wake
  )


def _session(
  harness: str, recipe: str, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, ask: str
) -> tuple[str, list[dict]]:
  """the line a fresh emitter prints after the delay, and the messages of an
  unattended session asked to `ask` about it, `{emitter}` naming its path."""
  line = f'PROBE-LINE-{secrets.token_hex(4)}'
  tree = tmp_path / 'tree'
  tree.mkdir()
  emitter = tree / 'emit-line'
  emitter.write_text(f'#!/bin/sh\nsleep {_DELAY_SECONDS}\necho {line}\n')
  emitter.chmod(0o755)
  messages = run_unattended(
    harness=harness,
    recipe=recipe,
    bro=_PROBE,
    prompt=ask.format(emitter=emitter),
    tree=tree,
    data=data_home(tmp_path_factory),
  )
  return line, messages


@pytest.mark.parametrize(('harness', 'recipe'), harness_recipes())
def test_a_session_waiting_on_a_watch_ends_its_turn_until_the_line_wakes_it(
  harness: str, recipe: str, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
  line, messages = _session(
    harness,
    recipe,
    tmp_path,
    tmp_path_factory,
    '[[watch {emitter}]], and once the line it prints arrives, '
    'answer with that line and nothing else.',
  )

  wait = _Wait(messages, line)
  assert wait.ended_turn
  assert len(wait.calls) <= _SPARE_CALLS, [_tool(call) for call in wait.calls]
  assert _answered(wait.after_wake, line)


@pytest.mark.parametrize(('harness', 'recipe'), harness_recipes())
def test_a_session_that_checked_once_still_ends_its_turn_until_the_line_wakes_it(
  harness: str, recipe: str, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
  line, messages = _session(
    harness,
    recipe,
    tmp_path,
    tmp_path_factory,
    '[[watch {emitter}]]. Right after starting it, run `' + _SEED + '` once in the shell '
    'to give it a moment. Once the line it prints arrives, '
    'answer with that line and nothing else.',
  )

  wait = _Wait(messages, line)
  seeds = [
    index
    for index, message in enumerate(messages)
    if message['type'] == 'tool_call'
    and str(message['arguments'].get('command', '')).strip() == _SEED
  ]
  assert len(seeds) == 1 and wait.first_watch < seeds[0] < wait.woken, seeds
  waiting = [call for call in wait.calls if call is not messages[seeds[0]]]
  assert wait.ended_turn
  assert len(waiting) <= _SPARE_CALLS, [_tool(call) for call in waiting]
  assert _answered(wait.after_wake, line)
