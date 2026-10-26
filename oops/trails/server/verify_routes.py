"""Fixture-driven recording and projection checks against a running server."""

import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen


def verify(base_url: str, token: str, fixtures: Path) -> None:
  def request(path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    with urlopen(
      Request(
        base_url + path,
        data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
      ),
      timeout=10,
    ) as response:
      return json.load(response)

  for case in json.loads(fixtures.read_text()):
    created = request('/v1/trails', case['blaze'])
    path = f'/v1/trails/{created["id"]}'
    appended = request(path + '/records', {'offset': created['extent'], 'records': case['records']})
    assert appended['appended'] == len(case['records']), case['name']
    assert appended['extent'] == created['extent'] + len(case['records']), case['name']
    messages = request(path + '/messages')['messages']
    assert [message['type'] for message in messages] == case['expected_types'], messages
    assistant = [message for message in messages if message['type'] == 'assistant']
    assert assistant[0]['content'] == case['reply'], assistant
    assert assistant[0]['terminal'] is True, assistant
    print(f'{case["name"]}: blaze, append and projection passed', flush=True)


if __name__ == '__main__':
  verify(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
