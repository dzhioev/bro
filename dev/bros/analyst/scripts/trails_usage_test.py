from datetime import UTC, datetime

import pytest

from bros.analyst.scripts import trails_usage as generate

_ANTHROPIC = {
  'input_tokens': 2,
  'cache_creation_input_tokens': 300,
  'cache_read_input_tokens': 5000,
  'output_tokens': 80,
}
_OPENAI = {
  'input_tokens': 1000,
  'input_tokens_details': {'cached_tokens': 600, 'cache_write_tokens': 100},
  'output_tokens': 40,
  'total_tokens': 1040,
}


def _header(trail_id, harness, bro, usage_by_model, ended=True):
  return {
    'id': trail_id,
    'harness': harness,
    'bro': bro,
    'usage': usage_by_model,
    'end': {'reason': 'ok'} if ended else None,
  }


class TestFoldHeaders:
  def test_cuts_the_same_spend_every_way(self):
    fold = generate.fold_headers(
      [
        _header('a', 'claude', 'bro-dev', {'claude-opus-5': _ANTHROPIC}),
        _header('b', 'bro', 'ppp-dev', {'gpt-5.6-sol': _OPENAI}),
      ]
    )
    assert fold.trails == 2
    assert fold.by_vendor['anthropic'] == {
      'input': 2,
      'cache_write': 300,
      'cache_read': 5000,
      'output': 80,
    }
    assert fold.by_vendor['openai'] == {
      'input': 300,
      'cache_write': 100,
      'cache_read': 600,
      'output': 40,
    }
    assert fold.by_harness['claude'] == fold.by_bro['bro-dev']
    assert fold.total()['output'] == 120

  def test_snapshots_of_one_model_fold_into_one_row(self):
    # a window spanning a snapshot rotation reports the model once, not twice
    fold = generate.fold_headers(
      [
        _header('a', 'bro', 'bro-dev', {'gpt-5-2025-08-07': _OPENAI}),
        _header('b', 'bro', 'bro-dev', {'gpt-5-2026-01-15': _OPENAI}),
      ]
    )
    assert list(fold.by_model) == ['gpt-5']
    assert fold.by_model['gpt-5']['output'] == 80

  def test_counts_live_trails_and_nameless_bros(self):
    fold = generate.fold_headers(
      [
        _header('a', 'claude', None, {'claude-opus-5': _ANTHROPIC}, ended=False),
        _header('b', 'claude', None, {}, ended=True),
      ]
    )
    assert (fold.live, fold.sessions_by_bro['(none)']) == (1, 2)


class TestResolveWindow:
  def test_days_before_until(self):
    window = generate.resolve_window(None, '2026-08-14T00:00:00+00:00', 30)
    assert window.since == datetime(2026, 7, 15, tzinfo=UTC)
    assert window.days == 30

  def test_empty_window_raises(self):
    with pytest.raises(ValueError, match='empty window'):
      generate.resolve_window('2026-08-14T00:00:00+00:00', '2026-08-01T00:00:00+00:00', 30)


class _StubClient:
  def __init__(self, calls_by_trail):
    self._calls_by_trail = calls_by_trail

  def iter_messages(self, trail_id, *, types=None):
    for call_usage in self._calls_by_trail[trail_id]:
      yield {'type': 'llm_call', 'usage': call_usage}


class TestVerify:
  def test_agreeing_call_stream_reports_nothing(self):
    headers = [_header('a', 'claude', 'bro-dev', {'claude-opus-5': _ANTHROPIC})]
    client = _StubClient({'a': [_ANTHROPIC]})
    assert generate.verify(client, headers, 1) == []

  def test_disagreeing_call_stream_is_reported(self):
    headers = [_header('a', 'claude', 'bro-dev', {'claude-opus-5': _ANTHROPIC})]
    client = _StubClient({'a': [_ANTHROPIC, _ANTHROPIC]})
    discrepancies = generate.verify(client, headers, 1)
    assert [d.trail_id for d in discrepancies] == ['a']
    assert discrepancies[0].calls['output'] == 160

  def test_checks_the_heaviest_first(self):
    light = _header('light', 'claude', 'bro-dev', {'claude-opus-5': _OPENAI})
    heavy = _header('heavy', 'claude', 'bro-dev', {'claude-opus-5': _ANTHROPIC})
    client = _StubClient({'heavy': [], 'light': []})
    assert [d.trail_id for d in generate.verify(client, [light, heavy], 1)] == ['heavy']


