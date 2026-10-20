import asyncio
import json
from unittest.mock import MagicMock

import pytest

from bro import quest
from bro.artifact import SHARE
from bro.broker import brotocol
from bro.broker.environment import BROKER_CHANNEL, BROKER_MISSION, BROKER_TALK
from bro.broker.journal import MAX_MESSAGE_BYTES
from bro.broker.journal_test_helper import text_at_the_message_bound
from bro.quest_test_helper import (
  entry,
  next_message,
  quest_record,
  reply,
  running_server,
)

REF = f'sha256:{"a" * 64}/report.md'


def test_help_lists_every_verb(capsys):
  with pytest.raises(SystemExit):
    quest.main(['quest', '--help'])
  output = capsys.readouterr().out
  for verb in ('check', 'history', 'say', 'ask', 'list', 'cancel'):
    assert f'\n    {verb} ' in output or f' {verb} ' in output

  with pytest.raises(SystemExit):
    quest.main(['quest', 'check', '--help'])
  output = capsys.readouterr().out
  assert '--wait' in output
  assert 'self' in output


def test_a_missing_verb_prints_help_and_fails(capsys):
  assert quest.main(['quest']) == 1
  assert 'check' in capsys.readouterr().err


def test_say_takes_no_question_flags():
  with pytest.raises(SystemExit):
    quest.main(['quest', 'say', 'REQ-1', 'text', '--wait'])
  with pytest.raises(SystemExit):
    quest.main(['quest', 'say', 'REQ-1', 'text', '--question'])


# --- say and ask ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_say_to_a_child_checks_talk_then_sends(monkeypatch):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'say', 'REQ-1', 'steer left'])
    )
    channel, query = await next_message(server)
    assert query.args == {'id': 'REQ-1'}
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started', talk=['owner.say', 'worker.say'])},
    )
    _, message = await next_message(server)
    assert message.type == brotocol.Tag.MESSAGE
    assert message.request_id == 'REQ-1'
    assert message.payload == {'text': 'steer left'}
    assert message.id is None
    assert await task == 0


@pytest.mark.asyncio
async def test_say_on_self_uses_the_session_quest_and_published_talk(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'OWN-QUEST')
    monkeypatch.setenv(BROKER_TALK, 'worker.say')
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'say', 'self', 'progress']))
    channel, query = await next_message(server)
    assert query.args == {'id': 'OWN-QUEST'}
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('OWN-QUEST', 'started', talk=['worker.say'])},
    )
    _, message = await next_message(server)
    assert message.request_id == 'OWN-QUEST'
    assert message.payload == {'text': 'progress'}
    assert await task == 0


@pytest.mark.asyncio
async def test_ask_prints_the_question_id_without_waiting(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'ask', 'REQ-1', 'ready?']))
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', talk=['owner.question', 'worker.say'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    _, question = await next_message(server)

    assert question.id is not None
    assert await task == 0
    assert capsys.readouterr().out == f'{question.id}\n'


@pytest.mark.asyncio
async def test_ask_wait_prints_the_reply(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'ask', 'REQ-1', 'ready?', '--wait', '5'])
    )
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', talk=['owner.question', 'worker.say'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    _, question = await next_message(server)
    assert question.id is not None
    asked = entry(1, 'owner', 'ready?', id=question.id, pending=True)
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': {**live, 'messages': [asked], 'chat_seq': 1}},
    )
    channel, waiting = await next_message(server)
    assert waiting.args['since'] == 1
    answered = entry(2, 'worker', 'yes', reply_to=question.id)
    await reply(
      server,
      channel,
      waiting,
      outcome='ok',
      value={'mission': {**live, 'messages': [asked, answered], 'chat_seq': 2}},
    )

    assert await task == 0
    assert capsys.readouterr().out == 'yes\n'


