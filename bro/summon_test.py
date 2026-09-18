import asyncio
import contextlib
import json
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from bro import summon
from bro.broker import brotocol
from bro.broker.brotocol import Message
from bro.broker.client import CHANNEL_ENV, QUEST_ENV
from bro.broker.transport import ChannelID
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport

TIMEOUT = 5.0


class StubSink:
  def __init__(self):
    self.messages: asyncio.Queue = asyncio.Queue()

  async def on_connect(self, channel: ChannelID) -> None:
    pass

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    self.messages.put_nowait((channel, message))

  async def on_disconnect(self, channel: ChannelID) -> None:
    pass


@dataclass
class Harness:
  transport: TcpServerTransport
  sink: StubSink


@contextlib.asynccontextmanager
async def running_server(monkeypatch):
  transport = TcpServerTransport([LOCAL_HOST])
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)
  provisioned = await transport.provision()
  monkeypatch.setenv(CHANNEL_ENV, provisioned.host_endpoint.address(LOCAL_HOST))
  monkeypatch.setenv(QUEST_ENV, 'ROOT')
  monkeypatch.setenv(summon.RUNTIME_ENV, '/runtime')
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def _next(server: Harness) -> tuple[ChannelID, Message]:
  return await asyncio.wait_for(server.sink.messages.get(), TIMEOUT)


def _id(message: Message) -> str:
  assert message.id is not None
  return message.id


async def _reply(server: Harness, channel: ChannelID, request: Message, **payload) -> None:
  await server.transport.send(channel, brotocol.result(_id(request), **payload))


def _quest(
  request_id: str,
  state: str,
  *,
  result: dict | None = None,
  kind: str = 'summon',
  trail_id: str | None = None,
  **overrides,
) -> dict:
  quest = {
    'id': request_id,
    'kind': kind,
    'parent': 'ROOT',
    'args': {'target': 'dev', 'prompt': 'work'},
    'state': state,
    'talk': [],
    'pending': [],
    'messages': [],
    'chat_seq': 0,
  }
  if result is not None:
    quest['result'] = result
  if trail_id is not None:
    quest['trail_id'] = trail_id
  quest.update(overrides)
  return quest


def test_help_names_check_list_and_no_cursor_option(capsys):
  with pytest.raises(SystemExit):
    summon.main(['summon', 'check', '--help'])
  output = capsys.readouterr().out
  assert '--wait' in output
  assert '--last-seen' not in output

  with pytest.raises(SystemExit):
    summon.main(['summon', '--help'])
  output = capsys.readouterr().out
  assert 'summon check' in output
  assert 'summon list' in output
  assert 'ride solo --summoned <token>' in ' '.join(output.split())


def test_bare_summon_forwards_the_request(monkeypatch):
  calls: list[tuple] = []
  monkeypatch.setattr(
    summon,
    'relay_summon',
    lambda target, prompt, *, timeout, into, hold, grant, revoke, share, llm, harness, party, isolation, talk, manual: (
      calls.append((target, prompt, timeout, into)) or 0
    ),
  )

  assert summon.main(['summon', '--timeout', '60', 'dev', 'deploy']) == 0
  assert calls == [('dev', 'deploy', 60.0, None)]


def test_manual_summon_refuses_launch_owned_flags(monkeypatch, caplog):
  monkeypatch.setenv(CHANNEL_ENV, 'tcp://token@127.0.0.1:1')
  for flags in (
    ['--timeout', '60'],
    ['--hold', 'attended'],
    ['--harness', 'claude'],
    ['--llm', ':fable5'],
    ['--start'],
    ['--join'],
    ['--unboxed'],
  ):
    assert summon.main(['summon', '--manual', *flags, 'dev', 'work']) == 1
  assert sum('launch owns' in record.getMessage() for record in caplog.records) == 7


