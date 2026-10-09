#!/usr/bin/env python
import os
from pathlib import Path

import pytest

import bro.llm.usage as usage
from bro.llm.usage import Footer, Usage

OPUS = 'claude-opus-4-8'
HAIKU = 'claude-haiku-4-5-20251001'

# an old single-number footer (pre four-class redesign) — must no longer parse
OLD_FOOTER = "> created with Claude Code 2.1.114 | Opus 4.8: 45'231\n> session(s): abc12345"
# the previous four-class shape (`↑ a / b (c) ↓ d`) — must no longer parse
PREVIOUS_FOOTER = (
  "> created with Claude Code 2.1.181 | Opus 4.8: ↑ 4'812 / 18'903 (1'204'556) ↓ 12'905"
)


def C(input=0, cache_write=0, cache_read=0, output=0):
  return {'input': input, 'cache_write': cache_write, 'cache_read': cache_read, 'output': output}


class TestFromProviderCounts:
  def test_anthropic_fields_are_already_disjoint(self):
    raw = {
      'input_tokens': 2,
      'cache_creation_input_tokens': 300,
      'cache_read_input_tokens': 5_000,
      'output_tokens': 80,
    }
    assert usage.from_vendor_counts(raw) == C(input=2, cache_write=300, cache_read=5_000, output=80)

  def test_absent_anthropic_fields_read_as_zero(self):
    assert usage.from_vendor_counts({'input_tokens': 11, 'output_tokens': 7}) == C(
      input=11, output=7
    )

  def test_openai_details_come_out_of_the_input_total(self):
    raw = {
      'input_tokens': 1_000,
      'input_tokens_details': {'cached_tokens': 600, 'cache_write_tokens': 100},
      'output_tokens': 40,
      'total_tokens': 1_040,
    }
    assert usage.from_vendor_counts(raw) == C(input=300, cache_write=100, cache_read=600, output=40)

  def test_openai_classes_stay_disjoint(self):
    raw = {
      'input_tokens': 8_687,
      'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 8_684},
      'output_tokens': 190,
    }
    counts = usage.from_vendor_counts(raw)
    assert counts['input'] + counts['cache_write'] + counts['cache_read'] == 8_687

  def test_openai_shape_without_input_tokens_raises(self):
    with pytest.raises(KeyError):
      usage.from_vendor_counts({'input_tokens_details': {'cached_tokens': 1}})


class TestFormatInt:
  def test_apostrophe_thousands(self):
    assert usage.format_int(0) == '0'
    assert usage.format_int(5_000) == "5'000"
    assert usage.format_int(45_231) == "45'231"
    assert usage.format_int(168_892) == "168'892"
    assert usage.format_int(1_275_432) == "1'275'432"


class TestVendorOf:
  @pytest.mark.parametrize(
    ('slug', 'vendor'),
    [
      ('claude-opus-5', 'anthropic'),
      ('claude-fable-5', 'anthropic'),
      ('claude-haiku-4-5-20251001', 'anthropic'),
      ('gpt-6.1-sol', 'openai'),
      ('gpt-5-2025-08-07', 'openai'),
      ('o3-mini', 'openai'),
    ],
  )
  def test_known_vendors(self, slug, vendor):
    assert usage.vendor_of(slug) == vendor

  def test_a_claude_slug_bills_anthropic_whichever_surface_ran_it(self):
    # the launch roster calls this same string `claude-code`; who bills it is a
    # separate question with a separate answer
    assert usage.vendor_of('claude-opus-5') == 'anthropic'

  def test_an_unclaimed_slug_raises(self):
    with pytest.raises(ValueError, match='no vendor known'):
      usage.vendor_of('llama-4')