class TestCacheBalance:
  def test_reuse_is_reads_per_write(self):
    counts = {'input': 100, 'cache_write': 200, 'cache_read': 1000, 'output': 5}
    row = generate._balance_row('scope', counts)
    assert '| 5.0x |' in row
    assert '| 15.4% | 92.3% |' in row

  def test_a_prefix_read_back_less_than_once_does_not_round_up(self):
    counts = {'input': 0, 'cache_write': 49_018, 'cache_read': 26_511, 'output': 1}
    assert '| 0.5x |' in generate._balance_row('scope', counts)

  def test_large_ratios_drop_the_decimal(self):
    counts = {'input': 0, 'cache_write': 100, 'cache_read': 6_800, 'output': 1}
    assert '| 68x |' in generate._balance_row('scope', counts)

  def test_uncached_scope_reports_no_ratio(self):
    row = generate._balance_row(
      'scope', {'input': 50, 'cache_write': 0, 'cache_read': 0, 'output': 1}
    )
    assert '| — | 0.0% | 0.0% |' in row

  def test_empty_scope_divides_by_nothing(self):
    row = generate._balance_row(
      'scope', {'input': 0, 'cache_write': 0, 'cache_read': 0, 'output': 0}
    )
    assert row.count('—') == 3

  def test_uploaded_excludes_output(self):
    assert generate.uploaded({'input': 1, 'cache_write': 2, 'cache_read': 3, 'output': 400}) == 6


_GENERATED = datetime(2026, 8, 15, 9, 30, tzinfo=UTC)


class TestReportName:
  def test_generation_date_then_slug(self):
    assert generate.report_name('june-2026 cache balance', _GENERATED) == (
      '2026-08-15–june-2026-cache-balance.md'
    )

  def test_slug_is_normalized(self):
    assert generate.report_name('  June 2026 / Testing!  ', _GENERATED).endswith(
      'june-2026-testing.md'
    )

  def test_nameless_slug_raises(self):
    with pytest.raises(ValueError, match='carries no name'):
      generate.report_name('!!!', _GENERATED)


class TestResolveDestination:
  def test_names_the_report_after_the_generation_date(self, tmp_path):
    destination = generate.resolve_destination(
      str(tmp_path / 'x.md'), 'anything', _GENERATED, force=False
    )
    assert destination.name == 'x.md'

  def test_existing_report_is_not_silently_replaced(self, tmp_path):
    existing = tmp_path / 'report.md'
    existing.write_text('a reading this script cannot reproduce')
    with pytest.raises(FileExistsError, match='--force'):
      generate.resolve_destination(str(existing), 'slug', _GENERATED, force=False)
    assert generate.resolve_destination(str(existing), 'slug', _GENERATED, force=True) == existing


class TestRender:
  def _rendered(self, discrepancies, verified):
    window = generate.resolve_window('2026-07-15T00:00:00+00:00', '2026-08-14T00:00:00+00:00', 30)
    fold = generate.fold_headers([_header('a', 'claude', 'bro-dev', {'claude-opus-5': _ANTHROPIC})])
    return generate.render(window, fold, discrepancies, verified, _GENERATED)

  def test_generation_time_is_stated_apart_from_the_window(self):
    body = self._rendered([], 10)
    assert '2026-08-15T09:30:00Z' in body
    assert '2026-07-15T00:00:00Z' in body

  def test_clean_check_is_stated(self):
    assert 'reproduced its header aggregate exactly' in self._rendered([], 10)

  def test_drift_is_stated_rather_than_suppressed(self):
    discrepancy = generate.Discrepancy('a', {'output': 1}, {'output': 2})
    body = self._rendered([discrepancy], 10)
    assert 'unreconciled' in body
    assert '`a`' in body

  def test_skipped_check_is_stated(self):
    assert '--verify 0' in self._rendered([], 0)
