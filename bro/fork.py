"""forking of recorded bro.trails.a *fork* spins up a fresh bro preseeded with a forked_from trail's prefix and
lets the caller continue with `.send(next_message)`. the new run gets its own
trail with `forked_from={trail_id, step_id}` so the source → child edge is
queryable through the fork index.

two replay paths, picked automatically based on the spec / fork-point combo:

- **server-side via `previous_response_id`** (cheap, same-provider/model
  only). when forking right after an `llm_call` step with the same provider,
  the same model, and no `system_prompt` override, set the new LLM's
  `_last_response_id` to that step's `response_id`. the provider holds the
  full prefix cached server-side; only the new user message is sent.
- **client-side via message replay** (universal). when any of those
  conditions doesn't hold (different model, different provider, fork at a
  non-`llm_call` step, prompt override), walk the trail and rebuild the full
  OpenAI input list, then pass it as the new LLM's input prefix. one API call,
  full re-tokenization.

reasoning-fidelity caveat for OpenAI reasoning models: same-model forks past
the provider's response_id TTL fall back to client-side replay and rely on
the encrypted reasoning items captured in each `llm_call.body.response.output`
at record time. cross-model forks discard reasoning by design — a different
model wouldn't have used the prior model's thinking anyway.
"""

import json
from collections.abc import Callable
from typing import Any, Optional, cast

import bro.llm.llms.openai as llm_llms_openai
from bro.bro import BaseBro
from bro.llm.llm import LLM, LLMSpec, NativeLLMSpec
from bro.llm.tracker import NullTracker, Tracker
from bro.registry import create_bro
from bro.trails.lineage import walk_chain
from bro.trails.model import ForkedFrom, RecordedTrail, Step

# the harness whose records this module replays; `bro/trails/record/bro.py`
# stamps it on every trail it opens
_BRO_HARNESS = 'bro'


def replay_messages(
  trail: RecordedTrail,
  up_to_step_id: int,
  *,
  fetch_forked_from: Optional[Callable[[str], RecordedTrail]] = None,
) -> list[dict]:
  """walk the trail's steps up to (and including) `up_to_step_id` and rebuild
  the OpenAI input list needed to resume the conversation from that point.

  layout of the returned list:
  - `{'role': 'system', 'content': <prompt>}` at index 0, taken from the
    trail's `system_prompt` step.
  - the ancestor prefix, when the trail is itself a fork: a fork trail's own
    steps carry only the post-fork suffix, so the prefix is rebuilt by
    walking through `fetch_forked_from(trail_id)` (each ancestor replayed up to its
    fork step, its system message dropped — the youngest trail's recorded
    prompt stands for the whole conversation). a fork trail replayed without
    `fetch_forked_from` raises rather than silently truncating the conversation.
  - `{'role': 'user', 'content': <text>}` for each `user_input`.
  - each `llm_call`'s `response.output` items appended in order — these carry
    intact `call_id`s on `function_call` items, which is what makes correct
    replay possible. `compaction` items are dropped: the replayed verbatim
    history subsumes what they summarize.
  - `{'type': 'function_call_output', 'call_id': ..., 'output': ...}` for each
    `tool_result`. dict outputs are JSON-encoded to match the wire format
    `OpenAI._execute_tool_calls` uses.

  legal fork points (the caller is responsible for picking one — or asking
  `latest_fork_point` for the trail's newest one): right after the first
  `user_input`, right after any terminal `llm_call`, right after any later
  `user_input`, right after a `tool_result` that completes its turn's calls,
  or the `system_prompt` step of a fork trail (an empty continuation — the
  replay is exactly the ancestor prefix). forking mid-tool-loop (right after
  an `llm_call` whose outputs include unanswered `function_call`s) produces an
  input the model cannot consume.

  raises `ValueError` if the trail has no `system_prompt` step or if
  `up_to_step_id` does not appear in it.
  """
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
  result: list[dict] = [{'role': 'system', 'content': system_text}]
  for recorded, bound in chain:
    segment_bound = bound.step_id if bound is not None else up_to_step_id
    result.extend(_replay_step_items(recorded, segment_bound))
  return result


def _replay_step_items(trail: RecordedTrail, up_to_step_id: int) -> list[dict]:
  items: list[dict] = []
  for step in trail.steps:
    if step.kind == 'user_input':
      items.append({'role': 'user', 'content': step.body})
    elif step.kind == 'llm_call':
      items.extend(_response_output_items(step.body))
    elif step.kind == 'tool_result':
      items.append(
        {
          'type': 'function_call_output',
          'call_id': step.extras['call_id'],
          'output': _encode_tool_output(step.body),
        }
      )
    # the youngest trail's prompt is already at index 0; decomposed output
    # records are represented by the canonical llm_call response.
    if step.step_id == up_to_step_id:
      return items
  raise ValueError(f'step_id {up_to_step_id!r} not found in trail {trail.header.id!r}')


