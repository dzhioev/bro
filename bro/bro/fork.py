"""forking of recorded trails.

a *fork* spins up a fresh `Bro` preseeded with a parent trail's prefix and
lets the caller continue with `.send(next_message)`. the new run gets its own
trail with `parent={trail_id, step_id, relationship='fork'}` so the parent →
child edge shows up under the same `parent.trail_id` GSI that sub-bro trails
will use.

two replay paths, picked automatically based on the spec / fork-point combo:

- **server-side via ****`previous_response_id`** (cheap, same-provider/model
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

import llm.llms.chat_gpt
from bro.bros.bro import Bro
from bro.registry import create_bro
from llm.llm import LLM, LLMSpec
from llm.tracker import NullTracker, Parent, RecordedTrail, Step, Tracker


def replay_messages(
  trail: RecordedTrail,
  up_to_step_id: str,
  *,
  fetch_parent: Optional[Callable[[str], RecordedTrail]] = None,
) -> list[dict]:
  """walk the trail's steps up to (and including) `up_to_step_id` and rebuild
  the OpenAI input list needed to resume the conversation from that point.

  layout of the returned list:
  - `{'role': 'system', 'content': <prompt>}` at index 0, taken from the
    trail's `system_prompt` step.
  - the ancestor prefix, when the trail is itself a fork: a fork trail's own
    steps carry only the post-fork suffix, so the prefix is rebuilt by
    recursing through `fetch_parent(trail_id)` (each parent replayed up to its
    fork step, its system message dropped — the youngest trail's recorded
    prompt stands for the whole conversation). a fork trail replayed without
    `fetch_parent` raises rather than silently truncating the conversation.
  - `{'role': 'user', 'content': <text>}` for each `user_input`.
  - each `llm_call`'s `response.output` items appended in order — these carry
    intact `call_id`s on `function_call` items, which is what makes correct
    replay possible. `compaction` items are dropped: the replayed verbatim
    history subsumes what they summarize.
  - `{'type': 'function_call_output', 'call_id': ..., 'output': ...}` for each
    `tool_result`. dict outputs are JSON-encoded to match the wire format
    `ChatGPT._execute_tool_calls` uses.

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
  result: list[dict] = [{'role': 'system', 'content': system_text}]
  result.extend(_ancestor_items(trail, fetch_parent))
  for step in trail.steps:
    if step.kind == 'user_input':
      result.append({'role': 'user', 'content': step.body})
    elif step.kind == 'llm_call':
      result.extend(_response_output_items(step.body))
    elif step.kind == 'tool_result':
      result.append(
        {
          'type': 'function_call_output',
          'call_id': step.extras['call_id'],
          'output': _encode_tool_output(step.body),
        }
      )
    # system_prompt, reasoning, assistant, tool_call, end, error: not
    # separately appended — the prompt is already at index 0, and the
    # `llm_call` step's `response.output` carries the canonical output items
    # (reasoning / message / function_call) for its turn.
    if step.step_id == up_to_step_id:
      return result
  raise ValueError(f'step_id {up_to_step_id!r} not found in trail {trail.header.trail_id!r}')


def _ancestor_items(
  trail: RecordedTrail, fetch_parent: Optional[Callable[[str], RecordedTrail]]
) -> list[dict]:
  parent = trail.header.parent
  if parent is None:
    return []
  if fetch_parent is None:
    raise ValueError(
      f'trail {trail.header.trail_id!r} is a fork of {parent.trail_id!r}; '
      'pass fetch_parent to rebuild the ancestor prefix'
    )
  parent_trail = fetch_parent(parent.trail_id)
  # drop the parent's leading system message — the child's own recorded prompt
  # (already at index 0 of the caller's list) stands for the conversation.
  return replay_messages(parent_trail, parent.step_id, fetch_parent=fetch_parent)[1:]


def latest_fork_point(trail: RecordedTrail) -> str:
  """the step id of the newest legal fork point — where a resume continues from.

  walks the steps tracking the turn's unanswered `function_call`s; a step
  qualifies when nothing is pending after it: a `user_input`, an `llm_call`
  with no function calls in its output, or the `tool_result` that answers its
  turn's last call. a trail killed mid-tool-loop thus resumes from the last
  consistent point before the unanswered call. for a fork trail the
  `system_prompt` step qualifies as the floor — an empty continuation still
  resumes, through the ancestor prefix its parent pointer carries.

  raises `ValueError` when the trail has no step to resume from (a parentless
  trail with no user input recorded).
  """
  last_good: Optional[str] = None
  pending: set[str] = set()
  for step in trail.steps:
    if step.kind == 'system_prompt':
      if trail.header.parent is not None and last_good is None:
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
    raise ValueError(f'trail {trail.header.trail_id!r} has no step to resume from')
  return last_good


