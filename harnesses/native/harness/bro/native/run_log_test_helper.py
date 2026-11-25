import os
from pathlib import Path

from bro.native.echo_declaration import Recipe as EchoRecipe
from bro.native.harness import BRO
from bro.native.run_log import RunLog
from bro.registry import create_bro


def session_environment(directory: Path) -> dict[str, str]:
  return {
    **os.environ,
    'BRO_STORE': str(directory / 'absent-credentials'),
    'RIDE_SESSION_DIR': str(directory / 'session'),
    'RIDE_WORKSPACE': 'test-session',
    'RIDE_HOST': 'test-host',
    'RIDE_HOST_TREE': str(directory / 'tree'),
    'RIDE_ISOLATION': 'unboxed',
    'RIDE_COMMAND': 'ride along test-session',
  }


def open_log(path: Path, *, interactive: bool = False) -> RunLog:
  writer = RunLog(path)
  writer.start('dev', {'type': 'echo', 'model': 'echo'}, 'system', None, interactive, 'test')
  return writer


def conversation_log(
  path: Path, *, parent=None, recipe=None, prompt=None, session_at=None, hold='guided'
):
  if prompt is None:
    prompt, session_at = create_bro('bro').system_prompt_parts_for(hold=hold, harness=BRO)
  writer = RunLog(path)
  writer.start(
    'bro',
    (EchoRecipe(model='echo') if recipe is None else recipe).dump(),
    prompt,
    parent,
    True,
    'test',
    hold=hold,
    session_at=session_at,
  )
  return writer


def echo_call(writer, *, text='reply', calls=(), stopped=False):
  response = {'model': 'echo', 'text': text}
  wire = 'echo'
  if calls:
    wire = 'responses'
    response = {
      'id': 'response',
      'output': [
        {'type': 'function_call', 'name': 'lookup', 'call_id': item, 'arguments': '{}'}
        for item in calls
      ],
    }
  return writer.step(
    'llm_call',
    {'request': {'tools': []}, 'response': response},
    wire=wire,
    **({'stopped': 'failed'} if stopped else {}),
  )
