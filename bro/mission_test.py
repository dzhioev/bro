import asyncio
import contextlib
import json

import pytest

from bro import mission
from bro.broker.environment import BROKER_MISSION
from bro.quest_test_helper import next_message, quest_record, reply, running_server


def _typed_entry(
  sequence: int,
  sender: str,
  payload: dict,
  *,
  question_id: str | None = None,
  reply_to: str | None = None,
  pending: bool = False,
) -> dict:
  entry = {
    'seq': sequence,
    'at': 'now',
    'transition': 'message',
    'from': sender,
    'head': payload,
  }
  if question_id is not None:
    entry['id'] = question_id
  if reply_to is not None:
    entry['reply_to'] = reply_to
  if pending:
    entry['pending'] = True
  return entry


def test_help_lists_every_verb(capsys):
  with pytest.raises(SystemExit):
    mission.main(['mission', '--help'])

  output = capsys.readouterr().out
  for verb in ('check', 'history', 'say', 'ask', 'list', 'watch', 'cancel'):
    assert f'  {verb}' in output


def test_payload_argument_requires_strict_json_objects():
  assert mission.payload_argument('{}') == {}
  assert mission.payload_argument('{"tool":"snapshot"}') == {'tool': 'snapshot'}
  for value in ('[]', '"text"', 'null', '{', '{"value":NaN}'):
    with pytest.raises(ValueError):
      mission.payload_argument(value)


@pytest.mark.asyncio
async def test_say_sends_the_json_object_it_is_given(monkeypatch):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        mission.main,
        ['mission', 'say', 'WEB-1', '{"tool":"snapshot","arguments":{}}'],
      )
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'mission': quest_record(
          'WEB-1', 'started', type='webview', talk=['owner.say', 'worker.say']
        )
      },
    )
    _, message = await next_message(server)

    assert message.payload == {'tool': 'snapshot', 'arguments': {}}
    assert await task == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('verb', ['say', 'ask'])
async def test_talk_refuses_a_typed_move_before_it_is_sent(monkeypatch, caplog, verb):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(mission.main, ['mission', verb, 'WEB-1', '{"step":1}'])
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('WEB-1', 'started', type='webview', talk=[])},
    )

    assert await task == 1
  assert 'forbids this mission chat move' in caplog.text


@pytest.mark.asyncio
async def test_ask_wait_prints_a_typed_reply(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        mission.main,
        ['mission', 'ask', 'WEB-1', '{"tool":"snapshot"}', '--wait', '5'],
      )
    )
    channel, query = await next_message(server)
    live = quest_record('WEB-1', 'started', type='webview', talk=['owner.question', 'worker.say'])
    await reply(server, channel, query, outcome='ok', value={'mission': live})
    _, question = await next_message(server)
    assert question.id is not None
    channel, query = await next_message(server)
    asked = _typed_entry(1, 'owner', {'tool': 'snapshot'}, question_id=question.id, pending=True)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': {**live, 'messages': [asked], 'chat_seq': 1}},
    )
    channel, waiting = await next_message(server)
    answered = _typed_entry(2, 'worker', {'text': 'page'}, reply_to=question.id)
    await reply(
      server,
      channel,
      waiting,
      outcome='ok',
      value={'mission': {**live, 'messages': [asked, answered], 'chat_seq': 2}},
    )

    assert await task == 0
    assert json.loads(capsys.readouterr().out) == {'text': 'page'}


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ('worker_type', 'result'),
  [
    ('bro', {'outcome': 'ok', 'value': 'answer'}),
    ('webview', {'outcome': 'failed', 'error': 'browser exited'}),
  ],
)
async def test_check_prints_the_retained_result_for_every_worker_type(
  monkeypatch, capsys, worker_type, result
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(mission.main, ['mission', 'check', 'M1']))
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('M1', 'ended', type=worker_type, result=result, outcome='ok')},
    )

    assert await task == 0
    output = json.loads(capsys.readouterr().out)
    assert output['type'] == worker_type
    assert output['outcome'] == result['outcome']
    assert output.get('value', output.get('error')) == result.get('value', result.get('error'))