@pytest.mark.asyncio
async def test_ask_wait_reports_a_counter_question_and_its_id(monkeypatch, capsys, caplog):
  caplog.set_level('INFO')
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'ask', 'REQ-1', 'ready?', '--wait', '5'])
    )
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', talk=['owner.question', 'worker.question'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    _, question = await next_message(server)
    assert question.id is not None
    asked = entry(1, 'owner', 'ready?', id=question.id, pending=True)
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': {**live, 'messages': [asked], 'chat_seq': 1}},
    )
    channel, waiting = await next_message(server)
    counter_question = entry(
      2,
      'worker',
      'Which environment?',
      id='COUNTER-1',
      reply_to=question.id,
      pending=True,
    )
    await reply(
      server,
      channel,
      waiting,
      outcome='ok',
      value={'mission': {**live, 'messages': [asked, counter_question], 'chat_seq': 2}},
    )

    assert await task == quest.QUESTION_EXIT_CODE
    assert capsys.readouterr().out == 'Which environment?\n'
    assert 'counter_question_id COUNTER-1' in caplog.text
    assert "quest ask REQ-1 '<answer>' --reply-to COUNTER-1 --wait" in caplog.text


@pytest.mark.asyncio
async def test_ask_wait_timeout_returns_its_recoverable_id(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'ask', 'REQ-1', 'ready?', '--wait', '0.1'])
    )
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', talk=['owner.question', 'worker.say'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    _, question = await next_message(server)
    assert question.id is not None
    channel, query = await next_message(server)
    asked = entry(1, 'owner', 'ready?', id=question.id, pending=True)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': {**live, 'messages': [asked], 'chat_seq': 1}},
    )
    _, waiting = await next_message(server)
    assert waiting.args['since'] == 1

    assert await task == quest.RUNNING_EXIT_CODE
    assert capsys.readouterr().out == f'{question.id}\n'


@pytest.mark.asyncio
async def test_share_hands_a_ref_to_a_live_bro_quest(monkeypatch):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.share, 'REQ-1', REF))
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started')},
    )
    channel, request = await next_message(server)
    assert request.kind == SHARE
    assert request.args == {'id': 'REQ-1', 'ref': REF}
    await reply(server, channel, request, outcome='ok', value={})

    assert await task == 'REQ-1'


@pytest.mark.asyncio
async def test_waiting_question_fails_on_its_correlated_host_refusal(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'ask', 'REQ-1', 'ready?', '--wait', '1'])
    )
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', talk=['owner.question', 'worker.say'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    _, question = await next_message(server)
    assert question.id is not None
    refused = entry(
      1,
      'owner',
      'ready?',
      id=question.id,
      transition='refused',
      reason='host refused the move',
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': {**live, 'messages': [refused], 'chat_seq': 1}},
    )

    assert await task == 1
    assert 'host refused the move' in caplog.text


@pytest.mark.asyncio
async def test_waiting_question_fails_when_the_quest_ends_without_a_reply(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'ask', 'REQ-1', 'ready?', '--wait', '1'])
    )
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', talk=['owner.question', 'worker.say'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    _, question = await next_message(server)
    assert question.id is not None
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'REQ-1',
          'ended',
          result={'outcome': 'ok', 'value': 'child stopped'},
          talk=['owner.question', 'worker.say'],
        )
      },
    )

    assert await task == 1
    assert 'already ended' in caplog.text


@pytest.mark.asyncio
async def test_say_refuses_an_ended_quest_before_sending(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'say', 'REQ-1', 'too late']))
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'REQ-1',
          'ended',
          result={'outcome': 'ok', 'value': 'done'},
          talk=['owner.say', 'worker.say'],
        )
      },
    )

    assert await task == 1
    assert 'already ended' in caplog.text


@pytest.mark.asyncio
async def test_say_reports_an_oversized_reply_id_as_a_cli_error(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        quest.main,
        [
          'quest',
          'say',
          'REQ-1',
          'reply',
          '--reply-to',
          'x' * (brotocol.MAX_IDENTIFIER_BYTES + 1),
        ],
      )
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started', talk=['worker.question', 'worker.say'])},
    )

    assert await task == 1
    assert 'over' in caplog.text


