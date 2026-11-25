from unittest.mock import Mock

import pytest

import bro.fork as fork_module
from bro.fork import fork, latest_fork_point, replay_prefix
from bro.llm.resolved import ResolvedRecipe
from bro.llm.tracker import RunFork
from bro.native.echo_declaration import Recipe as EchoRecipe
from bro.native.harness import BRO
from bro.native.run_log import read
from bro.native.run_log_test_helper import conversation_log as log, echo_call as call
from bro.openai.declaration import Recipe as OpenAIRecipe
from bro.registry import create_bro


def test_three_log_chain_keeps_exact_bounds_and_no_system_steps(tmp_path):
  first = log(tmp_path / 'first.jsonl')
  first.step('user_input', 'first')
  call(first)
  first.step('user_input', 'excluded')
  second = log(tmp_path / 'second.jsonl', parent=RunFork('first', 2))
  second.step('notification', 'news')
  call(second)
  third = log(tmp_path / 'third.jsonl', parent=RunFork('second', 1))
  third.step('user_input', 'third')
  prefix = replay_prefix(read(third.path), 1)
  assert [step.body for step in prefix.steps if step.kind in ('user_input', 'notification')] == [
    'first',
    'news',
    'third',
  ]
  assert all(step.kind != 'system_prompt' for step in prefix.steps)


@pytest.mark.parametrize(
  'tail, expected', [('none', 1), ('call', 2), ('tool', 4), ('partial_tool', 1), ('stopped', 1)]
)
def test_latest_consistent_point(tmp_path, tail, expected):
  writer = log(tmp_path / 'run.jsonl')
  writer.step('user_input', 'hello')
  if tail == 'call':
    call(writer)
  elif tail == 'tool':
    call(writer, calls=['one', 'two'])
    writer.step('tool_result', 'first result', call_id='one')
    writer.step('tool_result', 'second result', call_id='two')
    call(writer, calls=['unanswered'])
  elif tail == 'partial_tool':
    call(writer, calls=['one', 'two'])
    writer.step('tool_result', 'first result', call_id='one')
  elif tail == 'stopped':
    call(writer, stopped=True)
  assert latest_fork_point(read(writer.path)) == expected


def test_system_prompt_alone_needs_an_ancestor(tmp_path):
  root = log(tmp_path / 'root.jsonl')
  with pytest.raises(ValueError, match='no step to resume'):
    latest_fork_point(read(root.path))
  root.step('user_input', 'hello')
  child = log(tmp_path / 'child.jsonl', parent=RunFork('root', 1))
  assert latest_fork_point(read(child.path)) == 0
  assert replay_prefix(read(child.path), 0).steps[0].body == 'hello'


@pytest.mark.parametrize('ancestor', [False, True])
def test_wire_mismatch_is_refused_before_persona_construction(tmp_path, monkeypatch, ancestor):
  writer = log(tmp_path / 'root.jsonl')
  writer.step('user_input', 'hello')
  call(writer)
  path = writer.path
  if ancestor:
    child = log(
      tmp_path / 'child.jsonl', parent=RunFork('root', 2), recipe=OpenAIRecipe(model='gpt-6.1-sol')
    )
    path = child.path
  monkeypatch.setattr(fork_module, 'create_bro', Mock(side_effect=AssertionError('persona built')))
  with pytest.raises(ValueError, match='echo.*responses.*recorded wire'):
    fork(path, recipe=OpenAIRecipe(model='gpt-6.1-sol'), surface='test')


