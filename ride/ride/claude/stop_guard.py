"""Claude `Stop` hook holding a one-shot session's turn end to its missions.

Print mode holds the process while a background task is pending, so a turn
ending under `quest watch` with a mission in flight is a wait and stands. The
hook blocks the two other ends once per turn — Claude sets `stop_hook_active`
on the stop that follows a blocked one — with the reason as the model's next
input: missions in flight with no watch, and the watch with nothing in flight.

`background_tasks` (id, status, command per running task) is read off the hook
input as Claude Code sends it, undocumented. A payload without it is reported on
stderr with a non-blocking status, so the session ends as it would without the
hook rather than on a guess.
"""

import json
import os
import sys
from typing import Any, Literal

from bro.broker.environment import BROKER_CHANNEL
from bro.quest import LiveMission, live_mission_line, live_missions
from bro.summon import summoned

Surface = Literal['full', 'raw']

WATCH_COMMAND = 'quest watch'
FAILURE_STATUS = 1


def _running_watch_ids(tasks: list[dict[str, Any]]) -> list[str]:
  return [
    task['id']
    for task in tasks
    if task.get('status') == 'running' and task.get('command') == WATCH_COMMAND
  ]


def _in_flight_lines(missions: list[LiveMission]) -> str:
  return '\n'.join(live_mission_line(mission) for mission in missions)


def _unwatched_notice(missions: list[LiveMission], surface: Surface) -> str:
  count = len(missions)
  plural = 's' if count > 1 else ''
  if surface == 'full':
    wait = (
      f'Arm `Monitor` on exactly `{WATCH_COMMAND}`, persistent, and end the turn to wait for '
      'its events; end a mission you no longer need with `quest cancel <mission id>`.'
    )
  else:
    wait = (
      'Keep the turn active while the work runs, or end a mission you no longer need with '
      '`bro::quest_cancel`.'
    )
  return (
    f'{count} mission{plural} in flight and no `{WATCH_COMMAND}` armed: ending this turn ends '
    f'the session and orphans {"them" if count > 1 else "it"}. {wait}\n'
    f'{_in_flight_lines(missions)}'
  )


def _idle_watch_notice(watch_ids: list[str], *, summoned: bool) -> str:
  tasks = ', '.join(watch_ids)
  end = (
    'Deliver your result with `bro::answer`, which ends the session'
    if summoned
    else 'Stop it with `TaskStop` and end the turn'
  )
  return (
    f'`{WATCH_COMMAND}` is armed (task {tasks}) with no mission in flight: it holds this '
    f'session open until it is killed. {end}, or summon what remains.'
  )


def notice(
  payload: dict[str, Any], missions: list[LiveMission], surface: Surface, *, summoned: bool
) -> str | None:
  """The reason to hold this turn end, or None to let it stand."""
  if payload.get('stop_hook_active') is True:
    return None
  tasks = payload.get('background_tasks')
  if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
    raise ValueError('the Stop hook input carries no background_tasks list')
  watch_ids = _running_watch_ids(tasks)
  if len(missions) > 0 and len(watch_ids) == 0:
    return _unwatched_notice(missions, surface)
  if len(missions) == 0 and len(watch_ids) > 0:
    return _idle_watch_notice(watch_ids, summoned=summoned)
  return None


def _block(reason: str) -> None:
  print(json.dumps({'decision': 'block', 'reason': reason}))


def main(argv: list[str]) -> int:
  surface = argv[1]
  if surface not in ('full', 'raw'):
    raise ValueError(f'unknown session surface {surface!r}')
  payload = json.load(sys.stdin)
  missions = live_missions() if os.environ.get(BROKER_CHANNEL) is not None else []
  reason = notice(payload, missions, surface, summoned=summoned())
  if reason is not None:
    _block(reason)
  return 0


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except Exception as error:
    print(f'stop guard failed, letting the turn end stand: {error}', file=sys.stderr)
    sys.exit(FAILURE_STATUS)