@pytest.mark.asyncio
async def test_say_refuses_a_quest_this_session_did_not_summon(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'say', 'GRANDCHILD', 'skip a level'])
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'GRANDCHILD', 'started', talk=['owner.say', 'worker.say'], parent='CHILD'
        )
      },
    )

    assert await task == 1
    assert 'not one this session owns or undertakes' in caplog.text


@pytest.mark.asyncio
async def test_say_refuses_a_move_the_child_quest_talk_forbids(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'say', 'REQ-1', 'not allowed'])
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started', talk=['worker.say'])},
    )
    assert await task == 1
    assert 'query REQ-1 forbids' in caplog.text


@pytest.mark.asyncio
async def test_say_sends_text_at_the_message_bound_whole(monkeypatch):
  text = text_at_the_message_bound()
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'say', 'REQ-1', text]))
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started', talk=['owner.say', 'worker.say'])},
    )
    _, message = await next_message(server)
    assert message.payload == {'text': text}
    assert await task == 0


@pytest.mark.parametrize('verb', ['say', 'ask'])
def test_text_over_the_message_bound_is_refused_before_any_broker_traffic(
  monkeypatch, caplog, verb
):
  monkeypatch.delenv(BROKER_CHANNEL, raising=False)
  text = text_at_the_message_bound() + 'x'

  assert quest.main(['quest', verb, 'REQ-1', text]) == 1

  assert f'{MAX_MESSAGE_BYTES + 1} bytes' in caplog.text
  assert 'mint an artifact' in caplog.text


# --- check ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_reports_a_running_quest_with_its_trail(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'check', 'REQ-1']))
    channel, query = await next_message(server)
    assert query.kind == 'query'
    assert query.args == {'id': 'REQ-1'}
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started', trail_id='T9')},
    )

    assert await task == quest.RUNNING_EXIT_CODE
    assert json.loads(capsys.readouterr().out) == {
      'state': 'running',
      'quest_id': 'REQ-1',
      'trail_id': 'T9',
    }
    assert 'still running' in caplog.text
    assert 'T9' in caplog.text


def test_check_refuses_the_sessions_own_quest(monkeypatch, caplog):
  monkeypatch.setenv(BROKER_MISSION, 'OWN-QUEST')
  monkeypatch.setenv(BROKER_CHANNEL, 'tcp://token@127.0.0.1:1')

  assert quest.main(['quest', 'check', 'self']) == 1
  assert quest.main(['quest', 'check', 'OWN-QUEST']) == 1
  assert caplog.text.count('cannot check the quest it answers') == 2


@pytest.mark.asyncio
async def test_check_reports_the_open_questions_a_child_is_stalled_on(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'check', 'REQ-1']))
    channel, query = await next_message(server)
    answered = entry(1, 'worker', 'which branch?', id='QUESTION-1')
    answer = entry(2, 'owner', 'master', reply_to='QUESTION-1')
    open_question = entry(3, 'worker', 'approve?', id='QUESTION-2', pending=True)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'REQ-1',
          'started',
          trail_id='T9',
          talk=['owner.question', 'worker.question', 'worker.say'],
          messages=[answered, answer, open_question],
          chat_seq=3,
        )
      },
    )

    assert await task == quest.QUESTION_EXIT_CODE
    assert json.loads(capsys.readouterr().out) == {
      'state': 'question',
      'quest_id': 'REQ-1',
      'trail_id': 'T9',
      'questions': [{'id': 'QUESTION-2', 'text': 'approve?'}],
    }


