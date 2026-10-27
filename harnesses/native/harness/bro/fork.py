"""Forking recorded native trails through the selected wire."""

from collections.abc import Callable
from pathlib import Path
from typing import Optional

from bro.llm import wires
from bro.llm.llm import LLMSpec, NativeLLMSpec
from bro.llm.tracker import NullTracker, Tracker
from bro.llm.wire import Prefix
from bro.native.harness import BRO
from bro.native.runner import Runner
from bro.registry import create_bro
from bro.trails.lineage import walk_chain
from bro.trails.model import ForkedFrom, RecordedTrail, Step

# the harness whose records this module replays; `bro/trails/record/bro.py`
# stamps it on every trail it opens
_BRO_HARNESS = 'bro'


def replay_prefix(
  trail: RecordedTrail,
  up_to_step_id: int,
  *,
  fetch_forked_from: Optional[Callable[[str], RecordedTrail]] = None,
) -> Prefix:
  system_text = _extract_system_prompt(trail)
  if trail.header.forked_from is not None and fetch_forked_from is None:
    raise ValueError(
      f'trail {trail.header.id!r} is a fork of {trail.header.forked_from.trail_id!r}; '
      'pass fetch_forked_from to rebuild the ancestor prefix'
    )

  def parent(recorded: RecordedTrail) -> Optional[tuple[str, ForkedFrom]]:
    pointer = recorded.header.forked_from
    return None if pointer is None else (pointer.trail_id, pointer)

  def fetch_parent(trail_id: str) -> RecordedTrail:
    assert fetch_forked_from is not None
    return fetch_forked_from(trail_id)

  chain = walk_chain(
    trail,
    identity=lambda recorded: recorded.header.id,
    parent=parent,
    fetch_parent=fetch_parent,
  )
  steps: list[Step] = []
  for recorded, bound in chain:
    segment_bound = bound.step_id if bound is not None else up_to_step_id
    steps.extend(_prefix_steps(recorded, segment_bound))
  return Prefix(system_text, tuple(steps))


def _prefix_steps(trail: RecordedTrail, up_to_step_id: int) -> list[Step]:
  steps: list[Step] = []
  for step in trail.steps:
    if step.kind != 'system_prompt':
      steps.append(step)
    if step.step_id == up_to_step_id:
      return steps
  raise ValueError(f'step_id {up_to_step_id!r} not found in trail {trail.header.id!r}')


def latest_fork_point(trail: RecordedTrail) -> int:
  """the step id of the newest legal fork point — where a resume continues from.

  walks the steps tracking the turn's unanswered `function_call`s; a step
  qualifies when nothing is pending after it: a `user_input`, a `notification`,
  an `llm_call` with no function calls in its output, or the `tool_result` that answers its
  turn's last call. a trail killed mid-tool-loop thus resumes from the last
  consistent point before the unanswered call. for a fork trail the
  `system_prompt` step qualifies as the floor — an empty continuation still
  resumes, through the ancestor prefix its forked_from pointer carries.

  raises `ValueError` when the trail has no step to resume from (a forked_fromless
  trail with no user input recorded).
  """
  last_good: Optional[int] = None
  pending: set[str] = set()
  for step in trail.steps:
    if step.kind == 'system_prompt':
      if trail.header.forked_from is not None and last_good is None:
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
    raise ValueError(f'trail {trail.header.id!r} has no step to resume from')
  return last_good


