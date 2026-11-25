"""Continue native run logs through the selected wire."""

from pathlib import Path
from typing import Optional

from bro.llm import wires
from bro.llm.llm import Recipe
from bro.llm.providers import require_admitted
from bro.llm.resolved import IN_PROCESS_SOURCE, ResolvedRecipe
from bro.llm.tracker import NullTracker, RunFork, Tracker
from bro.llm.wire import Prefix
from bro.native.harness import BRO
from bro.native.run_log import RunLog, Snapshot, chain, new_path, read
from bro.native.runner import Runner
from bro.registry import create_bro
from bro.trails.model import Step


def replay_prefix(log: Snapshot, up_to_step_id: int) -> Prefix:
  steps = [
    step
    for segment, bound in chain(log, up_to_step_id)
    for step in segment.steps[: bound + 1]
    if step.kind != 'system_prompt'
  ]
  return Prefix(log.prompt, tuple(steps))


def latest_fork_point(log: Snapshot) -> int:
  """The newest step leaving no unanswered tool call."""
  last_good: Optional[int] = None
  pending: set[str] = set()
  for step in log.steps:
    if step.kind == 'system_prompt':
      if log.opened.get('forked_from') is not None and last_good is None:
        last_good = step.step_id
    elif step.kind in {'user_input', 'notification'}:
      if len(pending) == 0:
        last_good = step.step_id
    elif step.kind == 'llm_call':
      if 'stopped' in step.extras:
        continue
      response = step.body.get('response') if isinstance(step.body, dict) else None
      if not isinstance(response, dict):
        raise ValueError(f'llm_call step {step.step_id} has no response object')
      decoded = wires.decode(step.extras.get('wire'), response)
      pending = set()
      for call in decoded.calls:
        if not call.call_id:
          raise ValueError(f'llm_call step {step.step_id} has a tool call without an id')
        pending.add(call.call_id)
      if len(pending) == 0:
        last_good = step.step_id
    elif step.kind == 'tool_result':
      pending.discard(step.extras.get('call_id'))
      if len(pending) == 0:
        last_good = step.step_id
  if last_good is None:
    raise ValueError(f'run log {str(log.path)!r} has no step to resume from')
  return last_good


def fork(
  run: Path,
  up_to_step_id: Optional[int] = None,
  *,
  recipe: Recipe | ResolvedRecipe | None = None,
  system_prompt: Optional[str] = None,
  record: bool = True,
  tracker: Optional[Tracker] = None,
  surface: str,
  hold: Optional[str] = None,
  activity_file: Optional[Path] = None,
  run_log: Optional[Path] = None,
  notice: Optional[str] = None,
) -> Runner:
  """Build a continuation with a server chain or a restored same-wire prefix;
  `notice` is delivered as a notification ahead of its first exchange."""
  log = read(run)
  bound = latest_fork_point(log) if up_to_step_id is None else up_to_step_id
  target = new_path(log.path.parent) if run_log is None else run_log.resolve()
  if target.parent != log.path.parent:
    raise ValueError(f'--fork requires --run-log beside {log.path}')
  resolved = (
    ResolvedRecipe(Recipe.from_dict(log.opened['recipe']), f'run log {log.path.stem}')
    if recipe is None
    else recipe
    if isinstance(recipe, ResolvedRecipe)
    else ResolvedRecipe(recipe, IN_PROCESS_SOURCE)
  )
  selected_recipe = resolved.recipe
  require_admitted(selected_recipe)
  if selected_recipe.WIRE not in wires.client_names():
    raise ValueError(f'no native client for recorded wire {selected_recipe.WIRE!r}')
  prefix = replay_prefix(log, bound)
  for step in prefix.steps:
    if step.kind == 'llm_call':
      tag = step.extras.get('wire')
      recorded_wire = wires.registry.legacy_name() if tag is None else tag
      if recorded_wire != selected_recipe.WIRE:
        raise ValueError(
          f'cannot fork wire {recorded_wire!r} with target wire {selected_recipe.WIRE!r}; '
          f'select a recipe on recorded wire {recorded_wire!r}'
        )
  bro = create_bro(log.opened['bro'])
  effective_hold = hold if hold is not None else 'guided'
  composed, session_at = bro.system_prompt_parts_for(hold=effective_hold, harness=BRO)
  if system_prompt is not None:
    effective_prompt, session_at = system_prompt, len(system_prompt)
  elif composed[session_at:] == log.prompt[log.session_at :]:
    effective_prompt, session_at = log.prompt, log.session_at
  else:
    effective_prompt = composed
  fork_step = log.steps[bound]
  use_server_side = _server_side_eligible(
    forked_from_recipe=log.opened['recipe'],
    recipe=selected_recipe,
    fork_step=fork_step,
    system_prompt_override=effective_prompt if effective_prompt != log.prompt else None,
  )
  runner = Runner(bro, resolved, activity_file=activity_file, run_log=target)
  runner._tracker = (
    NullTracker() if not record else tracker if tracker is not None else runner._make_tracker()
  )
  inner_llm = runner._create_llm(hold=effective_hold)
  if use_server_side:
    inner_llm.seed_chain(fork_step.extras['response_id'])
  else:
    inner_llm.restore(Prefix(effective_prompt, prefix.steps))
  if notice is not None:
    inner_llm.deliver_ahead(notice)
  runner._llm = inner_llm
  runner._tracker.start(
    bro=bro.name,
    recipe=selected_recipe.dump(),
    system_prompt=effective_prompt,
    session_at=session_at,
    forked_from=RunFork(log.path.stem, bound),
    interactive=True,
    surface=surface,
    hold=effective_hold,
  )
  runner.run_log = runner._tracker.path if isinstance(runner._tracker, RunLog) else None
  return runner


def _server_side_eligible(
  *,
  forked_from_recipe: dict,
  recipe: Recipe,
  fork_step: Step,
  system_prompt_override: Optional[str],
) -> bool:
  if wires.registry.get(recipe.WIRE).CHAINING != 'server':
    return False
  if system_prompt_override is not None:
    return False
  if forked_from_recipe.get('type') != recipe.TYPE:
    return False
  if forked_from_recipe.get('model') != recipe.dump()['model']:
    return False
  if fork_step.kind != 'llm_call' or 'stopped' in fork_step.extras:
    return False
  return fork_step.extras.get('response_id') is not None
