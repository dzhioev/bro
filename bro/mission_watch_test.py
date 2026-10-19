import asyncio
import contextlib

import pytest

from bro import mission, quest, summon, watches
from bro.broker import brotocol
from bro.broker.client import Client
from bro.broker.environment import BROKER_CHANNEL, BROKER_MISSION, BROKER_TALK
from bro.broker.transport import connect
from bro.broker.transports.tcp import LOCAL_HOST
from bro.quest_test_helper import (
  TIMEOUT,
  entry,
  next_message,
  quest_record,
  reply,
  reply_empty_watch_replay,
  running_live_broker,
  running_server,
)


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
      original_read = mission._read_value
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

      monkeypatch.setattr(mission, '_read_value', inject_racing_message)
      with contextlib.closing(mission.watch(wait_seconds=0.05)) as watch:
        assert await asyncio.to_thread(next, watch) == (
          'before the watch: summoner says start with docs'
        )
        assert await asyncio.to_thread(next, watch) == (
          f'before the watch: mission {grandchild_endpoint.quest} (bro to reviewer) '
          f'asks ship this? (question {grandchild_question.id})'
        )
        assert await asyncio.to_thread(next, watch) == 'summoner says also run lint'


@pytest.mark.asyncio
async def test_watch_replays_retained_chat_at_arm_without_repeating_a_racing_message(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = mission.watch(wait_seconds=0.05)
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
      'before the watch: mission CHILD (bro to dev) asks ship this? (question CHILD-QUESTION)'
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
    watch = mission.watch(wait_seconds=0.05)
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
    watch = mission.watch(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await next_message(server)
    assert arm.kind == 'events'
    assert arm.args == {}
    await reply(server, channel, arm, outcome='ok', value={'head': 10, 'events': []})
    await reply_empty_watch_replay(server)
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
    assert await first_line == watches.quiet('mission B1 (benchmark) started')
    assert (
      await asyncio.to_thread(next, watch)
      == 'mission S1 (bro to reviewer) denied summon denied: not allowed'
    )

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
    assert await second_line == 'mission S2 (bro to dev) ended failed:timeout'

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
    assert await third_line == "mission S3 (bro to x\\ny) denied unknown bro 'x\\ny'"

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
      'mission S4 (bro to dev) refused quest talk lacks owner.say: blocked'
    )
    watch.close()


@pytest.mark.asyncio
async def test_watch_quiets_service_marks_and_a_childs_says(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = mission.watch(wait_seconds=0.05)
    lines = asyncio.create_task(asyncio.to_thread(lambda: [next(watch) for _ in range(7)]))
    channel, arm = await next_message(server)
    await reply(server, channel, arm, outcome='ok', value={'head': 0, 'events': []})
    await reply_empty_watch_replay(server)
    channel, poll = await next_message(server)
    child = {
      'kind': 'launch',
      'type': 'bro',
      'mission': 'S1',
      'parent': 'ROOT',
      'args': {'target': 'dev'},
    }
    message = {**child, 'transition': 'message', 'from': 'worker'}
    await reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 8,
        'events': [
          {**child, 'seq': 1, 'transition': 'accepted'},
          {**child, 'seq': 2, 'transition': 'started'},
          {**child, 'seq': 3, 'transition': 'trail', 'trail_id': 'T9'},
          {**child, 'seq': 4, 'transition': 'listening'},
          {**child, 'mission': 'ROOT', 'parent': 'PARENT', 'seq': 5, 'transition': 'listening'},
          {**message, 'seq': 6, 'head': {'text': 'halfway'}},
          {**message, 'seq': 7, 'id': 'Q1', 'head': {'text': 'approve?'}},
          {**child, 'seq': 8, 'transition': 'ended', 'outcome': 'ok'},
        ],
      },
    )

    assert await lines == [
      watches.quiet('mission S1 (bro to dev) accepted'),
      watches.quiet('mission S1 (bro to dev) started'),
      watches.quiet('mission S1 (bro to dev) trail T9'),
      watches.quiet('mission S1 (bro to dev) listening'),
      watches.quiet('mission S1 (bro to dev) says halfway'),
      'mission S1 (bro to dev) asks approve? (question Q1)',
      'mission S1 (bro to dev) ended ok',
    ]
    watch.close()