@pytest.mark.asyncio
async def test_detached_summon_waits_for_acceptance(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--detach', '--timeout', '42', 'dev', 'work'])
    )
    channel, request = await _next(server)
    assert request.kind == 'summon'
    assert request.args == {'target': 'dev', 'prompt': 'work', 'timeout': 42.0}
    await server.transport.send(channel, brotocol.mark(_id(request), 'accepted'))

    assert await task == 0
    assert capsys.readouterr().out == f'{request.id}\n'


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ('flag', 'field', 'value'),
  [
    ('--start', 'party', 'start'),
    ('--join', 'party', 'join'),
    ('--boxed', 'isolation', 'boxed'),
    ('--unboxed', 'isolation', 'unboxed'),
  ],
)
async def test_placement_flags_reach_the_request(monkeypatch, flag, field, value):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--detach', flag, 'dev', 'work'])
    )
    channel, request = await _next(server)
    assert request.args[field] == value
    await server.transport.send(channel, brotocol.mark(_id(request), 'accepted'))
    assert await task == 0


def test_join_refuses_into_before_opening_a_channel(monkeypatch, caplog):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)

  assert summon.main(['summon', '--join', '--into', 'feature', 'dev', 'work']) == 1

  assert 'shares the summoner' in caplog.text
  assert CHANNEL_ENV not in caplog.text


@pytest.mark.asyncio
async def test_denied_detached_summon_fails_without_printing_an_id(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--detach', 'dev', 'work'])
    )
    channel, request = await _next(server)
    await _reply(
      server,
      channel,
      request,
      outcome='denied',
      error="summon denied: 'dev' is not in the list",
    )

    assert await task == 1
    assert capsys.readouterr().out == ''
    assert 'summon denied' in caplog.text


@pytest.mark.asyncio
async def test_manual_detached_summon_returns_launch_token_after_acceptance(
  monkeypatch, capsys, caplog
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--manual', '--detach', 'dev', 'work'])
    )
    channel, request = await _next(server)
    assert request.args == {'target': 'dev', 'prompt': 'work', 'manual': True}
    await server.transport.send(channel, brotocol.mark(_id(request), 'accepted'))

    assert await task == 0
    assert capsys.readouterr().out == f'{request.id}\n'
    assert (
      summon.manual_launch_command(_id(request), 'dev')
      == f'/runtime/venv/bin/ride along --summoned {_id(request)} dev'
    )
    assert summon.manual_launch_command(_id(request), 'dev') in caplog.text


@pytest.mark.asyncio
async def test_blocking_summon_relays_the_answer(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'dev', 'work']))
    channel, request = await _next(server)
    await server.transport.send(channel, brotocol.mark(_id(request), 'accepted'))
    await server.transport.send(channel, brotocol.mark(_id(request), 'trail', trail_id='T1'))
    await _reply(server, channel, request, outcome='ok', value='answer')

    assert await task == 0
    assert capsys.readouterr().out == 'answer\n'
    assert request.id in caplog.text
    assert 'T1' in caplog.text


@pytest.mark.asyncio
async def test_blocking_summon_returns_at_a_child_question_and_rides_through_says(
  monkeypatch, capsys, caplog
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        summon.main,
        ['summon', '--talk', 'worker.question', 'dev', 'work'],
      )
    )
    channel, request = await _next(server)
    assert request.args['talk'] == ['worker.question']
    await server.transport.send(channel, brotocol.mark(_id(request), 'accepted'))
    await server.transport.send(channel, brotocol.message(_id(request), {'text': 'still working'}))
    await server.transport.send(
      channel, brotocol.message(_id(request), {'text': 'approve?'}, id='QUESTION-1')
    )

    assert await task == summon.QUESTION_EXIT_CODE
    assert capsys.readouterr().out == 'approve?\n'
    assert 'still working' in caplog.text
    assert 'QUESTION-1' in caplog.text


@pytest.mark.asyncio
async def test_say_to_a_child_checks_talk_then_sends(monkeypatch):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'say', 'REQ-1', 'steer left'])
    )
    channel, query = await _next(server)
    assert query.args == {'id': 'REQ-1'}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1',
          'started',
          talk=['requester.say', 'worker.say'],
          pending=[],
          messages=[],
          chat_seq=0,
        )
      },
    )
    _, message = await _next(server)
    assert message.type == brotocol.Tag.MESSAGE
    assert message.quest_id == 'REQ-1'
    assert message.payload == {'text': 'steer left'}
    assert await task == 0


