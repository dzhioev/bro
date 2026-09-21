"""Claude `Stop` hook holding a one-shot session's turn end to its live work.

Print mode holds the process while any background task is pending.
The hook blocks the two unpaired states once per turn: missions in flight with no
running task, and running tasks with no mission in flight.

`background_tasks` (id, status, command per task) is read off the hook input as
Claude Code sends it, undocumented.
A payload without it is reported on stderr with a non-blocking status.
"""

import json
import os
import sys
from typing import Any, Literal

from bro.broker.environment import BROKER_CHANNEL
from bro.mission import LiveMission, live_mission_line, live_missions
from bro.summon import summoned

Surface = Literal['full', 'raw']

FAILURE_STATUS = 1


def _running_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
  return [task for task in tasks if task.get('status') == 'running']


def _in_flight_lines(missions: list[LiveMission]) -> str:
  return '\n'.join(live_mission_line(mission) for mission in missions)


def _mission_routes(missions: list[LiveMission], surface: Surface) -> str:
  has_quests = any(mission.type == 'bro' for mission in missions)
  has_other_missions = any(mission.type != 'bro' for mission in missions)
  routes = []
  if has_quests:
    if surface == 'full':
      routes.append('`quest watch` and `quest cancel <quest id>` for quests')
    else:
      routes.append('`bro::quest_check` and `bro::quest_cancel` for quests')
  if has_other_missions:
    routes.append(
      '`mission watch` and `mission cancel <mission id>` for other missions'
      if surface == 'full'
      else '`mission history` and `mission cancel <mission id>` for other missions'
    )
  return '; '.join(routes)


def _unwatched_notice(missions: list[LiveMission], surface: Surface) -> str:
  count = len(missions)
  plural = 's' if count > 1 else ''
  if surface == 'full':
    wait = (
      'Arm `Monitor`, persistent, on the watch command this work needs and end the turn to '
      'wait for its events'
    )
  else:
    wait = 'Keep the turn active while the work runs, or end work you no longer need'
  return (
    f'{count} mission{plural} in flight and no background task running: ending this turn ends '
    f'the session and orphans {"them" if count > 1 else "it"}. {wait}; use '
    f'{_mission_routes(missions, surface)}.\n{_in_flight_lines(missions)}'
  )


def _task_label(task: dict[str, Any]) -> str:
  task_id = task.get('id')
  command = task.get('command')
  if not isinstance(task_id, str) or not isinstance(command, str):
    raise ValueError('a running background task carries no id or command')
  return f'{task_id} `{command.replace("`", "\\`")}`'


def _idle_tasks_notice(tasks: list[dict[str, Any]], *, summoned: bool) -> str:
  labels = ', '.join(_task_label(task) for task in tasks)
  end = (
    'Deliver your result with `bro::answer`, which ends the session'
    if summoned
    else 'Stop the tasks with `TaskStop` and end the turn'
  )
  return (
    f'Background tasks are running with no mission in flight: {labels}. They hold this session '
    f'open until killed. {end}, or launch the work they are waiting for.'
  )


def notice(
  payload: dict[str, Any], missions: list[LiveMission], surface: Surface, *, summoned: bool
) -> str | None:
  """Return the reason to hold this turn end, or None to let it stand."""
  if payload.get('stop_hook_active') is True:
    return None
  tasks = payload.get('background_tasks')
  if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
    raise ValueError('the Stop hook input carries no background_tasks list')
  running = _running_tasks(tasks)
  if len(missions) > 0 and len(running) == 0:
    return _unwatched_notice(missions, surface)
  if len(missions) == 0 and len(running) > 0:
    return _idle_tasks_notice(running, summoned=summoned)
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
