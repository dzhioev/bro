import asyncio
import contextlib
import json
from unittest.mock import MagicMock

import pytest

from bro import quest, summon
from bro.broker import brotocol
from bro.broker.client import Client
from bro.broker.environment import BROKER_CHANNEL, BROKER_MISSION, BROKER_TALK
from bro.broker.journal import MAX_MESSAGE_BYTES
from bro.broker.journal_test_helper import text_at_the_message_bound
from bro.broker.transport import connect
from bro.broker.transports.tcp import LOCAL_HOST
from bro.quest_test_helper import (
  TIMEOUT,
  entry,
  next_message,
  quest_record,
  reply,
  running_live_broker,
  running_server,
)


async def _reply_empty_watch_replay(server) -> None:
  channel, own_query = await next_message(server)
  assert own_query.args == {'id': 'ROOT'}
  await reply(
    server,
    channel,
    own_query,
    outcome='ok',
    value={'mission': quest_record('ROOT', 'started', kind='root')},
  )
  channel, listing = await next_message(server)
  assert listing.args == {}
  await reply(server, channel, listing, outcome='ok', value={'missions': []})


def test_help_lists_every_verb(capsys):
  with pytest.raises(SystemExit):
    quest.main(['quest', '--help'])
  output = capsys.readouterr().out
  for verb in ('check', 'history', 'say', 'ask', 'list', 'watch', 'cancel'):
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

    assert await task == quest.QUESTION_EXIT_CODE
    assert capsys.readouterr().out == f'{question.id}\n'


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


# --- watch ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_arm_replays_live_broker_chat_and_streams_a_racing_message_once(monkeypatch):
  async with running_live_broker() as (spawner, root_endpoint):
    with contextlib.ExitStack() as clients:
      root = clients.enter_context(
        Client(connect(root_endpoint.channel.host_endpoint.address(LOCAL_HOST)))
      )
      monkeypatch.setenv(BROKER_MISSION, root_endpoint.quest)
      monkeypatch.setenv(BROKER_TALK, brotocol.encode_talk(root_endpoint.talk))
      child_request = root.send(
        quest.LAUNCH,
        {
          'target': 'dev',
          'prompt': 'work',
          'talk': ['owner.say', 'owner.question', 'worker.question'],
        },
      )
      summon._await_acceptance(root, child_request)
      child_endpoint = await asyncio.to_thread(spawner.spawned.get, True, TIMEOUT)
      root.message(child_endpoint.quest, {'text': 'start with docs'})

      child = clients.enter_context(
        Client(connect(child_endpoint.channel.host_endpoint.address(LOCAL_HOST)))
      )
      monkeypatch.setenv(BROKER_MISSION, child_endpoint.quest)
      monkeypatch.setenv(BROKER_TALK, brotocol.encode_talk(child_endpoint.talk))
      own = quest.query_quest(child, child_endpoint.quest, wait_seconds=1, since=0)
      before_race = own['chat_seq']
      grandchild_request = child.send(
        quest.LAUNCH,
        {'target': 'reviewer', 'prompt': 'review', 'talk': ['worker.question']},
      )
      summon._await_acceptance(child, grandchild_request)
      grandchild_endpoint = await asyncio.to_thread(spawner.spawned.get, True, TIMEOUT)
      grandchild = clients.enter_context(
        Client(connect(grandchild_endpoint.channel.host_endpoint.address(LOCAL_HOST)))
      )
      monkeypatch.setenv(BROKER_MISSION, grandchild_endpoint.quest)
      monkeypatch.setenv(BROKER_TALK, brotocol.encode_talk(grandchild_endpoint.talk))
      grandchild_question = grandchild.message(
        grandchild_endpoint.quest, {'text': 'ship this?'}, question=True
      )

      monkeypatch.setenv(BROKER_MISSION, child_endpoint.quest)
      monkeypatch.setenv(BROKER_CHANNEL, child_endpoint.channel.host_endpoint.address(LOCAL_HOST))
      monkeypatch.setenv(BROKER_TALK, brotocol.encode_talk(child_endpoint.talk))
      quest.query_quest(child, grandchild_endpoint.quest, wait_seconds=1, since=0)
      original_read = quest._read_value
      raced = False

      def inject_racing_message(client, kind, args, *, timeout):
        nonlocal raced
        value = original_read(client, kind, args, timeout=timeout)
        if not raced and kind == 'events' and args == {}:
          raced = True
          monkeypatch.setenv(BROKER_MISSION, root_endpoint.quest)
          monkeypatch.setenv(BROKER_TALK, brotocol.encode_talk(root_endpoint.talk))
          root.message(child_endpoint.quest, {'text': 'also run lint'})
          quest.query_quest(root, child_endpoint.quest, wait_seconds=1, since=before_race)
          monkeypatch.setenv(BROKER_MISSION, child_endpoint.quest)
          monkeypatch.setenv(BROKER_TALK, brotocol.encode_talk(child_endpoint.talk))
        return value

      monkeypatch.setattr(quest, '_read_value', inject_racing_message)
      with contextlib.closing(quest.watch(wait_seconds=0.05)) as watch:
        assert await asyncio.to_thread(next, watch) == (
          'before the watch: summoner says start with docs'
        )
        assert await asyncio.to_thread(next, watch) == (
          'before the watch: summon asks ship this? '
          f'(quest {grandchild_endpoint.quest} to reviewer, '
          f'question {grandchild_question.id})'
        )
        assert await asyncio.to_thread(next, watch) == 'summoner says also run lint'


