"""conformance: a summoned session delivers its result through `bro::answer`.

The summoner is blocked on that answer, and a session's reply reaches it no
other way, so a summoned session that ends its turn in plain text holds the
summoner until the session answers. The quest here grants the summoner
`owner.say` and `owner.question`, the talk under which such a turn end once left
both sides waiting until the quest timed out. A summoned session that does end
its turn in plain text, with nothing left to wait on, is told so, and must then
deliver through `bro::answer`."""

import secrets
from pathlib import Path

from bro.llm.providers import bind_recipe
from bro.local.conformance_test_helper import Trail, harness_matrix, run_session_tree

_PROBE = 'bro-watch-probe'
_OWNER_TALK = ('owner.say', 'owner.question')
_UNANSWERED_NOTICE = '[notification: the turn ended without a result]'
_READY = 'READY'


def _tool(message: dict) -> str:
  return message['tool_name'].removeprefix('mcp__')


def _text(content: object) -> str:
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    return '\n'.join(
      block['text'] for block in content if isinstance(block, dict) and 'text' in block
    )
  return ''


def _summon(
  harness: str, recipe: str, tmp_path: Path, data: Path, task: str
) -> tuple[Trail, Trail]:
  """the root's and the child's trails, the root on `harness` under `recipe`
  summoning `_PROBE` on the same with `task`, unboxed under owner talk, and
  answering with the child's answer. The child drops the `trails` kind, as the
  root's launch does, so it records its trail locally too."""
  tree = tmp_path / 'tree'
  tree.mkdir()
  trails = run_session_tree(
    harness=harness,
    recipe=recipe,
    bro=_PROBE,
    prompt=(
      f'Summon `{_PROBE}` with the `bro::summon` tool, passing placement `unboxed`, '
      f'harness `{harness}`, llm `{recipe}`, talk `{_OWNER_TALK[0]}` and `{_OWNER_TALK[1]}`, '
      f'revoke `trails`, and this prompt verbatim: {task!r}. Wait for its answer, '
      'then answer with that answer and nothing else.'
    ),
    tree=tree,
    data=data,
    grants=(f'@{_PROBE}', 'bro.placement=unboxed'),
  )
  roots = [trail for trail in trails if 'summoned_by' not in trail.header]
  children = [trail for trail in trails if 'summoned_by' in trail.header]
  assert len(roots) == 1 and len(children) == 1, [trail.header for trail in trails]
  _assert_launched(roots[0], children[0], harness, recipe)
  return roots[0], children[0]


def _assert_launched(root: Trail, child: Trail, harness: str, recipe: str) -> None:
  """the child is the root's one summon of `_PROBE` under owner talk, and ran on
  `harness` under `recipe`."""
  summons = [
    message
    for message in root.messages
    if message['type'] == 'tool_call' and _tool(message) == 'bro__summon'
  ]
  assert len(summons) == 1, [message['arguments'] for message in summons]
  arguments = summons[0]['arguments']
  assert arguments.get('target') == _PROBE, arguments
  assert set(_OWNER_TALK) <= set(arguments.get('talk') or ()), arguments
  assert child.header['summoned_by']['trail_id'] == root.header['id'], child.header
  assert child.header['harness'] == harness, child.header
  assert child.header['native']['llm'] == bind_recipe(recipe).dump(), child.header


def _answers(child: Trail, token: str) -> list[int]:
  return [
    index
    for index, message in enumerate(child.messages)
    if message['type'] == 'tool_call'
    and _tool(message) == 'bro__answer'
    and token in str(message['arguments'].get('answer', ''))
  ]


def _notices(child: Trail) -> list[int]:
  return [
    index
    for index, message in enumerate(child.messages)
    if message['type'] in ('notification', 'user_input')
    and _UNANSWERED_NOTICE in _text(message.get('content'))
  ]


def _relayed(root: Trail, token: str) -> bool:
  replies = [message for message in root.messages if message['type'] == 'assistant']
  return len(replies) > 0 and token in _text(replies[-1]['content'])


@harness_matrix()
def test_a_summoned_session_delivers_its_result_through_answer(
  harness: str, recipe: str, tmp_path: Path, conformance_data: Path
) -> None:
  token = f'RESULT-{secrets.token_hex(4)}'
  root, child = _summon(
    harness,
    recipe,
    tmp_path,
    conformance_data,
    f'The result of this task is the token {token}. Deliver it.',
  )

  assert len(_answers(child, token)) == 1, [message['type'] for message in child.messages]
  assert _notices(child) == []
  assert _relayed(root, token)


@harness_matrix()
def test_a_summoned_session_told_its_turn_ended_without_a_result_delivers_through_answer(
  harness: str, recipe: str, tmp_path: Path, conformance_data: Path
) -> None:
  token = f'RESULT-{secrets.token_hex(4)}'
  root, child = _summon(
    harness,
    recipe,
    tmp_path,
    conformance_data,
    f'First reply with the word {_READY} as plain text and end your turn without calling '
    f'any tool. After that, deliver the token {token} as the result of this task.',
  )

  ready = [
    index
    for index, message in enumerate(child.messages)
    if message['type'] == 'assistant' and _READY in _text(message['content'])
  ]
  notices = _notices(child)
  answers = _answers(child, token)
  assert len(ready) > 0 and len(notices) == 1 and len(answers) == 1, (ready, notices, answers)
  assert ready[0] < notices[0] < answers[0]
  assert _relayed(root, token)