@pytest.mark.asyncio
async def test_say_without_a_quest_uses_the_session_quest_and_published_talk(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(QUEST_ENV, 'OWN-QUEST')
    monkeypatch.setenv(brotocol.TALK_ENV, 'worker.say')
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'say', 'progress']))
    channel, query = await _next(server)
    assert query.args == {'id': 'OWN-QUEST'}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'OWN-QUEST', 'started', talk=['worker.say'], pending=[], messages=[], chat_seq=0
        )
      },
    )
    _, message = await _next(server)
    assert message.quest_id == 'OWN-QUEST'
    assert message.payload == {'text': 'progress'}
    assert await task == 0


@pytest.mark.asyncio
async def test_say_question_timeout_returns_its_recoverable_id(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        summon.main,
        ['summon', 'say', 'REQ-1', 'ready?', '--wait', '0.1'],
      )
    )
    channel, query = await _next(server)
    live = _quest(
      'REQ-1',
      'started',
      talk=['requester.question', 'worker.say'],
      pending=[],
      messages=[],
      chat_seq=0,
    )
    await _reply(server, channel, query, outcome='ok', value={'quest': live})
    _, question = await _next(server)
    assert question.id is not None
    channel, query = await _next(server)
    asked = {
      'seq': 1,
      'at': 'now',
      'transition': 'message',
      'from': 'requester',
      'id': question.id,
      'head': {'text': 'ready?'},
    }
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'quest': {**live, 'pending': [asked], 'messages': [asked], 'chat_seq': 1}},
    )
    _, waiting = await _next(server)
    assert waiting.args['since'] == 1

    assert await task == summon.QUESTION_EXIT_CODE
    assert capsys.readouterr().out == f'{question.id}\n'


@pytest.mark.asyncio
async def test_waiting_question_fails_on_its_correlated_host_refusal(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        summon.main,
        ['summon', 'say', 'REQ-1', 'ready?', '--wait', '1'],
      )
    )
    channel, query = await _next(server)
    live = _quest('REQ-1', 'started', talk=['requester.question', 'worker.say'])
    await _reply(server, channel, query, outcome='ok', value={'quest': live})
    _, question = await _next(server)
    assert question.id is not None
    refused = {
      'seq': 1,
      'at': 'now',
      'transition': 'refused',
      'from': 'requester',
      'id': question.id,
      'head': {'text': 'ready?'},
      'reason': 'host refused the move',
    }
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': {**live, 'messages': [refused], 'chat_seq': 1},
      },
    )

    assert await task == 1
    assert 'host refused the move' in caplog.text


@pytest.mark.asyncio
async def test_waiting_question_fails_when_the_quest_ends_without_a_reply(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        summon.main,
        ['summon', 'say', 'REQ-1', 'ready?', '--wait', '1'],
      )
    )
    channel, query = await _next(server)
    live = _quest('REQ-1', 'started', talk=['requester.question', 'worker.say'])
    await _reply(server, channel, query, outcome='ok', value={'quest': live})
    _, question = await _next(server)
    assert question.id is not None
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1',
          'ended',
          result={'outcome': 'ok', 'value': 'child stopped'},
          talk=['requester.question', 'worker.say'],
        )
      },
    )

    assert await task == 1
    assert 'already ended successfully' in caplog.text


@pytest.mark.asyncio
async def test_say_refuses_an_ended_quest_before_sending(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'say', 'REQ-1', 'too late'])
    )
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1',
          'ended',
          result={'outcome': 'ok', 'value': 'done'},
          talk=['requester.say', 'worker.say'],
        )
      },
    )

    assert await task == 1
    assert 'already ended successfully' in caplog.text


@pytest.mark.asyncio
async def test_say_reports_an_oversized_reply_id_as_a_cli_error(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        summon.main,
        [
          'summon',
          'say',
          'REQ-1',
          'reply',
          '--reply-to',
          'x' * (brotocol.MAX_IDENTIFIER_BYTES + 1),
        ],
      )
    )
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1',
          'started',
          talk=['worker.question', 'worker.say'],
        )
      },
    )

    assert await task == 1
    assert 'over' in caplog.text


