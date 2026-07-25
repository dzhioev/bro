import io
import json

from boto3.dynamodb.types import TypeSerializer

from trails import session_migration
from trails.migrate_sessions import _resolve_summoners
from trails.verify_session_migration import _verify_target_source


def _line(record: dict) -> bytes:
  return (json.dumps(record, separators=(',', ':')) + '\n').encode()


def _record(
  record_type: str,
  timestamp: str,
  session_id: str,
  *,
  content=None,
  version: str = '2.1.0',
  message_id: str = 'message-1',
) -> dict:
  record: dict = {
    'type': record_type,
    'timestamp': timestamp,
    'sessionId': session_id,
    'version': version,
  }
  if record_type == 'user':
    record['message'] = {'content': content}
  if record_type == 'assistant':
    record['message'] = {
      'id': message_id,
      'model': 'claude-test',
      'usage': {'input_tokens': 10, 'output_tokens': 2},
      'content': content,
    }
  return record


def _source(body: bytes, *, orphan: bool = False, **item_overrides) -> session_migration.Source:
  item = None
  identity = '11111111-1111-4111-8111-111111111111'
  if not orphan:
    identity = '01kabcde12-34567890-abcdefgh'
    item = {
      'session_id': identity,
      'started_at': '2026-01-01T00:00:00Z',
      'synced_at': '2026-01-01T00:10:00Z',
      'workspace': 'work',
      'host': 'container-id',
      'is_container': True,
      'cw_command': 'cw ss --bro ppp-dev work',
      'model': 'claude-launch',
      'context': '[{"title":"task"}]',
      **item_overrides,
    }
  return session_migration.Source(
    identity=identity,
    key=f'logs/work/{identity}.jsonl',
    body=body,
    modified_at='2026-01-01T00:11:00+00:00',
    table_item=item,
  )


def test_plans_verified_lifetimes_as_a_fork_chain_and_preserves_final_legacy_id():
  first = _line(_record('user', '2026-01-01T00:00:01Z', 'segment-a', content='first'))
  leave = _line(
    {
      'type': session_migration.EVENT_TYPE,
      'subtype': 'leave',
      'timestamp': '2026-01-01T00:01:00Z',
      'sessionId': 'segment-a',
    }
  )
  resume = _line(
    {
      'type': session_migration.EVENT_TYPE,
      'subtype': 'resume',
      'timestamp': '2026-01-01T00:02:00Z',
      'sessionId': 'segment-b',
      'previousSessionId': 'segment-a',
      'historyVerified': True,
    }
  )
  second = _line(_record('user', '2026-01-01T00:02:01Z', 'segment-b', content='second'))
  source = _source(first + leave + resume + second)

  plan = session_migration.plan_source(source)

  assert len(plan.trails) == 2
  parent, leaf = plan.trails
  assert parent.artifact == first
  assert leaf.artifact == second
  assert leaf.header['id'] == source.identity
  assert leaf.header['forked_from'] == {'trail_id': parent.header['id'], 'step_id': '0'}
  assert parent.header['end'] == {'at': '2026-01-01T00:01:00Z', 'reason': 'ok'}
  assert leaf.header['end'] == {'at': '2026-01-01T00:10:00Z', 'reason': 'ok'}
  assert plan.marker_bytes == len(leave) + len(resume)
  assert sum(len(trail.artifact) for trail in plan.trails) + plan.marker_bytes == len(source.body)


def test_unverified_boundary_starts_a_root_and_marks_untrustworthy_parent_lost():
  first = _line(_record('user', '2026-01-01T00:00:01Z', 'segment-a', content='first'))
  resume = _line(
    {
      'type': session_migration.EVENT_TYPE,
      'subtype': 'resume',
      'timestamp': '2026-01-01T00:02:00Z',
      'sessionId': 'segment-b',
      'previousSessionId': 'segment-a',
      'historyVerified': False,
    }
  )
  second = _line(_record('user', '2026-01-01T00:02:01Z', 'segment-b', content='second'))

  plan = session_migration.plan_source(_source(first + resume + second))

  assert plan.trails[0].header['end']['reason'] == 'lost'
  assert 'forked_from' not in plan.trails[1].header


def test_orphan_keeps_uuid_on_root_and_is_lost_without_a_final_leave():
  body = _line(_record('user', '2026-01-01T00:00:01Z', 'segment-a', content='first'))
  source = _source(body, orphan=True)

  plan = session_migration.plan_source(source)

  assert plan.trails[0].header['id'] == source.identity
  assert plan.trails[0].header['end']['reason'] == 'lost'