@pytest.mark.asyncio
async def test_check_wait_returns_early_when_the_child_asks(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'check', '--wait', '--timeout', '5', 'REQ-1'])
    )
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', talk=['worker.question', 'worker.say'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    channel, waiting = await next_message(server)
    assert waiting.args['id'] == 'REQ-1'
    assert waiting.args['since'] == 0
    question = entry(1, 'worker', 'approve?', id='QUESTION-1', pending=True)
    await reply(
      server,
      channel,
      waiting,
      outcome='ok',
      value={'mission': {**live, 'messages': [question], 'chat_seq': 1}},
    )

    assert await task == quest.QUESTION_EXIT_CODE
    assert json.loads(capsys.readouterr().out)['questions'] == [
      {'id': 'QUESTION-1', 'text': 'approve?'}
    ]


@pytest.mark.asyncio
async def test_check_returns_the_retained_answer_repeatably(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    for _ in range(2):
      task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'check', 'REQ-1']))
      channel, query = await next_message(server)
      await reply(
        server,
        channel,
        query,
        outcome='ok',
        value={
          'mission': quest_record(
            'REQ-1', 'ended', result={'outcome': 'ok', 'value': 'retained answer'}
          )
        },
      )
      assert await task == 0
    assert capsys.readouterr().out == 'retained answer\nretained answer\n'


def test_wait_deadline_returns_running_without_starting_another_poll(monkeypatch):
  monkeypatch.setenv(BROKER_MISSION, 'ROOT')
  ticks = iter((10.0, 10.0, 15.0))
  polls: list[tuple[float, float | None]] = []

  monkeypatch.setattr(quest.time, 'monotonic', lambda: next(ticks))

  def query(client, quest_id, *, wait_seconds=0, since=None, read_timeout=None):
    del client
    polls.append((wait_seconds, read_timeout))
    return quest_record(quest_id, 'started', trail_id='T9')

  monkeypatch.setattr(quest, 'query_quest', query)
  outcome = quest.check('REQ-1', wait=True, timeout=5, client=MagicMock())

  assert outcome == quest.Outcome('REQ-1', trail_id='T9')
  assert outcome.state == 'running'
  assert polls == [(0, 5)]


def test_wait_deadline_bounds_a_stalled_broker_read(monkeypatch):
  monkeypatch.setenv(BROKER_MISSION, 'ROOT')
  ticks = iter((10.0, 10.0))
  client = MagicMock()
  client.call.side_effect = TimeoutError

  monkeypatch.setattr(quest.time, 'monotonic', lambda: next(ticks))

  with pytest.raises(quest.QuestError, match="no reply to broker 'query' request within 5s"):
    quest.check('REQ-1', wait=True, timeout=5, client=client)

  client.call.assert_called_once_with('query', {'id': 'REQ-1'}, 5)


@pytest.mark.parametrize('timeout', [float('nan'), float('inf')])
def test_wait_rejects_non_finite_deadlines(monkeypatch, timeout):
  monkeypatch.setenv(BROKER_MISSION, 'ROOT')
  with pytest.raises(ValueError, match='finite positive'):
    quest.check('REQ-1', wait=True, timeout=timeout, client=MagicMock())
  with pytest.raises(ValueError, match='finite positive'):
    quest.history('REQ-1', wait=True, timeout=timeout, client=MagicMock())


def test_a_timeout_without_a_wait_is_refused(monkeypatch, caplog):
  monkeypatch.setenv(BROKER_MISSION, 'ROOT')
  assert quest.main(['quest', 'check', '--timeout', '5', 'REQ-1']) == 1
  assert quest.main(['quest', 'history', '--timeout', '5', 'REQ-1']) == 1
  assert caplog.text.count('only bounds a wait') == 2


def test_check_wait_exits_running_when_its_deadline_passes(monkeypatch, caplog):
  monkeypatch.setattr(
    quest,
    'check',
    lambda quest_id, *, wait=False, timeout=None: quest.Outcome(quest_id, trail_id='T9'),
  )

  assert quest._check('REQ-1', wait=True, timeout=5) == quest.RUNNING_EXIT_CODE
  assert 'still running' in caplog.text
  assert 'T9' in caplog.text