def fork(
  parent_trail: RecordedTrail,
  up_to_step_id: str,
  *,
  llm_spec: Optional[LLMSpec] = None,
  system_prompt: Optional[str] = None,
  record: bool = True,
  tracker: Optional[Tracker] = None,
  entry_point: str = 'fork',
  fetch_parent: Optional[Callable[[str], RecordedTrail]] = None,
) -> Bro:
  """spin up a fresh `Bro` preseeded with the parent trail's prefix up to
  `up_to_step_id`. call `.send(next_message)` on the returned bro to continue
  the conversation.

  `llm_spec` defaults to the parent's spec (rehydrated via `LLMSpec.from_dict`);
  pass an override for cross-model / cross-provider forks. `system_prompt`
  defaults to the prompt recorded on the parent (`system_prompt` step body);
  pass an override to fork with a swapped prompt.

  the replay path is picked automatically: same-provider + same-model + fork
  at an `llm_call` step + no `system_prompt` override → server-side via
  `previous_response_id`. otherwise → client-side message replay, which needs
  `fetch_parent` when the parent trail is itself a fork (see
  `replay_messages`).

  `record=False` pins the new bro to a `NullTracker` — handy for one-shot
  exploration where the fork's trail is not worth keeping. `record=True` (the
  default) uses the explicit `tracker` if given, otherwise the bro's default
  factory (production: `HTTPTracker`; tests: `NullTracker` via `conftest.py`).
  `entry_point` labels the new trail's header — the default suits the generic
  `trails fork`; a surface with its own resume flow passes its own label
  (`call --resume` passes 'call' so the continuation is itself resumable).
  """
  bro = create_bro(parent_trail.header.bro)
  spec = llm_spec if llm_spec is not None else LLMSpec.from_dict(parent_trail.header.llm_spec)
  bro.llm_spec = spec

  fork_step = _find_step(parent_trail, up_to_step_id)
  use_server_side = _server_side_eligible(
    parent_spec=parent_trail.header.llm_spec,
    new_spec=spec,
    fork_step=fork_step,
    system_prompt_override=system_prompt,
  )
  parent_system_prompt = _extract_system_prompt(parent_trail)
  effective_system_prompt = system_prompt if system_prompt is not None else parent_system_prompt
  # `bro.system_prompt` is the raw prompt without the interactive-mode note;
  # forks reuse the parent's exact text and skip BaseBro's note-appending path
  # (the parent's prompt — recorded in the trail — already has the note baked
  # in from its original run).
  bro.system_prompt = effective_system_prompt

  bro._tracker = (
    NullTracker() if not record else (tracker if tracker is not None else bro._make_tracker())
  )
  bro._observer = bro._make_observer()
  inner_llm = spec.create_llm(
    mcp_servers=bro._mcp_servers_for(interactive=True),
    observer=bro._observer,
    tracker=bro._tracker,
    agent=bro.agent,
  )
  if use_server_side:
    _seed_response_id(inner_llm, fork_step.extras['response_id'])
  else:
    prefix = replay_messages(parent_trail, up_to_step_id, fetch_parent=fetch_parent)
    if system_prompt is not None:
      prefix[0] = {'role': 'system', 'content': system_prompt}
    _preseed(inner_llm, prefix)
  # pre-assigning _llm makes BaseBro.send take the subsequent-call branch on
  # the first .send(next_message) — it ships `[{'role': 'user', 'content': ...}]`
  # straight to the wrapped LLM, which either prepends the replayed prefix
  # (client-side) or passes `previous_response_id=<seeded>` (server-side).
  bro._llm = inner_llm

  parent = Parent(
    trail_id=parent_trail.header.trail_id,
    step_id=up_to_step_id,
    relationship='fork',
  )
  trail_id = bro._tracker.start_trail(
    bro=bro.name,
    llm_spec=spec.dump(),
    system_prompt=effective_system_prompt,
    parent=parent,
    interactive=True,
    entry_point=entry_point,
  )
  bro.trail_id = trail_id if len(trail_id) > 0 else None
  return bro


def _server_side_eligible(
  *,
  parent_spec: dict,
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
  if parent_spec.get('type') != new_spec.TYPE:
    return False
  if parent_spec.get('model') != new_spec.model:
    return False
  if fork_step.kind != 'llm_call':
    return False
  return fork_step.extras.get('response_id') is not None


def _find_step(trail: RecordedTrail, step_id: str) -> Step:
  for step in trail.steps:
    if step.step_id == step_id:
      return step
  raise ValueError(f'step_id {step_id!r} not found in trail {trail.header.trail_id!r}')


def _seed_response_id(inner_llm: LLM, response_id: str) -> None:
  # server-side replay seam: ChatGPT.send sends `previous_response_id=...` on
  # its first call when `_last_response_id` is set, and OpenAI carries the
  # entire prefix it had cached for that response. other LLM impls don't
  # support this; raise loudly rather than silently producing a fork that
  # ignores the seed.
  if not isinstance(inner_llm, llm.llms.chat_gpt.ChatGPT):
    raise NotImplementedError(
      f'server-side fork not implemented for {type(inner_llm).__name__}; '
      'currently supports ChatGPT only'
    )
  inner_llm._last_response_id = response_id


def _extract_system_prompt(trail: RecordedTrail) -> str:
  for step in trail.steps:
    if step.kind == 'system_prompt':
      return step.body
  raise ValueError(f'trail {trail.header.trail_id!r} has no system_prompt step')


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
  # mirrors `ChatGPT._execute_tool_calls`: dicts go on the wire as JSON; other
  # tool outputs (str, already-stringified) pass through.
  if isinstance(output, dict):
    return json.dumps(output)
  return output if isinstance(output, str) else str(output)


def _preseed(inner_llm: LLM, prefix: list[dict]) -> None:
  # client-side replay seam: ChatGPT consumes _input_prefix on its first send().
  # other LLM impls (e.g. Echo) don't support replay; their forks would need a
  # provider-specific shim. raise loudly rather than silently producing a fork
  # that ignores the prefix.
  if not isinstance(inner_llm, llm.llms.chat_gpt.ChatGPT):
    raise NotImplementedError(
      f'client-side fork not implemented for {type(inner_llm).__name__}; '
      'currently supports ChatGPT only'
    )
  # the prefix mixes role-keyed message dicts with raw OpenAI output items —
  # all valid `ResponseInputItemParam` shapes — but list invariance prevents a
  # plain assignment from the loosely-typed dicts pyright sees here.
  inner_llm._input_prefix = cast(Any, list(prefix))
