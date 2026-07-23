from trails.migrate_headers import migrate_header


def _header(**overrides) -> dict:
  header = {
    'trail_id': 'trail-1',
    'bro': 'dev',
    'bro_version': 1,
    'llm_spec': {'type': 'chat_gpt', 'model': 'gpt-5'},
    'started_at': '2026-01-01T00:00:00Z',
    'ended_at': '2026-01-01T00:01:00Z',
    'end_reason': 'terminal',
    'last_alive_at': '2026-01-01T00:01:00Z',
    'interactive': False,
    'entry_point': 'cli:bro_run',
    'parent': None,
    'aggregates': {'step_counts_by_kind': {'system_prompt': 1, 'user_input': 2, 'llm_call': 2}},
  }
  header.update(overrides)
  return header


def _llm_call(input_tokens: int, cached_tokens: int, output_tokens: int) -> dict:
  return {
    'kind': 'llm_call',
    'body': {
      'response': {
        'model': 'gpt-5',
        'usage': {
          'input_tokens': input_tokens,
          'input_tokens_details': {'cached_tokens': cached_tokens},
          'output_tokens': output_tokens,
          'output_tokens_details': {'reasoning_tokens': 3},
          'total_tokens': input_tokens + output_tokens,
        },
      }
    },
  }


def test_migrates_final_header_schema_and_raw_usage():
  item = migrate_header(_header(), [_llm_call(100, 40, 20), _llm_call(50, 10, 5)])
  assert item['id'] == 'trail-1'
  assert item['harness'] == 'bro'
  assert item['version'] == '1'
  assert item['surface'] == 'ask'
  assert item['end'] == {'at': '2026-01-01T00:01:00Z', 'reason': 'ok'}
  assert item['turn_count'] == 2
  assert item['native']['step_counts_by_kind']['llm_call'] == 2
  assert item['native']['usage']['gpt-5'] == {
    'input_tokens': 150,
    'input_tokens_details': {'cached_tokens': 50},
    'output_tokens': 25,
    'output_tokens_details': {'reasoning_tokens': 6},
    'total_tokens': 175,
  }


def test_migrates_lineage_and_direct_trail_provenance():
  item = migrate_header(
    _header(
      entry_point='fork',
      parent={'trail_id': 'parent', 'step_id': 'step', 'relationship': 'fork'},
      summoner={'target': 'pm', 'trail_id': 'summoner'},
    ),
    [],
  )
  assert item['surface'] == 'call'
  assert item['forked_from'] == {'trail_id': 'parent', 'step_id': 'step'}
  assert item['forked_from_id'] == 'parent'
  assert item['summoned_by'] == {'trail_id': 'summoner'}


def test_retains_unresolved_session_summoner_only_in_native():
  item = migrate_header(_header(summoner={'session': 'c:workspace'}), [])
  assert 'summoned_by' not in item
  assert item['native']['legacy_summoner'] == {'session': 'c:workspace'}


def test_recovers_error_detail_when_present():
  item = migrate_header(
    _header(end_reason='error'),
    [{'kind': 'error', 'body': {'message': 'boom', 'traceback': '...'}}],
  )
  assert item['end'] == {
    'at': '2026-01-01T00:01:00Z',
    'reason': 'error',
    'detail': 'boom',
  }