class TestModelFamily:
  def test_known_families(self):
    assert usage.model_family(OPUS) == 'Opus 4.8'
    assert usage.model_family(HAIKU) == 'Haiku 4.5'
    assert usage.model_family('claude-sonnet-4-6') == 'Sonnet 4.6'

  def test_single_number_families(self):
    assert usage.model_family('claude-fable-5') == 'Fable 5'
    assert usage.model_family('claude-mythos-5') == 'Mythos 5'

  def test_openai_snapshot_collapses_to_family(self):
    assert usage.model_family('gpt-5-2025-08-07') == 'gpt-5'
    assert usage.model_family('gpt-5') == 'gpt-5'

  def test_unknown_slug_passes_through(self):
    assert usage.model_family('<synthetic>') == '<synthetic>'
    assert usage.model_family('claude-experimental-99-12') == 'claude-experimental-99-12'

  def test_two_snapshots_of_one_model_share_a_family(self):
    assert usage.model_family('gpt-5-2025-08-07') == usage.model_family('gpt-5-2026-01-15')
    assert usage.model_family('claude-haiku-4-5-20251001') == usage.model_family(
      'claude-haiku-4-5-20260514'
    )

  def test_two_versions_of_one_model_do_not(self):
    # Opus 4.8 and Opus 5 are different models, not snapshots of one
    assert usage.model_family('claude-opus-4-8') != usage.model_family('claude-opus-5')


class TestUsageFile:
  def test_publish_writes_the_processs_own_file_and_points_at_it(self, tmp_path, monkeypatch):
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    monkeypatch.setattr('tempfile.gettempdir', lambda: str(tmp_path))
    usage.publish('bro//dev', {'gpt-5': C(input=10, cache_read=4, output=2)})
    published = Path(os.environ[usage.USAGE_FILE_VARIABLE])
    assert published.parent == tmp_path
    assert usage.read_usage_file(published) == Usage(
      agent='bro//dev', per_model={'gpt-5': C(input=10, cache_read=4, output=2)}
    )

  def test_publish_leaves_an_inherited_pointers_file_alone(self, tmp_path, monkeypatch):
    inherited = tmp_path / 'session-usage.json'
    claude = Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=9)})
    usage.write_usage_file(inherited, claude)
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(inherited))
    monkeypatch.setattr('tempfile.gettempdir', lambda: str(tmp_path / 'tmp'))
    (tmp_path / 'tmp').mkdir()
    usage.publish('bro//dev', {'gpt-5': C(output=1)})
    assert usage.read_usage_file(inherited) == claude
    assert usage.current_usage() == Usage(agent='bro//dev', per_model={'gpt-5': C(output=1)})

  def test_publish_replaces_whole_snapshot(self, tmp_path, monkeypatch):
    monkeypatch.setattr('tempfile.gettempdir', lambda: str(tmp_path))
    usage.publish('bro//dev', {'gpt-5': C(output=1)})
    usage.publish('bro//dev', {'gpt-5': C(output=5)})
    assert usage.current_usage() == Usage(agent='bro//dev', per_model={'gpt-5': C(output=5)})


class TestCurrentUsage:
  def test_reads_the_pointed_file(self, tmp_path, monkeypatch):
    pointer = tmp_path / 'usage.json'
    usage.write_usage_file(pointer, Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=3)}))
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(pointer))
    assert usage.current_usage() == Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=3)})
    assert usage.agent_session()

  def test_a_pending_record_is_an_agent_session_without_usage(self, tmp_path, monkeypatch):
    pointer = tmp_path / 'usage.json'
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(pointer))
    with usage.holding_publisher_lock(pointer) as lock:
      usage.write_usage_pending(pointer, lock)
      assert usage.current_usage() is None
      assert usage.agent_session()
    with pytest.raises(usage.UsageUnavailable, match='no longer keeps it current'):
      usage.current_usage()

  def test_a_pointer_naming_no_file_fails_the_read(self, tmp_path, monkeypatch):
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(tmp_path / 'usage.json'))
    with pytest.raises(usage.UsageUnavailable, match='no usage publisher has written it'):
      usage.current_usage()

  def test_a_failed_publisher_fails_the_read(self, tmp_path, monkeypatch):
    pointer = tmp_path / 'usage.json'
    usage.write_usage_file(pointer, Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=3)}))
    usage.write_usage_failure(pointer, 'OSError: disk full')
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(pointer))
    with pytest.raises(usage.UsageUnavailable, match='disk full'):
      usage.current_usage()

  def test_a_snapshot_naming_a_lock_is_current_while_its_publisher_holds_it(
    self, tmp_path, monkeypatch
  ):
    pointer = tmp_path / 'usage.json'
    current = Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=3)})
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(pointer))
    with usage.holding_publisher_lock(pointer) as lock:
      usage.write_usage_file(pointer, current, lock=lock)
      assert usage.current_usage() == current
    with pytest.raises(usage.UsageUnavailable, match='no longer keeps it current'):
      usage.current_usage()

  def test_without_a_pointer_there_is_no_agent_and_no_usage(self, monkeypatch):
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    assert usage.current_usage() is None
    assert not usage.agent_session()