@pytest.mark.asyncio
async def test_say_refuses_a_descendant_that_is_not_a_direct_child(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'say', 'GRANDCHILD', 'skip a level'])
    )
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'GRANDCHILD',
          'started',
          talk=['requester.say', 'worker.say'],
          parent='CHILD',
        )
      },
    )

    assert await task == 1
    assert 'not a direct child' in caplog.text


@pytest.mark.asyncio
async def test_say_refuses_a_move_the_child_quest_talk_forbids(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'say', 'REQ-1', 'not allowed'])
    )
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1', 'started', talk=['worker.say'], pending=[], messages=[], chat_seq=0
        )
      },
    )
    assert await task == 1
    assert 'query REQ-1 forbids' in caplog.text


@pytest.mark.asyncio
async def test_silent_blocking_wait_queries_live_state_then_resumes(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--timeout', '0.05', 'dev', 'work'])
    )
    channel, original = await _next(server)
    await server.transport.send(channel, brotocol.mark(_id(original), 'accepted'))

    query_channel, query = await _next(server)
    assert query.kind == 'query'
    assert query.args == {'id': original.id}
    await server.transport.send(query_channel, brotocol.result(_id(original), 'ok', value='answer'))
    await _reply(
      server,
      query_channel,
      query,
      outcome='ok',
      value={'quest': _quest(_id(original), 'started', trail_id='T2')},
    )

    assert await task == 0
    assert capsys.readouterr().out == 'answer\n'


@pytest.mark.asyncio
async def test_silent_blocking_wait_interprets_terminal_query(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--timeout', '0.05', 'dev', 'work'])
    )
    _, original = await _next(server)
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          _id(original),
          'ended',
          result={'outcome': 'ok', 'value': 'retained answer'},
        )
      },
    )

    assert await task == 0
    assert capsys.readouterr().out == 'retained answer\n'


@pytest.mark.asyncio
async def test_check_queries_pending_without_consuming(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))
    channel, query = await _next(server)
    assert query.kind == 'query'
    assert query.args == {'id': 'REQ-1'}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'quest': _quest('REQ-1', 'started', trail_id='T9')},
    )

    assert await task == summon.PENDING_EXIT_CODE
    assert 'still running' in caplog.text
    assert 'T9' in caplog.text


@pytest.mark.asyncio
async def test_check_without_a_quest_reads_the_session_quest_and_reports_a_question(
  monkeypatch, capsys
):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(QUEST_ENV, 'OWN-QUEST')
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check']))
    channel, query = await _next(server)
    assert query.args == {'id': 'OWN-QUEST'}
    question = {
      'seq': 2,
      'at': 'now',
      'transition': 'message',
      'from': 'requester',
      'id': 'QUESTION-2',
      'head': {'text': 'which region?'},
    }
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'OWN-QUEST',
          'started',
          talk=['requester.question', 'worker.say'],
          pending=[question],
          messages=[question],
          chat_seq=2,
        )
      },
    )

    assert await task == summon.QUESTION_EXIT_CODE
    output = json.loads(capsys.readouterr().out)
    assert output['question'] == {'id': 'QUESTION-2', 'text': 'which region?'}
    assert output['talk'] == ['requester.question', 'worker.say']


@pytest.mark.asyncio
async def test_check_keeps_descendant_lifecycle_recovery_without_claiming_its_question(
  monkeypatch, capsys
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'GRANDCHILD']))
    channel, query = await _next(server)
    question = {
      'seq': 2,
      'at': 'now',
      'transition': 'message',
      'from': 'worker',
      'id': 'QUESTION-2',
      'head': {'text': 'ask my direct summoner'},
    }
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'GRANDCHILD',
          'started',
          parent='CHILD',
          talk=['worker.question', 'worker.say'],
          pending=[question],
          messages=[question],
          chat_seq=2,
        )
      },
    )

    assert await task == summon.PENDING_EXIT_CODE
    output = json.loads(capsys.readouterr().out)
    assert output['messages'] == [question]
    assert 'question' not in output