@pytest.mark.asyncio
async def test_watch_replays_retained_chat_at_arm_without_repeating_a_racing_message(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = quest.watch(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await next_message(server)
    await reply(server, channel, arm, outcome='ok', value={'head': 10, 'events': []})

    before = entry(5, 'owner', 'start with docs')
    own_question = entry(6, 'owner', 'which branch?', id='OWN-QUESTION', pending=True)
    racing = entry(11, 'owner', 'also run lint')
    channel, own_query = await next_message(server)
    await reply(
      server,
      channel,
      own_query,
      outcome='ok',
      value={
        'mission': quest_record(
          'ROOT',
          'started',
          kind='root',
          messages=[before, own_question, racing],
          chat_seq=11,
        )
      },
    )
    child_question = entry(8, 'worker', 'ship this?', id='CHILD-QUESTION')
    channel, listing = await next_message(server)
    await reply(
      server,
      channel,
      listing,
      outcome='ok',
      value={
        'missions': [
          {
            **quest_record('CHILD', 'started', talk=['worker.question']),
            'pending': [child_question],
          }
        ]
      },
    )

    assert await first_line == 'before the watch: summoner says start with docs'
    assert await asyncio.to_thread(next, watch) == (
      'before the watch: summoner asks which branch? (question OWN-QUESTION)'
    )
    assert await asyncio.to_thread(next, watch) == (
      'before the watch: summon asks ship this? (quest CHILD to dev, question CHILD-QUESTION)'
    )

    racing_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, poll = await next_message(server)
    assert poll.args == {'after': 10, 'wait': 0.05}
    await reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 11,
        'events': [
          {
            **racing,
            'kind': 'launch',
            'type': 'bro',
            'mission': 'ROOT',
            'parent': 'PARENT',
            'args': {'target': 'dev'},
          }
        ],
      },
    )
    assert await racing_line == 'summoner says also run lint'
    watch.close()