@pytest.mark.asyncio
async def test_check_wait_deadline_returns_running_when_the_final_read_times_out(
  monkeypatch, caplog
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'check', '--wait', '--timeout', '0.2', 'REQ-1'])
    )
    channel, initial = await next_message(server)
    assert initial.args == {'id': 'REQ-1'}
    await reply(
      server,
      channel,
      initial,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started', trail_id='T9')},
    )

    _, final = await next_message(server)
    assert final.args['id'] == 'REQ-1'
    assert 0 < final.args['wait'] <= 0.2

    assert await task == quest.RUNNING_EXIT_CODE
    assert 'still running' in caplog.text
    assert 'T9' in caplog.text


@pytest.mark.asyncio
async def test_check_wait_loops_query_until_terminal(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'check', '--wait', '--timeout', '1', 'REQ-1'])
    )
    channel, query = await next_message(server)
    assert query.args == {'id': 'REQ-1'}
    await reply(
      server, channel, query, outcome='ok', value={'mission': quest_record('REQ-1', 'started')}
    )
    channel, query = await next_message(server)
    assert query.args['id'] == 'REQ-1'
    assert 0 < query.args['wait'] <= 1
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'ended', result={'outcome': 'ok', 'value': 'done'})},
    )

    assert await task == 0
    assert capsys.readouterr().out == 'done\n'


@pytest.mark.asyncio
async def test_unknown_and_evicted_checks_fail(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    unknown = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'check', 'UNKNOWN']))
    channel, query = await next_message(server)
    await reply(server, channel, query, outcome='denied', error="unknown quest id 'UNKNOWN'")
    assert await unknown == 1

    evicted = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'check', 'OLD']))
    channel, query = await next_message(server)
    await reply(
      server, channel, query, outcome='ok', value={'mission': quest_record('OLD', 'evicted')}
    )
    assert await evicted == 1
  assert 'unknown quest id' in caplog.text
  assert 'no longer retained' in caplog.text


# --- history ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_of_self_marks_the_summoners_open_question(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'OWN-QUEST')
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'history', 'self']))
    channel, query = await next_message(server)
    assert query.args == {'id': 'OWN-QUEST'}
    steer = entry(1, 'owner', 'start with docs')
    question = entry(2, 'owner', 'which region?', id='QUESTION-2', pending=True)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'OWN-QUEST',
          'started',
          talk=['owner.question', 'owner.say', 'worker.say'],
          messages=[steer, question],
          chat_seq=2,
        )
      },
    )

    assert await task == quest.QUESTION_EXIT_CODE
    assert json.loads(capsys.readouterr().out) == {
      'quest_id': 'OWN-QUEST',
      'talk': ['owner.question', 'owner.say', 'worker.say'],
      'messages': [steer, question],
    }


@pytest.mark.asyncio
async def test_history_shows_a_truncated_tail_and_the_recovered_reply(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'history', 'REQ-1']))
    channel, query = await next_message(server)
    answer = entry(3, 'worker', 'yes', reply_to='QUESTION-1')
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'REQ-1',
          'started',
          talk=['owner.question', 'worker.say'],
          messages=[answer],
          messages_truncated=True,
          chat_seq=3,
        )
      },
    )

    assert await task == 0
    output = json.loads(capsys.readouterr().out)
    assert output['messages'] == [answer]
    assert output['truncated'] is True


@pytest.mark.asyncio
async def test_history_surfaces_a_message_sent_past_the_local_guard_and_refused_by_the_host(
  monkeypatch, capsys
):
  async with running_server(monkeypatch) as server:

    def send_anyway() -> None:
      with quest.open_client() as client:
        client.message('REQ-1', {'text': 'blocked'})

    sending = asyncio.create_task(asyncio.to_thread(send_anyway))
    _, message = await next_message(server)
    assert message.type == brotocol.Tag.MESSAGE
    await sending

    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'history', 'REQ-1']))
    channel, query = await next_message(server)
    refused = entry(
      4, 'owner', 'blocked', transition='refused', reason='quest talk lacks owner.say'
    )
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'REQ-1', 'started', talk=['worker.say'], messages=[refused], chat_seq=4
        )
      },
    )

    assert await task == 0
    assert json.loads(capsys.readouterr().out)['messages'] == [refused]


