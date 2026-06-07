"""client-side forking of recorded trails.

a *fork* replays a parent trail's prefix into a fresh `Bro` and lets the caller
continue with `.send(next_message)`. the new run gets its own trail with
`parent={trail_id, step_id, relationship='fork'}` so the parent → child edge
shows up under the same `parent.trail_id` GSI that sub-bro trails will use.

two replay paths exist in the design: server-side via `previous_response_id`
(cheap, same-provider only) and client-side via full message replay (universal).
this module implements the client-side path; the server-side path lands in
stage 5 once the deployed server is in place.

reasoning-fidelity caveat for OpenAI reasoning models: same-model forks past the
provider's response_id TTL rely on the encrypted reasoning items captured in
each `llm_call.body.response.output` at record time. cross-model forks discard
reasoning by design — a different model wouldn't have used the prior model's
thinking anyway.
"""

import json
from typing import Any, cast

import llm.llms.chat_gpt
from bro.bros.bro import Bro
from bro.registry import create_bro
from llm.llm import LLM, LLMSpec
from llm.tracker import NullTracker, Parent, RecordedTrail, Tracker


def replay_messages(trail: RecordedTrail, up_to_step_id: str) -> list[dict]:
  """walk the trail's steps up to (and including) `up_to_step_id` and rebuild
  the OpenAI input list needed to resume the conversation from that point.

  layout of the returned list:
  - `{'role': 'system', 'content': <prompt>}` at index 0, taken from the
    trail's `system_prompt` step.
  - `{'role': 'user', 'content': <text>}` for each `user_input`.
  - each `llm_call`'s `response.output` items appended in order — these carry
    intact `call_id`s on `function_call` items, which is what makes correct
    replay possible.
  - `{'type': 'function_call_output', 'call_id': ..., 'output': ...}` for each
    `tool_result`. dict outputs are JSON-encoded to match the wire format
    `ChatGPT._execute_tool_calls` uses.

  legal fork points (the caller is responsible for picking one): right after
  the first `user_input`, right after any terminal `llm_call`, right after any
  later `user_input`. forking mid-tool-loop (right after an `llm_call` whose
  outputs include unanswered `function_call`s) produces an input the model
  cannot consume.

  raises `ValueError` if the trail has no `system_prompt` step or if
  `up_to_step_id` does not appear in it.
  """
  system_text = _extract_system_prompt(trail)
  result: list[dict] = [{'role': 'system', 'content': system_text}]
  for step in trail.steps:
    if step.kind == 'system_prompt':
      continue
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
    # reasoning, assistant, tool_call, end, error: not separately appended —
    # the `llm_call` step's `response.output` already carries the canonical
    # output items (reasoning / message / function_call) for that turn.
    if step.step_id == up_to_step_id:
      return result
  raise ValueError(f'step_id {up_to_step_id!r} not found in trail {trail.header.trail_id!r}')


def fork(
  parent_trail: RecordedTrail,
  up_to_step_id: str,
  *,
  llm_spec: LLMSpec | None = None,
  system_prompt: str | None = None,
  record: bool = True,
  tracker: Tracker | None = None,
) -> Bro:
  """spin up a fresh `Bro` preseeded with the parent trail's prefix up to
  `up_to_step_id`. call `.send(next_message)` on the returned bro to continue
  the conversation.

  `llm_spec` defaults to the parent's spec (rehydrated via `LLMSpec.from_dict`);
  pass an override for cross-model / cross-provider forks. `system_prompt`
  defaults to the prompt recorded on the parent (`system_prompt` step body);
  pass an override to fork with a swapped prompt.

  `record=False` pins the new bro to a `NullTracker` — handy for one-shot
  exploration where the fork's trail is not worth keeping. `record=True` (the
  default) uses the explicit `tracker` if given, otherwise the bro's default
  factory (production: `HttpTracker`; tests: `NullTracker` via `conftest.py`).
  """
  bro = create_bro(parent_trail.header.bro)
  spec = llm_spec if llm_spec is not None else LLMSpec.from_dict(parent_trail.header.llm_spec)
  bro.llm_spec = spec

  prefix = replay_messages(parent_trail, up_to_step_id)
  if system_prompt is not None:
    prefix[0] = {'role': 'system', 'content': system_prompt}
  effective_system_prompt = prefix[0]['content']
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
  )
  _preseed(inner_llm, prefix)
  # pre-assigning _llm makes BaseBro.send take the subsequent-call branch on
  # the first .send(next_message) — it ships `[{'role': 'user', 'content': ...}]`
  # straight to the wrapped LLM, which prepends the prefix to the API input.
  bro._llm = inner_llm

  parent = Parent(
    trail_id=parent_trail.header.trail_id,
    step_id=up_to_step_id,
    relationship='fork',
  )
  bro._tracker.start_trail(
    bro=bro.name,
    llm_spec=spec.dump(),
    system_prompt=effective_system_prompt,
    parent=parent,
    interactive=True,
    entry_point='fork',
  )
  return bro


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
  return [item for item in output if isinstance(item, dict)]


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