@pytest.mark.asyncio
async def test_check_wait_collects_a_descendant_answer(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        summon.main,
        ['summon', 'check', '--wait', '--timeout', '1', 'GRANDCHILD'],
      )
    )
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'GRANDCHILD',
          'ended',
          parent='CHILD',
          result={'outcome': 'ok', 'value': 'descendant answer'},
        )
      },
    )

    assert await task == 0
    assert capsys.readouterr().out == 'descendant answer\n'


@pytest.mark.asyncio
async def test_check_recovers_a_reply_after_the_original_waiter_is_gone(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))
    channel, query = await _next(server)
    reply = {
      'seq': 3,
      'at': 'now',
      'transition': 'message',
      'from': 'worker',
      'reply_to': 'QUESTION-1',
      'head': {'text': 'yes'},
    }
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1',
          'started',
          talk=['requester.question', 'worker.say'],
          pending=[],
          messages=[reply],
          messages_truncated=True,
          chat_seq=3,
        )
      },
    )

    assert await task == summon.PENDING_EXIT_CODE
    output = json.loads(capsys.readouterr().out)
    assert output['messages'][-1]['head']['text'] == 'yes'
    assert output['messages_truncated'] is True


@pytest.mark.asyncio
async def test_check_surfaces_a_message_sent_past_the_local_guard_and_refused_by_the_host(
  monkeypatch, capsys
):
  async with running_server(monkeypatch) as server:

    def send_anyway() -> None:
      with summon.open_client() as client:
        client.message('REQ-1', {'text': 'blocked'})

    sending = asyncio.create_task(asyncio.to_thread(send_anyway))
    _, message = await _next(server)
    assert message.type == brotocol.Tag.MESSAGE
    await sending

    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))
    channel, query = await _next(server)
    refused = {
      'seq': 4,
      'at': 'now',
      'transition': 'refused',
      'from': 'requester',
      'head': {'text': 'blocked'},
      'reason': 'quest talk lacks requester.say',
    }
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1',
          'started',
          talk=['worker.say'],
          pending=[],
          messages=[refused],
          chat_seq=4,
        )
      },
    )

    assert await task == summon.PENDING_EXIT_CODE
    assert json.loads(capsys.readouterr().out)['messages'] == [refused]


@pytest.mark.asyncio
async def test_check_returns_the_retained_answer_repeatably(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    for _ in range(2):
      task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))
      channel, query = await _next(server)
      await _reply(
        server,
        channel,
        query,
        outcome='ok',
        value={
          'quest': _quest(
            'REQ-1',
            'ended',
            result={'outcome': 'ok', 'value': 'retained answer'},
          )
        },
      )
      assert await task == 0
    assert capsys.readouterr().out == 'retained answer\nretained answer\n'


def test_wait_deadline_returns_pending_without_starting_another_poll(monkeypatch):
  monkeypatch.setenv(QUEST_ENV, 'ROOT')
  ticks = iter((10.0, 10.0, 15.0))
  polls: list[tuple[float, float | None]] = []

  monkeypatch.setattr(summon.time, 'monotonic', lambda: next(ticks))

  def query(client, request_id, *, wait_seconds=0, since=None, read_timeout=None):
    del client
    polls.append((wait_seconds, read_timeout))
    return _quest(request_id, 'started', trail_id='T9')

  monkeypatch.setattr(summon, '_query_quest', query)
  status = summon.wait_summon('REQ-1', timeout=5, client=MagicMock())

  assert status == summon.SummonStatus(pending=True, trail_id='T9', request_id='REQ-1')
  assert polls == [(0, 5)]


def test_wait_deadline_bounds_a_stalled_broker_read(monkeypatch):
  ticks = iter((10.0, 10.0))
  client = MagicMock()
  client.call.side_effect = TimeoutError

  monkeypatch.setattr(summon.time, 'monotonic', lambda: next(ticks))

  with pytest.raises(summon.SummonError, match="no reply to broker 'query' read within 5s"):
    summon.wait_summon('REQ-1', timeout=5, client=client)

  client.call.assert_called_once_with('query', {'id': 'REQ-1'}, 5)


