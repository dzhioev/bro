"""Local run-log continuation and conversation history."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.fork import fork, latest_fork_point
from bro.llm.llm import Recipe
from bro.llm.resolved import ResolvedRecipe
from bro.native.run_log import Snapshot, chain, latest, read
from bro.native.runner import Runner
from bro.native_trails.format import STEP_LINE_FORMAT
from bro.trails.backends import get_format
from bro.trails.display import DisplayRecord
from bro.trails.display.recorded import MessageAdapter

RESUME_LATEST = 'latest'


@dataclass(frozen=True)
class ResumedCall:
  runner: Runner
  history: list[DisplayRecord]
  run: Path


def conversation_history(log: Snapshot, bound: int) -> list[DisplayRecord]:
  adapter = MessageAdapter()
  format = get_format('bro')
  records = []
  for segment, through in chain(log, bound):
    messages = []
    for entry in segment.entries:
      if entry.step_number is None or entry.step_number > through:
        continue
      row = {
        'run_log': STEP_LINE_FORMAT,
        'body': entry.line,
        'trail_id': segment.path.stem,
        'step_id': entry.step_number,
        'ts': entry.value['at'],
      }
      messages.extend(format.project(row))
    records.extend(adapter.message_records(segment.path.stem, messages))
  return records


def resume(
  bro_name: str,
  run_ref: str,
  *,
  recipe: Recipe | ResolvedRecipe | None = None,
  at: Optional[int] = None,
  hold: Optional[str] = None,
  activity_file: Optional[Path] = None,
  run_log: Optional[Path] = None,
  notice: Optional[str] = None,
) -> ResumedCall:
  path = latest(bro_name) if run_ref == RESUME_LATEST else Path(run_ref)
  if path is None:
    raise ValueError(f'no run log found for {bro_name} among the 200 newest run logs')
  log = read(path)
  if log.opened['bro'] != bro_name:
    raise ValueError(f'run log {path} belongs to bro {log.opened["bro"]!r}, not {bro_name!r}')
  bound = latest_fork_point(log) if at is None else at
  history = conversation_history(log, bound)
  runner = fork(
    path,
    bound,
    recipe=recipe,
    surface='call',
    hold=hold,
    activity_file=activity_file,
    run_log=run_log,
    notice=notice,
  )
  return ResumedCall(runner=runner, history=history, run=log.path)