def fork(
  forked_from_trail: RecordedTrail,
  up_to_step_id: int,
  *,
  llm_spec: Optional[NativeLLMSpec] = None,
  system_prompt: Optional[str] = None,
  record: bool = True,
  tracker: Optional[Tracker] = None,
  surface: str,
  hold: Optional[str] = None,
  fetch_forked_from: Optional[Callable[[str], RecordedTrail]] = None,
  activity_file: Optional[Path] = None,
) -> Runner:
  """Build a continuation with a server chain or a restored same-wire prefix."""
  if forked_from_trail.header.harness != _BRO_HARNESS:
    raise ValueError(
      f'trail {forked_from_trail.header.id!r} was recorded by the '
      f'{forked_from_trail.header.harness!r} harness, which drives its own loop; there is no '
      'bro-native conversation to continue'
    )
  bro_name = forked_from_trail.header.bro
  if bro_name is None:
    raise ValueError(f'trail {forked_from_trail.header.id!r} has no bro persona')
  spec = llm_spec if llm_spec is not None else LLMSpec.from_dict(forked_from_trail.header.llm_spec)
  if not isinstance(spec, NativeLLMSpec) or spec.WIRE not in wires.client_names():
    raise ValueError(
      f'trail {forked_from_trail.header.id!r} was recorded under {spec.TYPE!r}, whose harness '
      f'has no client for wire {spec.WIRE!r}; there is no bro-native conversation to continue'
    )
  prefix = replay_prefix(forked_from_trail, up_to_step_id, fetch_forked_from=fetch_forked_from)
  for step in prefix.steps:
    if step.kind == 'llm_call':
      tag = step.extras.get('wire')
      recorded_wire = wires.registry.legacy_name() if tag is None else tag
      if recorded_wire != spec.WIRE:
        raise ValueError(
          f'cannot fork wire {recorded_wire!r} with target wire {spec.WIRE!r}; '
          f'select a recipe on recorded wire {recorded_wire!r}'
        )
  bro = create_bro(bro_name)
  bro.llm_spec = spec

  fork_step = _find_step(forked_from_trail, up_to_step_id)
  forked_from_system_prompt = _extract_system_prompt(forked_from_trail)
  effective_system_prompt = (
    system_prompt if system_prompt is not None else forked_from_system_prompt
  )
  prompt_changed = effective_system_prompt != forked_from_system_prompt
  effective_hold = hold if hold is not None else 'guided'
  if system_prompt is None and effective_hold != forked_from_trail.header.hold:
    effective_system_prompt = bro.system_prompt_for(hold=effective_hold, harness=BRO)
    prompt_changed = effective_system_prompt != forked_from_system_prompt
  use_server_side = _server_side_eligible(
    forked_from_spec=forked_from_trail.header.llm_spec,
    new_spec=spec,
    fork_step=fork_step,
    system_prompt_override=effective_system_prompt if prompt_changed else None,
  )
  # This value is the complete prompt, including its hold fragment;
  # pre-assigning it bypasses the runner's fresh-run fragment append.
  bro._system_prompt_override = effective_system_prompt

  runner = Runner(bro, activity_file=activity_file)
  runner._tracker = (
    NullTracker() if not record else (tracker if tracker is not None else runner._make_tracker())
  )
  inner_llm = runner._create_llm(hold=effective_hold)
  if use_server_side:
    inner_llm.seed_chain(fork_step.extras['response_id'])
  else:
    inner_llm.restore(Prefix(effective_system_prompt, prefix.steps))
  runner._llm = inner_llm

  forked_from = ForkedFrom(
    trail_id=forked_from_trail.header.id,
    step_id=up_to_step_id,
  )
  trail_id = runner._tracker.start_trail(
    bro=bro.name,
    llm_spec=spec.dump(),
    system_prompt=effective_system_prompt,
    forked_from=forked_from,
    interactive=True,
    surface=surface,
    hold=effective_hold,
  )
  runner.trail_id = trail_id if len(trail_id) > 0 else None
  return runner


def _server_side_eligible(
  *,
  forked_from_spec: dict,
  new_spec: NativeLLMSpec,
  fork_step: Step,
  system_prompt_override: Optional[str],
) -> bool:
  if wires.registry.get(new_spec.WIRE).CHAINING != 'server':
    return False
  if system_prompt_override is not None:
    return False
  if forked_from_spec.get('type') != new_spec.TYPE:
    return False
  if forked_from_spec.get('model') != new_spec.model:
    return False
  if fork_step.kind != 'llm_call' or 'stopped' in fork_step.extras:
    return False
  return fork_step.extras.get('response_id') is not None


def _find_step(trail: RecordedTrail, step_id: int) -> Step:
  for step in trail.steps:
    if step.step_id == step_id:
      return step
  raise ValueError(f'step_id {step_id!r} not found in trail {trail.header.id!r}')


def _extract_system_prompt(trail: RecordedTrail) -> str:
  for step in trail.steps:
    if step.kind == 'system_prompt':
      return step.body
  raise ValueError(f'trail {trail.header.id!r} has no system_prompt step')
