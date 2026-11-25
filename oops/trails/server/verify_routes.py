"""Fixture-driven recording and projection checks against a running server."""

import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from bro.base import log
from bro.base.lulid import lulid


def verify(base_url: str, token: str, fixtures: Path) -> None:
  def request(path: str, payload: dict | None = None, *, status: int = 200) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    with urlopen(
      Request(
        base_url + path,
        data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
      ),
      timeout=10,
    ) as response:
      assert response.status == status, (path, response.status, status)
      return json.load(response)

  for case in json.loads(fixtures.read_text()):
    created = request('/v1/trails', case['start'], status=201)
    assert created['created'] is True
    if 'id' in case['start']:
      repeated = request('/v1/trails', case['start'])
      assert repeated == {**created, 'created': False}, repeated
    path = f'/v1/trails/{created["id"]}'
    appended = request(path + '/records', {'offset': created['extent'], 'records': case['records']})
    assert appended['appended'] == len(case['records']), case['name']
    assert appended['extent'] == created['extent'] + len(case['records']), case['name']
    if 'id' in case['start']:
      repeated = request('/v1/trails', case['start'])
      assert repeated['extent'] == appended['extent'], repeated
      assert repeated['created'] is False, repeated
    if 'fork_kind' in case:
      edge = {'trail_id': created['id'], 'step_id': 0, 'kind': case['fork_kind']}
      child = request(
        '/v1/trails', {**case['start'], 'id': lulid(), 'forked_from': edge}, status=201
      )
      header = request(f'/v1/trails/{child["id"]}')
      assert header['forked_from'] == edge, header
    messages = request(path + '/messages')['messages']
    assert [message['type'] for message in messages] == case['expected_types'], messages
    assistant = [message for message in messages if message['type'] == 'assistant']
    assert assistant[0]['content'] == case['reply'], assistant
    assert assistant[0]['terminal'] is True, assistant
    log.info('%s: recording and projection checks passed', case['name'])


if __name__ == '__main__':
  verify(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