@pytest.mark.asyncio
async def test_check_wait_deadline_returns_pending_when_the_final_read_times_out(
  monkeypatch, caplog
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        summon.main,
        ['summon', 'check', '--wait', '--timeout', '0.2', 'REQ-1'],
      )
    )
    channel, initial = await _next(server)
    assert initial.args == {'id': 'REQ-1'}
    await _reply(
      server,
      channel,
      initial,
      outcome='ok',
      value={'quest': _quest('REQ-1', 'started', trail_id='T9')},
    )

    _, final = await _next(server)
    assert final.args['id'] == 'REQ-1'
    assert 0 < final.args['wait'] <= 0.2

    assert await task == summon.PENDING_EXIT_CODE
    assert 'still running' in caplog.text
    assert 'T9' in caplog.text


@pytest.mark.parametrize('timeout', [float('nan'), float('inf')])
def test_wait_rejects_non_finite_deadlines(timeout):
  with pytest.raises(summon.SummonError, match='finite positive'):
    summon.wait_summon('REQ-1', timeout=timeout, client=MagicMock())


def test_check_wait_exits_pending_when_its_deadline_passes(monkeypatch, caplog):
  monkeypatch.setattr(
    summon,
    'wait_summon',
    lambda request_id, *, timeout=None: summon.SummonStatus(pending=True, trail_id='T9'),
  )

  assert summon._check('REQ-1', wait=True, timeout=5) == summon.PENDING_EXIT_CODE
  assert 'still running' in caplog.text
  assert 'T9' in caplog.text


@pytest.mark.asyncio
async def test_check_wait_loops_query_until_terminal(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'check', '--wait', '--timeout', '1', 'REQ-1'])
    )
    channel, query = await _next(server)
    assert query.args == {'id': 'REQ-1'}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'quest': _quest('REQ-1', 'started')},
    )
    channel, query = await _next(server)
    assert query.args['id'] == 'REQ-1'
    assert 0 < query.args['wait'] <= 1
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1',
          'ended',
          result={'outcome': 'ok', 'value': 'done'},
        )
      },
    )

    assert await task == 0
    assert capsys.readouterr().out == 'done\n'


@pytest.mark.asyncio
async def test_unknown_and_evicted_checks_fail(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    unknown = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'UNKNOWN']))
    channel, query = await _next(server)
    await _reply(server, channel, query, outcome='denied', error="unknown quest id 'UNKNOWN'")
    assert await unknown == 1

    evicted = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'OLD']))
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'quest': _quest('OLD', 'evicted')},
    )
    assert await evicted == 1
  assert 'unknown quest id' in caplog.text
  assert 'no longer retained' in caplog.text


@pytest.mark.asyncio
async def test_list_reads_every_page_and_keeps_only_summons(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'list']))
    channel, query = await _next(server)
    assert query.args == {}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quests': [_quest('ROOT', 'started', kind='root'), _quest('S1', 'started')],
        'cursor': 'NEXT',
      },
    )
    channel, query = await _next(server)
    assert query.args == {'cursor': 'NEXT'}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quests': [
          _quest('B1', 'ended', kind='benchmark'),
          _quest('S0', 'denied', result={'outcome': 'denied', 'error': 'no'}),
        ]
      },
    )

    assert await task == 0
    assert [quest['id'] for quest in json.loads(capsys.readouterr().out)['quests']] == ['S1', 'S0']