@pytest.mark.asyncio
async def test_watch_rearm_replays_only_chat_past_the_last_yielded_entry(monkeypatch):
  async with running_server(monkeypatch) as server:
    with contextlib.closing(mission.watch(wait_seconds=0.05)) as watch:
      before_gap = entry(8, 'owner', 'already delivered')
      first_line = asyncio.create_task(asyncio.to_thread(next, watch))
      channel, arm = await next_message(server)
      await reply(server, channel, arm, outcome='ok', value={'head': 10, 'events': []})
      channel, own_query = await next_message(server)
      await reply(
        server,
        channel,
        own_query,
        outcome='ok',
        value={
          'mission': quest_record('ROOT', 'started', kind='root', messages=[before_gap], chat_seq=8)
        },
      )
      channel, listing = await next_message(server)
      await reply(server, channel, listing, outcome='ok', value={'missions': []})
      assert await first_line == 'before the watch: summoner says already delivered'

      gap_line = asyncio.create_task(asyncio.to_thread(next, watch))
      channel, poll = await next_message(server)
      assert poll.args == {'after': 10, 'wait': 0.05}
      await reply(server, channel, poll, outcome='denied', error='events gap: oldest is 15')
      channel, rearm = await next_message(server)
      assert rearm.args == {}
      await reply(server, channel, rearm, outcome='ok', value={'head': 20, 'events': []})
      assert await gap_line == 'mission watch gap: events gap: oldest is 15; re-armed at 20'

      replay_line = asyncio.create_task(asyncio.to_thread(next, watch))
      retained = entry(18, 'owner', 'while disconnected')
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
            messages=[before_gap, retained],
            chat_seq=18,
          )
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
      assert await resumed == 'mission S1 (bro to dev) denied not allowed'


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
  assert mission._chat_event_line(child, 'ROOT') == (
    'mission CHILD (bro to dev) asks approve? (question QUESTION-1)'
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
  assert mission._chat_event_line(own, 'ROOT') == 'summoner replies approved (to QUESTION-2)'
  assert mission._chat_event_line({**own, 'from': 'worker'}, 'ROOT') is None
  refused = {
    **child,
    'transition': 'refused',
    'from': 'owner',
    'id': None,
    'reason': 'quest talk lacks owner.say',
    'head': {'text': 'blocked'},
  }
  assert mission._chat_event_line(refused, 'ROOT') == (
    'mission CHILD (bro to dev) refused quest talk lacks owner.say: blocked'
  )
  counter = {**child, 'id': 'COUNTER', 'reply_to': 'ORIGINAL'}
  assert mission._chat_event_line(counter, 'ROOT') == (
    'mission CHILD (bro to dev) asks approve? (question COUNTER, to ORIGINAL)'
  )
  own_counter = {**own, 'id': 'COUNTER', 'reply_to': 'ORIGINAL', 'head': {'text': 'which?'}}
  assert mission._chat_event_line(own_counter, 'ROOT') == (
    'summoner asks which? (question COUNTER, to ORIGINAL)'
  )
  oversized = {
    **refused,
    'reason': 'over the message bound',
    'head': {'head': '{"text":"xx', 'truncated': True},
  }
  assert mission._chat_event_line(oversized, 'ROOT') == (
    'mission CHILD (bro to dev) refused over the message bound'
  )


def test_the_watch_line_can_carry_any_registered_name_whole():
  from bro.broker.journal import ARGS_STRING_HEAD
  from bro.registry import MAX_NAME_LENGTH

  assert MAX_NAME_LENGTH <= ARGS_STRING_HEAD


@pytest.mark.asyncio
async def test_watch_refuses_a_quest_this_session_did_not_summon(monkeypatch):
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    watch = mission.watch(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await next_message(server)
    await reply(server, channel, arm, outcome='ok', value={'head': 0, 'events': []})
    await reply_empty_watch_replay(server)
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
    with pytest.raises(quest.QuestError, match='did not launch'):
      await first_line


@pytest.mark.asyncio
async def test_watch_refuses_to_start_without_the_quest_this_session_answers(monkeypatch):
  async with running_server(monkeypatch):
    monkeypatch.delenv(BROKER_MISSION, raising=False)
    watch = mission.watch(wait_seconds=0.05)
    with pytest.raises(quest.QuestError, match=BROKER_MISSION):
      await asyncio.to_thread(next, watch)


@pytest.mark.asyncio
@pytest.mark.parametrize('replayed', [False, True])
async def test_operator_overrides_deliver_the_contract_once_without_hiding_worker_events(
  monkeypatch, tmp_path, replayed
):
  from bro.authority_view_test_helper import publish
  from bro.harness import get_harness
  from bro.monitor import SESSION_DIR_ENV
  from bro.prompts import save_deferred_contract, take_deferred_contract

  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
  publish(monkeypatch)
  save_deferred_contract(harness=get_harness('bro'), hold='unattended')
  overrides = [
    {
      'seq': sequence,
      'kind': 'root',
      'mission': 'ROOT',
      'transition': 'override',
      'operation': f'operation-{sequence}',
      'changes': [[verb, '@bro']],
      'may_summon': ['bro'] if verb == 'grant' else [],
    }
    for sequence, verb in [(4, 'grant'), (5, 'revoke'), (6, 'grant')]
  ]
  worker_event = {
    'seq': 7,
    'kind': 'launch',
    'type': 'benchmark',
    'mission': 'BENCHMARK',
    'parent': 'ROOT',
    'args': {},
    'transition': 'ended',
    'outcome': 'ok',
  }
  async with running_server(monkeypatch) as server:
    monkeypatch.setenv(BROKER_MISSION, 'ROOT')
    with contextlib.closing(mission.watch(wait_seconds=0.05)) as watch:
      first_line = asyncio.create_task(asyncio.to_thread(next, watch))
      channel, arm = await next_message(server)
      await reply(server, channel, arm, outcome='ok', value={'head': 6 if replayed else 3})
      channel, own_query = await next_message(server)
      await reply(
        server,
        channel,
        own_query,
        outcome='ok',
        value={
          'mission': {
            **quest_record('ROOT', 'started', kind='root'),
            'overrides': overrides if replayed else [],
          }
        },
      )
      channel, listing = await next_message(server)
      await reply(server, channel, listing, outcome='ok', value={'missions': []})
      if not replayed:
        channel, poll = await next_message(server)
        await reply(
          server,
          channel,
          poll,
          outcome='ok',
          value={'head': 7, 'events': [*overrides, worker_event]},
        )
      assert await first_line == 'operator override operation-4 applied: --grant @bro'
      assert '# Summoning session' in await asyncio.to_thread(next, watch)
      assert await asyncio.to_thread(next, watch) == (
        'operator override operation-5 applied: --revoke @bro'
      )
      assert await asyncio.to_thread(next, watch) == (
        'operator override operation-6 applied: --grant @bro'
      )
      worker_line = asyncio.create_task(asyncio.to_thread(next, watch))
      if replayed:
        channel, poll = await next_message(server)
        await reply(
          server, channel, poll, outcome='ok', value={'head': 7, 'events': [worker_event]}
        )
      assert await worker_line == 'mission BENCHMARK (benchmark) ended ok'
      assert take_deferred_contract() is None