@pytest.mark.asyncio
async def test_history_wait_returns_on_the_next_message(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'history', '--wait', '--timeout', '5', 'REQ-1'])
    )
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', talk=['worker.say'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    channel, waiting = await next_message(server)
    assert waiting.args == {'id': 'REQ-1', 'wait': waiting.args['wait'], 'since': 0}
    said = entry(1, 'worker', 'working')
    await reply(
      server,
      channel,
      waiting,
      outcome='ok',
      value={'mission': {**live, 'messages': [said], 'chat_seq': 1}},
    )

    assert await task == 0
    assert json.loads(capsys.readouterr().out)['messages'] == [said]


@pytest.mark.asyncio
async def test_history_wait_returns_at_once_on_an_ended_quest(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'history', '--wait', 'REQ-1'])
    )
    channel, query = await next_message(server)
    said = entry(1, 'worker', 'done')
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'REQ-1',
          'ended',
          result={'outcome': 'ok', 'value': 'answer'},
          talk=['worker.say'],
          messages=[said],
          chat_seq=1,
        )
      },
    )

    assert await task == 0
    assert json.loads(capsys.readouterr().out)['messages'] == [said]


@pytest.mark.asyncio
async def test_history_of_an_evicted_quest_fails(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'history', 'OLD']))
    channel, query = await next_message(server)
    await reply(
      server, channel, query, outcome='ok', value={'mission': quest_record('OLD', 'evicted')}
    )
    assert await task == 1
  assert 'no longer retained' in caplog.text


# --- cancel -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_refuses_a_non_bro_mission_before_sending(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'cancel', 'REQ-1']))
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started', type='benchmark')},
    )

    assert await task == 1
  assert 'not a bro launch' in caplog.text


@pytest.mark.asyncio
async def test_cancel_checks_the_bro_quest_then_waits_for_its_end(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'cancel', 'REQ-1']))
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started', trail_id='T9')
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    channel, cancel = await next_message(server)
    assert cancel.kind == 'cancel'
    await reply(server, channel, cancel, outcome='ok')
    channel, query = await next_message(server)
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    channel, wait = await next_message(server)
    assert wait.args == {'id': 'REQ-1', 'wait': quest.READ_WAIT_SECONDS}
    await reply(
      server,
      channel,
      wait,
      outcome='ok',
      value={
        'mission': quest_record(
          'REQ-1',
          'ended',
          outcome='failed',
          reason='cancelled',
          trail_id='T9',
          result={'outcome': 'failed', 'detail': {'reason': 'cancelled'}},
        )
      },
    )

    assert await task == 0
  assert 'quest REQ-1 ended failed:cancelled' in caplog.text


@pytest.mark.asyncio
async def test_cancel_refusal_fails_without_waiting(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'cancel', 'REQ-1']))
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('REQ-1', 'started')},
    )
    channel, cancel = await next_message(server)
    await reply(server, channel, cancel, outcome='denied', error='cancel refused')

    assert await task == 1
  assert 'cancel refused' in caplog.text


@pytest.mark.asyncio
async def test_cancel_timeout_keeps_the_quest_recoverable(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', 'cancel', 'REQ-1', '--timeout', '0.2'])
    )
    channel, query = await next_message(server)
    live = quest_record('REQ-1', 'started')
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    channel, cancel = await next_message(server)
    await reply(server, channel, cancel, outcome='ok')
    channel, query = await next_message(server)
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    _, wait = await next_message(server)
    assert 0 < wait.args['wait'] <= 0.2

    assert await task == quest.RUNNING_EXIT_CODE
  assert 'cancel accepted; quest REQ-1 has not ended' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ('verb', 'arguments'),
  [
    ('check', []),
    ('history', []),
    ('say', ['text']),
    ('ask', ['question']),
  ],
)
async def test_every_conversation_verb_refuses_a_non_bro_mission(
  monkeypatch, caplog, verb, arguments
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(quest.main, ['quest', verb, 'OTHER-1', *arguments])
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('OTHER-1', 'started', type='benchmark')},
    )

    assert await task == 1
  assert 'not a bro launch' in caplog.text