@pytest.mark.asyncio
async def test_history_sequence_reads_the_full_event(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(mission.main, ['mission', 'history', 'WEB-1', '--seq', '12'])
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'mission': quest_record('WEB-1', 'started', type='webview')},
    )
    channel, events = await next_message(server)
    assert events.args == {'after': 11}
    event = {
      'seq': 12,
      'kind': 'launch',
      'type': 'webview',
      'mission': 'WEB-1',
      'parent': 'ROOT',
      'args': {'type': 'webview'},
      'transition': 'message',
      'from': 'worker',
      'head': {'snapshot': {'title': 'Example'}},
    }
    await reply(server, channel, events, outcome='ok', value={'head': 12, 'events': [event]})

    assert await task == 0
    assert json.loads(capsys.readouterr().out) == event


@pytest.mark.asyncio
async def test_list_type_filters_the_fully_read_listing(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(mission.main, ['mission', 'list', '--type', 'webview'])
    )
    channel, query = await next_message(server)
    await reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'missions': [
          quest_record('WEB-1', 'started', type='webview'),
          quest_record('BRO-1', 'started'),
        ]
      },
    )

    assert await task == 0
    assert [record['id'] for record in json.loads(capsys.readouterr().out)['missions']] == ['WEB-1']


@pytest.mark.asyncio
async def test_cancel_accepts_any_owned_worker_type(monkeypatch):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(mission.main, ['mission', 'cancel', 'WEB-1', '--timeout', '1'])
    )
    channel, cancel = await next_message(server)
    assert cancel.kind == 'cancel'
    await reply(server, channel, cancel, outcome='ok')
    channel, query = await next_message(server)
    ended = quest_record(
      'WEB-1',
      'ended',
      type='webview',
      outcome='failed',
      reason='cancelled',
      result={'outcome': 'failed', 'detail': {'reason': 'cancelled'}},
    )
    await reply(server, channel, query, outcome='ok', value={'mission': ended})

    assert await task == 0


@pytest.mark.asyncio
async def test_watch_renders_both_types_and_cuts_an_oversized_chat_line(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = mission.watch(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await next_message(server)
    await reply(server, channel, arm, outcome='ok', value={'head': 0, 'events': []})
    channel, listing = await next_message(server)
    await reply(server, channel, listing, outcome='ok', value={'missions': []})
    channel, poll = await next_message(server)
    events = [
      {
        'seq': 1,
        'kind': 'launch',
        'type': 'bro',
        'mission': 'BRO-1',
        'parent': 'ROOT',
        'args': {'target': 'dev'},
        'transition': 'started',
      },
      {
        'seq': 2,
        'kind': 'launch',
        'type': 'webview',
        'mission': 'WEB-1',
        'parent': 'ROOT',
        'args': {'type': 'webview'},
        'transition': 'message',
        'from': 'worker',
        'head': {'snapshot': 'x' * 1500},
      },
    ]
    await reply(server, channel, poll, outcome='ok', value={'head': 2, 'events': events})

    assert await first_line == 'mission BRO-1 (bro to dev) started'
    cut = await asyncio.to_thread(next, watch)
    assert len(cut.encode()) == mission.WATCH_LINE_BYTES
    assert cut.endswith('[1544 bytes, cut; seq 2 in mission history WEB-1]')
    watch.close()


def test_watch_renders_an_untyped_denial_without_guessing_a_worker_type():
  event = {
    'seq': 3,
    'kind': 'launch',
    'type': None,
    'mission': 'DENIED-1',
    'parent': 'ROOT',
    'args': {},
    'transition': 'denied',
    'reason': 'unknown worker type',
  }

  assert mission._event_line(event, 'ROOT') == (
    'mission DENIED-1 (unknown) denied unknown worker type'
  )


def test_watch_line_cut_preserves_a_unicode_boundary():
  event = {'seq': 7, 'mission': 'WEB-1'}

  line = mission._bounded_chat_line('🙂' * 400, event)

  assert len(line.encode()) <= mission.WATCH_LINE_BYTES
  assert line.endswith('[1600 bytes, cut; seq 7 in mission history WEB-1]')


def test_payload_at_the_message_bound_is_checked_before_a_channel_opens(monkeypatch):
  monkeypatch.delenv('BROKER_CHANNEL', raising=False)
  payload = {'value': 'x' * 20_000}

  assert mission.main(['mission', 'say', 'M1', json.dumps(payload)]) == 1


def test_watch_type_filter_is_available_as_a_library_argument():
  with pytest.raises(ValueError, match='non-empty'):
    with contextlib.closing(mission.watch('')) as watch:
      next(watch)