class TestToLabels:
  def test_collapses_slugs_to_labels(self):
    out = usage.to_labels({OPUS: C(output=100), HAIKU: C(output=5)})
    assert out == {'Opus 4.8': C(output=100), 'Haiku 4.5': C(output=5)}

  def test_same_label_different_date_merges_per_class(self):
    out = usage.to_labels(
      {
        'claude-haiku-4-5-20251001': C(input=1, output=5),
        'claude-haiku-4-5-20260101': C(input=2, output=7),
      }
    )
    assert out == {'Haiku 4.5': C(input=3, output=12)}


class TestFormatFooter:
  def test_single_agent(self):
    out = usage.format_footer(
      ['Claude Code 2.1.114'],
      {'Opus 4.8': C(input=48_787, cache_write=2_103_810, cache_read=41_676_292, output=434_029)},
    )
    assert out == (
      "> created with Claude Code 2.1.114 | Opus 4.8: ↑(48'787 2'103'810 41'676'292) ↓434'029"
    )

  def test_multi_agent_multi_model(self):
    out = usage.format_footer(
      ['Claude Code 2.1.114', 'bro//dev'],
      {'Opus 4.8': C(input=168_892, output=10), 'gpt-5': C(cache_read=5_000)},
    )
    assert out == (
      '> created with Claude Code 2.1.114, bro//dev | '
      "Opus 4.8: ↑(168'892 0 0) ↓10, gpt-5: ↑(0 0 5'000) ↓0"
    )


class TestParseFooter:
  def test_round_trips_format_footer(self):
    agents = ['Claude Code 2.1.114', 'bro//dev']
    tokens = {
      'Opus 4.8': C(input=1, cache_write=2, cache_read=3, output=4),
      'gpt-5': C(output=5_000),
    }
    parsed = usage.parse_footer(usage.format_footer(agents, tokens))
    assert parsed == Footer(agents=agents, delta=tokens)

  def test_single(self):
    parsed = usage.parse_footer(
      "> created with Claude Code 2.1.114 | Opus 4.8: ↑(48'787 2'103'810 41'676'292) ↓434'029"
    )
    assert parsed == Footer(
      agents=['Claude Code 2.1.114'],
      delta={
        'Opus 4.8': C(input=48_787, cache_write=2_103_810, cache_read=41_676_292, output=434_029)
      },
    )

  def test_bro_agent_footer(self):
    parsed = usage.parse_footer('> created with bro//dev | gpt-5: ↑(70 0 30) ↓22')
    assert parsed == Footer(
      agents=['bro//dev'],
      delta={'gpt-5': C(input=70, cache_write=0, cache_read=30, output=22)},
    )

  def test_historic_compressed_versions_normalize_to_full_agents(self):
    # historic squash footers compressed same-agent versions into bare tokens
    parsed = usage.parse_footer(
      '> created with Claude Code 2.1.114, 2.1.120 | Opus 4.8: ↑(1 0 0) ↓2'
    )
    assert parsed is not None
    assert parsed.agents == ['Claude Code 2.1.114', 'Claude Code 2.1.120']

  def test_finds_footer_among_other_lines(self):
    footer = usage.format_footer(['Claude Code 2.1'], {'Opus 4.8': C(output=10)})
    msg = f'fix: a thing\n\nbody text\n\n{footer}\n'
    parsed = usage.parse_footer(msg)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=10)}

  def test_old_single_number_footer_does_not_parse(self):
    assert usage.parse_footer(OLD_FOOTER) is None

  def test_previous_slash_parens_footer_does_not_parse(self):
    assert usage.parse_footer(PREVIOUS_FOOTER) is None

  def test_footerless(self):
    assert usage.parse_footer('chore: bump deps\n\nroutine.\n') is None
    assert usage.parse_footer('') is None
