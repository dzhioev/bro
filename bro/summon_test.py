import asyncio

import pytest

from bro import quest, summon
from bro.broker import brotocol
from bro.broker.client import CHANNEL_ENV
from bro.quest_test_helper import (
  entry,
  message_id,
  next_message,
  quest_record,
  reply,
  running_server,
)


def test_help_points_at_quest_and_the_manual_launch(capsys):
  with pytest.raises(SystemExit):
    summon.main(['summon', '--help'])
  output = ' '.join(capsys.readouterr().out.split())
  assert 'with `quest`' in output
  assert 'ride solo --summoned <token>' in output
  assert '--detach' in output


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
    channel, request = await next_message(server)
    assert request.kind == 'summon'
    assert request.args == {'target': 'dev', 'prompt': 'work', 'timeout': 42.0}
    await server.transport.send(channel, brotocol.mark(message_id(request), 'accepted'))

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
    channel, request = await next_message(server)
    assert request.args[field] == value
    await server.transport.send(channel, brotocol.mark(message_id(request), 'accepted'))
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
    channel, request = await next_message(server)
    await reply(
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
    channel, request = await next_message(server)
    assert request.args == {'target': 'dev', 'prompt': 'work', 'manual': True}
    await server.transport.send(channel, brotocol.mark(message_id(request), 'accepted'))

    assert await task == 0
    assert capsys.readouterr().out == f'{request.id}\n'
    assert (
      summon.manual_launch_command(message_id(request), 'dev')
      == f'/runtime/venv/bin/ride along --summoned {message_id(request)} dev'
    )
    assert summon.manual_launch_command(message_id(request), 'dev') in caplog.text


@pytest.mark.asyncio
async def test_blocking_summon_relays_the_answer(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'dev', 'work']))
    channel, request = await next_message(server)
    await server.transport.send(channel, brotocol.mark(message_id(request), 'accepted'))
    await server.transport.send(channel, brotocol.mark(message_id(request), 'trail', trail_id='T1'))
    await reply(server, channel, request, outcome='ok', value='answer')

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
      asyncio.to_thread(summon.main, ['summon', '--talk', 'summoned.question', 'dev', 'work'])
    )
    channel, request = await next_message(server)
    assert request.args['talk'] == ['summoned.question']
    await server.transport.send(channel, brotocol.mark(message_id(request), 'accepted'))
    await server.transport.send(
      channel, brotocol.message(message_id(request), {'text': 'still working'})
    )
    await server.transport.send(
      channel, brotocol.message(message_id(request), {'text': 'approve?'}, id='QUESTION-1')
    )

    assert await task == quest.QUESTION_EXIT_CODE
    assert capsys.readouterr().out == 'approve?\n'
    assert 'still working' in caplog.text
    assert f"quest say {request.id} '<text>' --reply-to QUESTION-1" in caplog.text


@pytest.mark.asyncio
async def test_silent_blocking_wait_queries_live_state_then_resumes(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--timeout', '0.05', 'dev', 'work'])
    )
    channel, original = await next_message(server)
    await server.transport.send(channel, brotocol.mark(message_id(original), 'accepted'))

    query_channel, query = await next_message(server)
    assert query.kind == 'query'
    assert query.args == {'id': original.id}
    await server.transport.send(
      query_channel, brotocol.result(message_id(original), 'ok', value='answer')
    )
    await reply(
      server,
      query_channel,
      query,
      outcome='ok',
      value={'quest': quest_record(message_id(original), 'started', trail_id='T2')},
    )

    assert await task == 0
    assert capsys.readouterr().out == 'answer\n'


@pytest.mark.asyncio
async def test_silent_blocking_wait_interprets_terminal_query(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--timeout', '0.05', 'dev', 'work'])
    )
    _, original = await next_message(server)
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': quest_record(
          message_id(original),
          'ended',
          result={'outcome': 'ok', 'value': 'retained answer'},
        )
      },
    )

    assert await task == 0
    assert capsys.readouterr().out == 'retained answer\n'


@pytest.mark.asyncio
async def test_silent_blocking_wait_returns_an_open_child_question_from_the_journal(
  monkeypatch, capsys
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--timeout', '0.05', 'dev', 'work'])
    )
    _, original = await next_message(server)
    channel, query = await next_message(server)
    question = entry(1, 'summoned', 'approve?', id='QUESTION-1', pending=True)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': quest_record(
          message_id(original),
          'started',
          talk=['summoned.question', 'summoned.say'],
          messages=[question],
          chat_seq=1,
        )
      },
    )

    assert await task == quest.QUESTION_EXIT_CODE
    assert capsys.readouterr().out == 'approve?\n'


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