@pytest.mark.asyncio
async def test_continuation_restores_locally_and_records_its_own_log(tmp_path, monkeypatch):
  writer = log(tmp_path / 'root.jsonl')
  writer.step('user_input', 'earlier')
  call(writer, text='earlier answer')
  writer.end('ok')
  monkeypatch.setattr(
    'bro.trails.store.default_store', Mock(side_effect=AssertionError('store read'))
  )
  from bro.native.echo import Echo

  requests = []
  respond = Echo._respond

  def observe(self, request):
    requests.append(request)
    return respond(self, request)

  monkeypatch.setattr(Echo, '_respond', observe)
  runner = fork(writer.path, surface='call')
  assert runner.resolved_recipe == ResolvedRecipe(EchoRecipe(model='echo'), 'run log root')
  with runner:
    assert await runner.send('next', surface='call') == 'next'
  assert runner.run_log is not None and runner.run_log.parent == tmp_path
  continued = read(runner.run_log)
  assert continued.opened['forked_from'] == {'run': 'root', 'step': 2}
  assert continued.opened['interactive'] is True
  request = next(step.body['request'] for step in continued.steps if step.kind == 'llm_call')
  assert [item['text'] for item in request['input']] == ['next']
  assert requests[0]['input'][1:] == [
    {'role': 'user', 'text': 'earlier'},
    {'role': 'assistant', 'text': 'earlier answer'},
    {'role': 'user', 'text': 'next'},
  ]
  assert continued.prompt == read(writer.path).prompt


def test_new_log_must_be_beside_the_origin(tmp_path):
  writer = log(tmp_path / 'root.jsonl')
  writer.step('user_input', 'hello')
  target = tmp_path / 'elsewhere' / 'child.jsonl'
  with pytest.raises(ValueError, match='beside'):
    fork(writer.path, surface='test', run_log=target)
  assert not target.exists()


@pytest.mark.parametrize(
  'change', ['persona', 'hold', 'summoned', 'no-longer-summoned', 'summoner']
)
def test_prompt_rule_compares_the_session_part(tmp_path, monkeypatch, change):
  bro = create_bro('bro')
  with monkeypatch.context() as initial:
    if change == 'no-longer-summoned':
      initial.setenv('RIDE_SUMMONED', '1')
      initial.setattr('bro.summon.talk', lambda: ())
    original, offset = bro.system_prompt_parts_for(hold='guided', harness=BRO)
  recorded = 'recorded persona\n\n' + original[offset:]
  writer = log(tmp_path / 'root.jsonl', prompt=recorded, session_at=len('recorded persona\n\n'))
  writer.step('user_input', 'hello')
  hold = 'guided'
  if change == 'hold':
    hold = 'unattended'
  elif change == 'summoned':
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    monkeypatch.setattr('bro.summon.talk', lambda: ())
  elif change == 'summoner':
    monkeypatch.setattr('bro.summon.effective_may_summon', lambda: ('bro',))
  runner = fork(writer.path, surface='test', hold=hold)
  assert runner.run_log is not None
  used = read(runner.run_log)
  if change == 'persona':
    assert used.prompt == recorded
  else:
    assert used.prompt != recorded
    assert used.prompt == create_bro('bro').system_prompt_for(hold=hold, harness=BRO)
  expected, expected_offset = create_bro('bro').system_prompt_parts_for(hold=hold, harness=BRO)
  assert used.prompt[used.session_at :] == expected[expected_offset:]
  runner.end()


@pytest.mark.parametrize('reason', ['same', 'model', 'prompt', 'input', 'stopped', 'wire'])
def test_server_chaining_eligibility(tmp_path, monkeypatch, reason):
  recipe = EchoRecipe(model='echo') if reason == 'wire' else OpenAIRecipe(model='gpt-6.1-sol')
  writer = log(tmp_path / 'root.jsonl', recipe=recipe)
  writer.step('user_input', 'hello')
  writer.step(
    'llm_call',
    {'request': {'tools': []}, 'response': {'id': 'response', 'output': []}},
    wire=recipe.WIRE,
    response_id='response',
    **({'stopped': 'failed'} if reason == 'stopped' else {}),
  )
  wire = Mock()
  monkeypatch.setattr('bro.native.runner.Runner._create_llm', lambda self, **kwargs: wire)
  options = {}
  if reason == 'model':
    options['recipe'] = OpenAIRecipe(model='gpt-5.6-terra')
  if reason == 'prompt':
    options['system_prompt'] = 'replacement'
  runner = fork(writer.path, 1 if reason == 'input' else 2, surface='test', **options)
  if reason == 'same':
    wire.seed_chain.assert_called_once_with('response')
    wire.restore.assert_not_called()
  else:
    wire.seed_chain.assert_not_called()
    wire.restore.assert_called_once()
  runner.end()