# --- list -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_missions_keeps_every_owned_unended_launch(monkeypatch):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.live_missions))
    channel, query = await next_message(server)
    assert query.args == {}
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'missions': [
          quest_record('S2', 'accepted', args={'target': 'reviewer', 'prompt': 'review'}),
          quest_record('S1', 'started'),
          quest_record('G1', 'started', parent='S1'),
          quest_record('W1', 'started', type='test'),
          quest_record('S0', 'ended', result={'outcome': 'ok', 'value': 'done'}),
          quest_record('D0', 'denied', result={'outcome': 'denied', 'error': 'no'}),
        ]
      },
    )

    assert await task == [
      quest.LiveMission('S2', 'bro', 'reviewer'),
      quest.LiveMission('S1', 'bro', 'dev'),
      quest.LiveMission('W1', 'test', 'test'),
    ]


@pytest.mark.asyncio
async def test_live_missions_refuses_an_unknown_mission_state(monkeypatch):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.live_missions))
    channel, query = await next_message(server)
    await reply(
      server, channel, query, outcome='ok', value={'missions': [quest_record('S1', 'limbo')]}
    )

    with pytest.raises(quest.QuestError, match="unknown state 'limbo'"):
      await task


@pytest.mark.asyncio
async def test_live_missions_refuses_a_live_bro_without_a_target(monkeypatch):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.live_missions))
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'missions': [quest_record('S1', 'started', args={})]},
    )

    with pytest.raises(quest.QuestError, match='bro mission without a target'):
      await task


@pytest.mark.asyncio
async def test_list_reads_every_page_and_keeps_only_summons(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(quest.main, ['quest', 'list']))
    channel, query = await next_message(server)
    assert query.args == {}
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'missions': [
          quest_record('ROOT', 'started', kind='root'),
          quest_record('S1', 'started'),
        ],
        'cursor': 'NEXT',
      },
    )
    channel, query = await next_message(server)
    assert query.args == {'cursor': 'NEXT'}
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'missions': [
          quest_record('W1', 'ended', type='test'),
          quest_record('S0', 'denied', result={'outcome': 'denied', 'error': 'no'}),
        ]
      },
    )

    assert await task == 0
    assert [record['id'] for record in json.loads(capsys.readouterr().out)['quests']] == [
      'S1',
      'S0',
    ]


# --- errors -----------------------------------------------------------------------


def test_errors_without_a_channel(monkeypatch, caplog):
  monkeypatch.delenv(BROKER_CHANNEL, raising=False)
  monkeypatch.setenv(BROKER_MISSION, 'ROOT')
  assert quest.main(['quest', 'check', 'SOME-ID']) == 1
  assert quest.main(['quest', 'history', 'self']) == 1
  assert quest.main(['quest', 'list']) == 1
  assert BROKER_CHANNEL in caplog.text


def test_a_failure_before_any_trail_does_not_point_at_trails():
  payload = {
    'outcome': 'failed',
    'error': None,
    'detail': {'reason': 'exit', 'exit_code': 1, 'output_tail': 'recorder: no state dir\n'},
  }
  with pytest.raises(quest.QuestError) as raised:
    quest.interpret_result(payload, None)
  message = str(raised.value)
  assert message.startswith('summon failed (exit); recorder: no state dir; ')
  assert 'announced no trail' in message
  assert 'rewind' not in message


def test_a_failure_with_a_trail_points_at_it():
  payload = {'outcome': 'failed', 'error': None, 'detail': {'reason': 'exit', 'exit_code': 1}}
  with pytest.raises(quest.QuestError, match='summon failed \\(exit\\); .*rewind show T1'):
    quest.interpret_result(payload, 'T1')