def test_derives_per_split_usage_turns_and_final_launch_metadata():
  first_user = _line(_record('user', '2026-01-01T00:00:01Z', 'segment-a', content='subject'))
  assistant_block_one = _line(
    _record('assistant', '2026-01-01T00:00:02Z', 'segment-a', content=[{'type': 'text'}])
  )
  assistant_block_two = _line(
    _record('assistant', '2026-01-01T00:00:03Z', 'segment-a', content=[{'type': 'thinking'}])
  )
  source = _source(first_user + assistant_block_one + assistant_block_two)

  trail = session_migration.plan_source(source).trails[0]

  assert trail.header['turn_count'] == 1
  assert trail.header['subject'] == 'subject'
  assert trail.header['bro'] == 'ppp-dev'
  assert trail.header['location'] == {'workspace': 'work', 'is_container': True}
  assert trail.header['native']['usage'] == {
    'claude-test': {'input_tokens': 10, 'output_tokens': 2}
  }
  assert trail.header['native']['llm'] == {'model': 'claude-launch'}
  assert trail.header['native']['cw_command'] == 'cw ss --bro ppp-dev work'
  assert trail.context == b'[{"title":"task"}]'


def test_missing_segment_artifact_stays_a_single_degenerate_root():
  marker = _line(
    {
      'type': session_migration.EVENT_TYPE,
      'subtype': 'missing-segment',
      'timestamp': '2026-01-01T00:00:00Z',
      'sessionId': 'segment-a',
    }
  )
  native = _line(_record('user', '2026-01-01T00:00:01Z', 'segment-a', content='hello'))

  plan = session_migration.plan_source(_source(marker + native))

  assert plan.degenerate is True
  assert len(plan.trails) == 1
  assert plan.trails[0].artifact == marker + native
  assert plan.marker_bytes == 0


def test_raised_is_applied_only_to_the_final_leaf():
  first = _line(
    _record(
      'assistant',
      '2026-01-01T00:00:01Z',
      'segment-a',
      content=[
        {
          'type': 'tool_use',
          'name': session_migration.RAISE_TOOL,
          'input': {'reason': 'old raise'},
        }
      ],
    )
  )
  leave = _line(
    {
      'type': session_migration.EVENT_TYPE,
      'subtype': 'leave',
      'timestamp': '2026-01-01T00:01:00Z',
      'sessionId': 'segment-a',
    }
  )
  resume = _line(
    {
      'type': session_migration.EVENT_TYPE,
      'subtype': 'resume',
      'timestamp': '2026-01-01T00:02:00Z',
      'sessionId': 'segment-a',
      'previousSessionId': 'segment-a',
      'historyVerified': True,
    }
  )
  final = _line(_record('user', '2026-01-01T00:02:01Z', 'segment-a', content='continued'))

  plan = session_migration.plan_source(
    _source(first + leave + resume + final, raised='final raise')
  )

  assert plan.trails[0].header['end']['reason'] == 'ok'
  assert plan.trails[1].header['end'] == {
    'at': '2026-01-01T00:10:00Z',
    'reason': 'raised',
    'detail': 'final raise',
  }


def test_resolves_container_workspace_summoners_only_inside_one_lifetime():
  claude = {
    'id': 'claude-1',
    'started_at': '2026-01-01T00:00:00Z',
    'end': {'at': '2026-01-01T01:00:00Z', 'reason': 'ok'},
    'location': {'workspace': 'work', 'is_container': True},
  }
  bro = {
    'id': 'bro-1',
    'started_at': '2026-01-01T00:30:00Z',
    'native': {'legacy_summoner': {'session': 'c:work'}},
  }

  resolved, unresolved = _resolve_summoners([claude], [bro])

  assert resolved == [(bro, claude)]
  assert unresolved == []


def test_verifier_checks_header_artifact_usage_and_context():
  plan = session_migration.plan_source(
    _source(_line(_record('user', '2026-01-01T00:00:01Z', 'segment-a', content='subject')))
  )
  manifest = session_migration.manifest_source(plan)
  serializer = TypeSerializer()
  raw_headers = {
    trail.header['id']: {key: serializer.serialize(value) for key, value in trail.header.items()}
    for trail in plan.trails
  }
  objects = {}
  for trail in plan.trails:
    objects[trail.header['native']['s3_key']] = trail.artifact
    if trail.context is not None:
      objects[trail.header['native']['context_s3']] = trail.context

  class Dynamo:
    def get_item(self, **arguments):
      trail_id = arguments['Key']['id']['S']
      return {'Item': raw_headers[trail_id]}

  class S3:
    def get_object(self, **arguments):
      return {'Body': io.BytesIO(objects[arguments['Key']])}

  _verify_target_source(Dynamo(), S3(), 'trails-v2', 'bucket', manifest)