@pytest.mark.asyncio
async def test_watch_arm_replays_an_open_question_older_than_the_tail(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = quest.watch(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await next_message(server)
    await reply(server, channel, arm, outcome='ok', value={'head': 40, 'events': []})
    old_question = entry(3, 'owner', 'which branch?', id='OLD-QUESTION', pending=True)
    recent = entry(39, 'owner', 'nearly there?')
    channel, own_query = await next_message(server)
    await reply(
      server,
      channel,
      own_query,
      outcome='ok',
      value={
        'mission': quest_record(
          'ROOT',
          'started',
          kind='root',
          messages=[old_question, recent],
          messages_truncated=True,
          chat_seq=39,
        )
      },
    )
    channel, listing = await next_message(server)
    await reply(server, channel, listing, outcome='ok', value={'missions': []})

    assert await first_line == (
      'before the watch: summoner asks which branch? (question OLD-QUESTION)'
    )
    assert await asyncio.to_thread(next, watch) == 'before the watch: summoner says nearly there?'
    watch.close()


@pytest.mark.asyncio
async def test_watch_arms_at_head_and_prints_ordered_summon_transitions(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = quest.watch(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await next_message(server)
    assert arm.kind == 'events'
    assert arm.args == {}
    await reply(server, channel, arm, outcome='ok', value={'head': 10, 'events': []})
    await _reply_empty_watch_replay(server)
    channel, poll = await next_message(server)
    assert poll.args == {'after': 10, 'wait': 0.05}
    await reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 12,
        'events': [
          {
            'seq': 11,
            'kind': 'launch',
            'type': 'benchmark',
            'mission': 'B1',
            'parent': 'ROOT',
            'args': {'config': 'benchmark/job.yaml'},
            'transition': 'started',
          },
          {
            'seq': 12,
            'kind': 'launch',
            'type': 'bro',
            'mission': 'S1',
            'parent': 'ROOT',
            'args': {'target': 'reviewer'},
            'transition': 'denied',
            'reason': 'summon denied: not allowed',
          },
        ],
      },
    )
    assert await first_line == 'summon denied: not allowed (quest S1 to reviewer)'

    second_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, poll = await next_message(server)
    assert poll.args == {'after': 12, 'wait': 0.05}
    await reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 13,
        'events': [
          {
            'seq': 13,
            'kind': 'launch',
            'type': 'bro',
            'mission': 'S2',
            'parent': 'ROOT',
            'args': {'target': 'dev'},
            'transition': 'ended',
            'outcome': 'failed',
            'reason': 'timeout',
          }
        ],
      },
    )
    assert await second_line == 'summon ended failed:timeout (quest S2 to dev)'

    third_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, poll = await next_message(server)
    assert poll.args == {'after': 13, 'wait': 0.05}
    await reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 14,
        'events': [
          {
            'seq': 14,
            'kind': 'launch',
            'type': 'bro',
            'mission': 'S3',
            'parent': 'ROOT',
            'args': {'target': 'x\ny'},
            'transition': 'denied',
            'reason': "unknown bro 'x\\ny'",
          }
        ],
      },
    )
    assert await third_line == "unknown bro 'x\\ny' (quest S3 to x\\ny)"

    refusal_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, poll = await next_message(server)
    assert poll.args == {'after': 14, 'wait': 0.05}
    await reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 15,
        'events': [
          {
            'seq': 15,
            'kind': 'launch',
            'type': 'bro',
            'mission': 'S4',
            'parent': 'ROOT',
            'args': {'target': 'dev'},
            'transition': 'refused',
            'from': 'owner',
            'reason': 'quest talk lacks owner.say',
            'head': {'text': 'blocked'},
          }
        ],
      },
    )
    assert await refusal_line == (
      'summon refused quest talk lacks owner.say: blocked (quest S4 to dev)'
    )
    watch.close()


@pytest.mark.asyncio
async def test_watch_replays_retained_chat_after_an_event_gap(monkeypatch):
  async with running_server(monkeypatch) as server:
    with contextlib.closing(quest.watch(wait_seconds=0.05)) as watch:
      gap_line = asyncio.create_task(asyncio.to_thread(next, watch))
      channel, arm = await next_message(server)
      await reply(server, channel, arm, outcome='ok', value={'head': 10, 'events': []})
      await _reply_empty_watch_replay(server)
      channel, poll = await next_message(server)
      assert poll.args == {'after': 10, 'wait': 0.05}
      await reply(server, channel, poll, outcome='denied', error='events gap: oldest is 15')
      channel, rearm = await next_message(server)
      assert rearm.args == {}
      await reply(server, channel, rearm, outcome='ok', value={'head': 20, 'events': []})
      assert await gap_line == 'quest watch gap: events gap: oldest is 15; re-armed at 20'

      replay_line = asyncio.create_task(asyncio.to_thread(next, watch))
      retained = entry(18, 'owner', 'while disconnected')
      channel, own_query = await next_message(server)
      await reply(
        server,
        channel,
        own_query,
        outcome='ok',
        value={
          'mission': quest_record('ROOT', 'started', kind='root', messages=[retained], chat_seq=18)
        },
      )
      channel, listing = await next_message(server)
      await reply(server, channel, listing, outcome='ok', value={'missions': []})
      assert await replay_line == 'before the watch: summoner says while disconnected'

      resumed = asyncio.create_task(asyncio.to_thread(next, watch))
      channel, poll = await next_message(server)
      assert poll.args == {'after': 20, 'wait': 0.05}
      await reply(
        server,
        channel,
        poll,
        outcome='ok',
        value={
          'head': 21,
          'events': [
            {
              'seq': 21,
              'kind': 'launch',
              'type': 'bro',
              'mission': 'S1',
              'parent': 'ROOT',
              'args': {'target': 'dev'},
              'transition': 'denied',
              'reason': 'not allowed',
            }
          ],
        },
      )
      assert await resumed == 'not allowed (quest S1 to dev)'