def latest_fork_point(trail: RecordedTrail) -> int:
  """the step id of the newest legal fork point — where a resume continues from.

  walks the steps tracking the turn's unanswered `function_call`s; a step
  qualifies when nothing is pending after it: a `user_input`, an `llm_call`
  with no function calls in its output, or the `tool_result` that answers its
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
    elif step.kind == 'user_input':
      if len(pending) == 0:
        last_good = step.step_id
    elif step.kind == 'llm_call':
      pending = {
        item['call_id']
        for item in _response_output_items(step.body)
        if item.get('type') == 'function_call' and 'call_id' in item
      }
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
) -> BaseBro:
  """spin up a fresh bro preseeded with the forked_from trail's prefix up to
  `up_to_step_id`. call `.send(next_message)` on the returned bro to continue
  the conversation.

  `llm_spec` defaults to the forked_from's spec (rehydrated via `LLMSpec.from_dict`);
  pass an override for cross-model / cross-provider forks. `system_prompt`
  defaults to the prompt recorded on the forked_from (`system_prompt` step body);
  pass an override to fork with a swapped prompt.

  the replay path is picked automatically: same-provider + same-model + fork
  at an `llm_call` step + no `system_prompt` override → server-side via
  `previous_response_id`. otherwise → client-side message replay, which needs
  `fetch_forked_from` when the forked_from trail is itself a fork (see
  `replay_messages`).

  `hold` replaces the recorded hold fragment when provided; that prompt change
  selects client-side replay. Omit it to preserve the recorded prompt.

  `record=False` pins the new bro to a `NullTracker` — handy for one-shot
  exploration where the fork's trail is not worth keeping. `record=True` (the
  default) uses the explicit `tracker` if given, otherwise the bro's default
  factory (the production default; tests use `NullTracker` via `conftest.py`).
  `surface` labels the driving program on the new trail; every caller supplies
  it explicitly.
  """
  if forked_from_trail.header.harness != _BRO_HARNESS:
    raise ValueError(
      f'trail {forked_from_trail.header.id!r} was recorded by the '
      f'{forked_from_trail.header.harness!r} harness, which drives its own loop; there is no '
      'bro-native conversation to continue'
    )
  bro_name = forked_from_trail.header.bro
  if bro_name is None:
    raise ValueError(f'trail {forked_from_trail.header.id!r} has no bro persona')
  bro = create_bro(bro_name)
  spec = llm_spec if llm_spec is not None else LLMSpec.from_dict(forked_from_trail.header.llm_spec)
  if not isinstance(spec, NativeLLMSpec):
    raise ValueError(
      f'trail {forked_from_trail.header.id!r} was recorded under {spec.TYPE!r}, whose harness '
      'runs its own loop; there is no bro-native conversation to continue'
    )
  bro.llm_spec = spec

  fork_step = _find_step(forked_from_trail, up_to_step_id)
  forked_from_system_prompt = _extract_system_prompt(forked_from_trail)
  effective_system_prompt = (
    system_prompt if system_prompt is not None else forked_from_system_prompt
  )
  prompt_changed = system_prompt is not None
  if system_prompt is None and hold is not None and hold != forked_from_trail.header.hold:
    effective_system_prompt = _replace_hold_fragment(
      forked_from_system_prompt,
      recorded_hold=forked_from_trail.header.hold,
      resumed_hold=hold,
    )
    prompt_changed = True
  use_server_side = _server_side_eligible(
    forked_from_spec=forked_from_trail.header.llm_spec,
    new_spec=spec,
    fork_step=fork_step,
    system_prompt_override=effective_system_prompt if prompt_changed else None,
  )
  # This value is the complete recorded prompt, including its hold fragment;
  # pre-assigning it bypasses BaseBro's fresh-run fragment append.
  bro.system_prompt = effective_system_prompt
  effective_hold = hold if hold is not None else 'guided'

  bro._tracker = (
    NullTracker() if not record else (tracker if tracker is not None else bro._make_tracker())
  )
  bro._observer = bro._make_observer()
  inner_llm = spec.create_llm(
    mcp_servers=bro._mcp_servers_for(hold=effective_hold),
    observer=bro._observer,
    tracker=bro._tracker,
    agent=bro.agent,
  )
  if use_server_side:
    _seed_response_id(inner_llm, fork_step.extras['response_id'])
  else:
    prefix = replay_messages(forked_from_trail, up_to_step_id, fetch_forked_from=fetch_forked_from)
    if prompt_changed:
      prefix[0] = {'role': 'system', 'content': effective_system_prompt}
    _preseed(inner_llm, prefix)
  # pre-assigning _llm makes BaseBro.send take the subsequent-call branch on
  # the first .send(next_message) — it ships `[{'role': 'user', 'content': ...}]`
  # straight to the wrapped LLM, which either prepends the replayed prefix
  # (client-side) or passes `previous_response_id=<seeded>` (server-side).
  bro._llm = inner_llm

  forked_from = ForkedFrom(
    trail_id=forked_from_trail.header.id,
    step_id=up_to_step_id,
  )
  trail_id = bro._tracker.start_trail(
    bro=bro.name,
    llm_spec=spec.dump(),
    system_prompt=effective_system_prompt,
    forked_from=forked_from,
    interactive=True,
    surface=surface,
    hold=effective_hold,
  )
  bro.trail_id = trail_id if len(trail_id) > 0 else None
  return bro


def _replace_hold_fragment(
  system_prompt: str,
  *,
  recorded_hold: Optional[str],
  resumed_hold: str,
) -> str:
  if recorded_hold is None:
    raise ValueError('cannot change the hold of a trail that recorded no hold')
  from bro.base import credentials
  from bro.prompts import hold_fragment

  known_credentials = credentials.known_names()
  recorded_fragment = hold_fragment(
    recorded_hold, harness='bro', wire='bare', creds=known_credentials
  )
  suffix = f'\n\n{recorded_fragment}'
  if not system_prompt.endswith(suffix):
    raise ValueError('recorded system prompt does not end with its hold fragment')
  resumed_fragment = hold_fragment(
    resumed_hold, harness='bro', wire='bare', creds=known_credentials
  )
  return f'{system_prompt.removesuffix(suffix)}\n\n{resumed_fragment}'


def _server_side_eligible(
  *,
  forked_from_spec: dict,
  new_spec: LLMSpec,
  fork_step: Step,
  system_prompt_override: Optional[str],
) -> bool:
  # server-side fork hands the prefix to the provider via `previous_response_id`,
  # so it only works when (a) the provider hasn't changed (the response_id
  # belongs to that provider's storage), (b) the model hasn't changed (the
  # response_id pins the model on the server), (c) the fork point is right
  # after an `llm_call` so a response_id actually exists at that point, and
  # (d) the system prompt hasn't been swapped — the cached server-side prefix
  # already carries the prompt and we can't restate it. client-side replay
  # covers every other case.
  if system_prompt_override is not None:
    return False
  if forked_from_spec.get('type') != new_spec.TYPE:
    return False
  if forked_from_spec.get('model') != new_spec.model:
    return False
  if fork_step.kind != 'llm_call':
    return False
  return fork_step.extras.get('response_id') is not None


def _find_step(trail: RecordedTrail, step_id: int) -> Step:
  for step in trail.steps:
    if step.step_id == step_id:
      return step
  raise ValueError(f'step_id {step_id!r} not found in trail {trail.header.id!r}')


def _seed_response_id(inner_llm: LLM, response_id: str) -> None:
  # server-side replay seam: OpenAI.send sends `previous_response_id=...` on
  # its first call when `_last_response_id` is set, and OpenAI carries the
  # entire prefix it had cached for that response. other LLM impls don't
  # support this; raise loudly rather than silently producing a fork that
  # ignores the seed.
  if not isinstance(inner_llm, llm_llms_openai.OpenAI):
    raise NotImplementedError(
      f'server-side fork not implemented for {type(inner_llm).__name__}; '
      'currently supports OpenAI only'
    )
  inner_llm._last_response_id = response_id


def _extract_system_prompt(trail: RecordedTrail) -> str:
  for step in trail.steps:
    if step.kind == 'system_prompt':
      return step.body
  raise ValueError(f'trail {trail.header.id!r} has no system_prompt step')


def _response_output_items(llm_call_body: Any) -> list[dict]:
  if not isinstance(llm_call_body, dict):
    return []
  response = llm_call_body.get('response')
  if not isinstance(response, dict):
    return []
  output = response.get('output', [])
  # recorded items are full Response dumps; as *input* the API rejects the
  # response-only `status` field and null-valued optionals (e.g.
  # `encrypted_content: null` on reasoning items), so strip both. reasoning
  # items replay as-is even cross-model — OpenAI requires a function_call's
  # paired reasoning item when the call carries its id. `compaction` items
  # (emitted when server-side compaction triggered mid-run) are dropped
  # entirely: replay carries the full verbatim history the summary stands for,
  # so keeping the item would duplicate that context in encrypted form.
  return [
    {k: v for k, v in item.items() if k != 'status' and v is not None}
    for item in output
    if isinstance(item, dict) and item.get('type') != 'compaction'
  ]


def _encode_tool_output(output: Any) -> str:
  # mirrors `OpenAI._execute_tool_calls`: dicts go on the wire as JSON; other
  # tool outputs (str, already-stringified) pass through.
  if isinstance(output, dict):
    return json.dumps(output)
  return output if isinstance(output, str) else str(output)


def _preseed(inner_llm: LLM, prefix: list[dict]) -> None:
  # client-side replay seam: OpenAI consumes _input_prefix on its first send().
  # other LLM impls (e.g. Echo) don't support replay; their forks would need a
  # provider-specific shim. raise loudly rather than silently producing a fork
  # that ignores the prefix.
  if not isinstance(inner_llm, llm_llms_openai.OpenAI):
    raise NotImplementedError(
      f'client-side fork not implemented for {type(inner_llm).__name__}; '
      'currently supports OpenAI only'
    )
  # the prefix mixes role-keyed message dicts with raw OpenAI output items —
  # all valid `ResponseInputItemParam` shapes — but list invariance prevents a
  # plain assignment from the loosely-typed dicts pyright sees here.
  inner_llm._input_prefix = cast(Any, list(prefix))