@pytest.mark.asyncio
async def test_watch_arms_at_head_and_prints_ordered_summon_transitions(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(QUEST_ENV, 'ROOT')
    watch = summon.watch_summons(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await _next(server)
    assert arm.kind == 'events'
    assert arm.args == {}
    await _reply(server, channel, arm, outcome='ok', value={'head': 10, 'events': []})
    channel, poll = await _next(server)
    assert poll.args == {'after': 10, 'wait': 0.05}
    await _reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 12,
        'events': [
          {
            'seq': 11,
            'kind': 'benchmark',
            'quest': 'B1',
            'parent': 'ROOT',
            'args': {},
            'transition': 'started',
          },
          {
            'seq': 12,
            'kind': 'summon',
            'quest': 'S1',
            'parent': 'ROOT',
            'args': {'target': 'reviewer'},
            'transition': 'denied',
            'reason': 'summon denied: not allowed',
          },
        ],
      },
    )
    assert await first_line == 'summon denied: not allowed (request S1 to reviewer)'

    second_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, poll = await _next(server)
    assert poll.args == {'after': 12, 'wait': 0.05}
    await _reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 13,
        'events': [
          {
            'seq': 13,
            'kind': 'summon',
            'quest': 'S2',
            'parent': 'C1',
            'args': {'target': 'dev'},
            'transition': 'ended',
            'outcome': 'failed',
            'reason': 'timeout',
          }
        ],
      },
    )
    assert await second_line == (
      'summon ended failed:timeout (request S2 to dev, summoned by request C1)'
    )

    third_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, poll = await _next(server)
    assert poll.args == {'after': 13, 'wait': 0.05}
    await _reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 14,
        'events': [
          {
            'seq': 14,
            'kind': 'summon',
            'quest': 'S3',
            'parent': 'ROOT',
            'args': {'target': 'x\ny'},
            'transition': 'denied',
            'reason': "unknown bro 'x\\ny'",
          }
        ],
      },
    )
    assert await third_line == "unknown bro 'x\\ny' (request S3 to x\\ny)"

    refusal_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, poll = await _next(server)
    assert poll.args == {'after': 14, 'wait': 0.05}
    await _reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 15,
        'events': [
          {
            'seq': 15,
            'kind': 'summon',
            'quest': 'S4',
            'parent': 'ROOT',
            'args': {'target': 'dev'},
            'transition': 'refused',
            'from': 'requester',
            'reason': 'quest talk lacks requester.say',
            'head': {'text': 'blocked'},
          }
        ],
      },
    )
    assert await refusal_line == (
      'summon refused quest talk lacks requester.say: blocked (request S4 to dev)'
    )
    watch.close()


def test_chat_watch_lines_show_the_other_end_and_every_refusal():
  child = {
    'kind': 'summon',
    'quest': 'CHILD',
    'parent': 'ROOT',
    'args': {'target': 'dev'},
    'transition': 'message',
    'from': 'worker',
    'id': 'QUESTION-1',
    'head': {'text': 'approve?'},
  }
  assert summon._chat_event_line(child, 'ROOT') == (
    'summon asks approve? (request CHILD to dev, question QUESTION-1)'
  )
  own = {
    **child,
    'quest': 'ROOT',
    'parent': 'PARENT',
    'from': 'requester',
    'id': None,
    'reply_to': 'QUESTION-2',
    'head': {'text': 'approved'},
  }
  assert summon._chat_event_line(own, 'ROOT') == 'summoner replies approved (to QUESTION-2)'
  assert summon._chat_event_line({**own, 'from': 'worker'}, 'ROOT') is None
  refused = {
    **child,
    'transition': 'refused',
    'from': 'requester',
    'id': None,
    'reason': 'quest talk lacks requester.say',
    'head': {'text': 'blocked'},
  }
  assert summon._chat_event_line(refused, 'ROOT') == (
    'summon refused quest talk lacks requester.say: blocked (request CHILD to dev)'
  )


def test_the_watch_line_can_carry_any_registered_name_whole():
  from bro.broker.journal import ARGS_STRING_HEAD
  from bro.registry import MAX_NAME_LENGTH

  assert MAX_NAME_LENGTH <= ARGS_STRING_HEAD


@pytest.mark.asyncio
async def test_watch_refuses_an_accepted_summon_event_without_a_target(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(QUEST_ENV, 'ROOT')
    watch = summon.watch_summons(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await _next(server)
    await _reply(server, channel, arm, outcome='ok', value={'head': 0, 'events': []})
    channel, poll = await _next(server)
    await _reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 1,
        'events': [
          {
            'seq': 1,
            'kind': 'summon',
            'quest': 'S1',
            'parent': 'ROOT',
            'args': {'head': '{"target":"dev","share":[', 'truncated': True},
            'transition': 'accepted',
          }
        ],
      },
    )
    with pytest.raises(summon.SummonError, match='without a target'):
      await first_line


@pytest.mark.asyncio
async def test_watch_refuses_to_start_without_the_quest_this_session_answers(monkeypatch):
  async with running_server(monkeypatch):
    monkeypatch.delenv(QUEST_ENV, raising=False)
    watch = summon.watch_summons(wait_seconds=0.05)
    with pytest.raises(summon.SummonError, match=QUEST_ENV):
      await asyncio.to_thread(next, watch)