def test_chat_watch_lines_show_the_other_end_and_every_refusal():
  child = {
    'kind': 'launch',
    'type': 'bro',
    'mission': 'CHILD',
    'parent': 'ROOT',
    'args': {'target': 'dev'},
    'transition': 'message',
    'from': 'worker',
    'id': 'QUESTION-1',
    'head': {'text': 'approve?'},
  }
  assert quest._chat_event_line(child, 'ROOT') == (
    'summon asks approve? (quest CHILD to dev, question QUESTION-1)'
  )
  own = {
    **child,
    'mission': 'ROOT',
    'parent': 'PARENT',
    'from': 'owner',
    'id': None,
    'reply_to': 'QUESTION-2',
    'head': {'text': 'approved'},
  }
  assert quest._chat_event_line(own, 'ROOT') == 'summoner replies approved (to QUESTION-2)'
  assert quest._chat_event_line({**own, 'from': 'worker'}, 'ROOT') is None
  refused = {
    **child,
    'transition': 'refused',
    'from': 'owner',
    'id': None,
    'reason': 'quest talk lacks owner.say',
    'head': {'text': 'blocked'},
  }
  assert quest._chat_event_line(refused, 'ROOT') == (
    'summon refused quest talk lacks owner.say: blocked (quest CHILD to dev)'
  )
  oversized = {
    **refused,
    'reason': 'over the message bound',
    'head': {'head': '{"text":"xx', 'truncated': True},
  }
  assert quest._chat_event_line(oversized, 'ROOT') == (
    'summon refused over the message bound (quest CHILD to dev)'
  )


def test_the_watch_line_can_carry_any_registered_name_whole():
  from bro.broker.journal import ARGS_STRING_HEAD
  from bro.registry import MAX_NAME_LENGTH

  assert MAX_NAME_LENGTH <= ARGS_STRING_HEAD


@pytest.mark.asyncio
async def test_watch_refuses_an_accepted_summon_event_without_a_target(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = quest.watch(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await next_message(server)
    await reply(server, channel, arm, outcome='ok', value={'head': 0, 'events': []})
    await _reply_empty_watch_replay(server)
    channel, poll = await next_message(server)
    await reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 1,
        'events': [
          {
            'seq': 1,
            'kind': 'launch',
            'type': 'bro',
            'mission': 'S1',
            'parent': 'ROOT',
            'args': {'head': '{"target":"dev","share":[', 'truncated': True},
            'transition': 'accepted',
          }
        ],
      },
    )
    with pytest.raises(quest.QuestError, match='without a target'):
      await first_line


@pytest.mark.asyncio
async def test_watch_refuses_a_quest_this_session_did_not_summon(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = quest.watch(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await next_message(server)
    await reply(server, channel, arm, outcome='ok', value={'head': 0, 'events': []})
    await _reply_empty_watch_replay(server)
    channel, poll = await next_message(server)
    await reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 1,
        'events': [
          {
            'seq': 1,
            'kind': 'launch',
            'type': 'bro',
            'mission': 'GRANDCHILD',
            'parent': 'CHILD',
            'args': {'target': 'dev'},
            'transition': 'started',
          }
        ],
      },
    )
    with pytest.raises(quest.QuestError, match='did not summon'):
      await first_line


@pytest.mark.asyncio
async def test_watch_refuses_to_start_without_the_quest_this_session_answers(monkeypatch):
  async with running_server(monkeypatch):
    monkeypatch.delenv(BROKER_MISSION, raising=False)
    watch = quest.watch(wait_seconds=0.05)
    with pytest.raises(quest.QuestError, match=BROKER_MISSION):
      await asyncio.to_thread(next, watch)


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
