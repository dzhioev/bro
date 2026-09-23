"""Claude `Stop` hook holding a one-shot session's turn end to its live work.

A one-shot session ends at a turn end with no background task running, and
only a task's end starts a turn. The hook blocks the unpaired states once per
turn: a watch running with no `watch-next` waiting, which nothing would wake;
missions in flight with no running task; and running tasks with neither a
mission in flight nor a wait.

`background_tasks` (id, status, command per task) is read off the hook input as
Claude Code sends it, undocumented.
A payload without it is reported on stderr with a non-blocking status.
"""

import json
import os
import re
import sys
from typing import Any

from bro.broker.environment import BROKER_CHANNEL
from bro.mission import LiveMission, live_mission_line, live_missions
from bro.summon import summoned
from ride.claude.watch_commands import WATCH_NEXT, WATCH_RUN

FAILURE_STATUS = 1


def _running_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
  return [task for task in tasks if task.get('status') == 'running']


def _running(tasks: list[dict[str, Any]], pattern: re.Pattern[str]) -> list[dict[str, Any]]:
  return [task for task in tasks if pattern.search(str(task.get('command', '')))]


def _in_flight_lines(missions: list[LiveMission]) -> str:
  return '\n'.join(live_mission_line(mission) for mission in missions)


def _mission_routes(missions: list[LiveMission]) -> str:
  has_quests = any(mission.type == 'bro' for mission in missions)
  has_other_missions = any(mission.type != 'bro' for mission in missions)
  routes = []
  if has_quests:
    routes.append(
      '`watch-run quest watch` in the background with `watch-next` waiting, and '
      '`quest cancel <quest id>`, for quests'
    )
  if has_other_missions:
    routes.append(
      '`watch-run mission watch` in the background with `watch-next` waiting, and '
      '`mission cancel <mission id>`, for other missions'
    )
  return '; '.join(routes)


def _unwatched_notice(missions: list[LiveMission]) -> str:
  count = len(missions)
  plural = 's' if count > 1 else ''
  return (
    f'{count} mission{plural} in flight and no background task running: ending this turn ends '
    f'the session and orphans {"them" if count > 1 else "it"}. Keep a watch on this work and '
    f'end the turn to wait for its lines: {_mission_routes(missions)}.\n{_in_flight_lines(missions)}'
  )


def _unwaited_watch_notice(producers: list[dict[str, Any]]) -> str:
  labels = ', '.join(_task_label(task) for task in producers)
  return (
    f'A watch runs with no `watch-next` waiting: {labels}. Nothing would start your next turn '
    'for its lines. Run `watch-next` in the background and end the turn, or stop the watch with '
    '`TaskStop` once it is no longer needed.'
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


def notice(payload: dict[str, Any], missions: list[LiveMission], *, summoned: bool) -> str | None:
  """Return the reason to hold this turn end, or None to let it stand."""
  if payload.get('stop_hook_active') is True:
    return None
  tasks = payload.get('background_tasks')
  if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
    raise ValueError('the Stop hook input carries no background_tasks list')
  running = _running_tasks(tasks)
  producers = _running(running, WATCH_RUN)
  waits = _running(running, WATCH_NEXT)
  if len(producers) > 0 and len(waits) == 0:
    return _unwaited_watch_notice(producers)
  if len(missions) > 0 and len(running) == 0:
    return _unwatched_notice(missions)
  if len(missions) == 0 and len(running) > 0 and len(waits) == 0:
    return _idle_tasks_notice(running, summoned=summoned)
  return None


def _block(reason: str) -> None:
  print(json.dumps({'decision': 'block', 'reason': reason}))


def main(argv: list[str]) -> int:
  del argv
  payload = json.load(sys.stdin)
  missions = live_missions() if os.environ.get(BROKER_CHANNEL) is not None else []
  reason = notice(payload, missions, summoned=summoned())
  if reason is not None:
    _block(reason)
  return 0


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except Exception as error:
    print(f'stop guard failed, letting the turn end stand: {error}', file=sys.stderr)
    sys.exit(FAILURE_STATUS)