def test_summon_chat_text_is_capped_to_a_complete_journal_head():
  from bro.broker.journal import MESSAGE_HEAD_BYTES

  bounded = summon._bounded_text('"\n' * MESSAGE_HEAD_BYTES)
  encoded = json.dumps({'text': bounded}, ensure_ascii=False, separators=(',', ':')).encode()
  assert len(encoded) <= MESSAGE_HEAD_BYTES
  assert len(bounded) < MESSAGE_HEAD_BYTES


def test_may_summon_round_trips_the_launch_published_list(monkeypatch):
  monkeypatch.setenv(summon.MAY_SUMMON_ENV, summon.encode_may_summon({'reviewer', 'dev'}))
  assert summon.may_summon() == ('dev', 'reviewer')


def test_may_summon_distinguishes_empty_and_unpublished(monkeypatch):
  monkeypatch.setenv(summon.MAY_SUMMON_ENV, '')
  assert summon.may_summon() == ()
  monkeypatch.delenv(summon.MAY_SUMMON_ENV)
  assert summon.may_summon() is None


def test_invalid_published_permit_fails(monkeypatch):
  monkeypatch.setenv(summon.PERMITS_ENV, 'party.start')

  with pytest.raises(ValueError, match='unknown permit'):
    summon.permits()


def test_errors_without_a_channel(monkeypatch, caplog):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert summon.main(['summon', 'dev', 'work']) == 1
  assert summon.main(['summon', 'check', 'SOME-ID']) == 1
  assert summon.main(['summon', 'list']) == 1
  assert CHANNEL_ENV in caplog.text


def test_summoned_child_env_is_what_the_child_reads_back(monkeypatch):
  for key, value in summon.summoned_child_env(
    {'reviewer', 'dev'}, {'party.join'}, {'trail_id': 'T1'}
  ).items():
    monkeypatch.setenv(key, value)
  assert summon.summoned()
  assert summon.may_summon() == ('dev', 'reviewer')
  assert summon.permits() == ('party.join',)
  assert summon.summoned_by_from_env() == {'trail_id': 'T1'}


@pytest.mark.parametrize(
  'value',
  [
    '{"unknown":"value"}',
    '{"trail_id":"T1","unknown":true}',
  ],
)
def test_summoner_provenance_refuses_an_unknown_shape(monkeypatch, value):
  monkeypatch.setenv(summon.SUMMONER_ENV, value)
  with pytest.raises(ValueError, match='invalid summoned_by shape'):
    summon.summoned_by_from_env()


def test_party_member_reads_the_joined_session_mark(monkeypatch):
  assert summon.party_member() is None
  monkeypatch.setenv(summon.PARTY_MEMBER_ENV, 'broker-CH')
  assert summon.party_member() == 'broker-CH'


def test_summoned_child_env_without_a_summoner_carries_no_provenance(monkeypatch):
  env = summon.summoned_child_env((), (), None)
  assert summon.SUMMONER_ENV not in env
  for key, value in env.items():
    monkeypatch.setenv(key, value)
  assert summon.may_summon() == ()
  assert summon.summoned_by_from_env() is None


def test_a_failure_before_any_trail_does_not_point_at_trails():
  payload = {
    'outcome': 'failed',
    'error': None,
    'detail': {'reason': 'exit', 'exit_code': 1, 'output_tail': 'recorder: no state dir\n'},
  }
  with pytest.raises(summon.SummonError) as raised:
    summon._interpret_payload(payload, None)
  message = str(raised.value)
  assert message.startswith('summon failed (exit); recorder: no state dir; ')
  assert 'announced no trail' in message
  assert 'rewind' not in message


def test_a_failure_with_a_trail_points_at_it():
  payload = {'outcome': 'failed', 'error': None, 'detail': {'reason': 'exit', 'exit_code': 1}}
  with pytest.raises(summon.SummonError, match='summon failed \\(exit\\); .*rewind show T1'):
    summon._interpret_payload(payload, 'T1')
