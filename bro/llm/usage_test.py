#!/usr/bin/env python
import json
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
    assert usage.from_provider_counts(raw) == C(
      input=2, cache_write=300, cache_read=5_000, output=80
    )

  def test_absent_anthropic_fields_read_as_zero(self):
    assert usage.from_provider_counts({'input_tokens': 11, 'output_tokens': 7}) == C(
      input=11, output=7
    )

  def test_openai_details_come_out_of_the_input_total(self):
    raw = {
      'input_tokens': 1_000,
      'input_tokens_details': {'cached_tokens': 600, 'cache_write_tokens': 100},
      'output_tokens': 40,
      'total_tokens': 1_040,
    }
    assert usage.from_provider_counts(raw) == C(
      input=300, cache_write=100, cache_read=600, output=40
    )

  def test_openai_classes_stay_disjoint(self):
    raw = {
      'input_tokens': 8_687,
      'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 8_684},
      'output_tokens': 190,
    }
    counts = usage.from_provider_counts(raw)
    assert counts['input'] + counts['cache_write'] + counts['cache_read'] == 8_687

  def test_openai_shape_without_input_tokens_raises(self):
    with pytest.raises(KeyError):
      usage.from_provider_counts({'input_tokens_details': {'cached_tokens': 1}})


class TestFormatInt:
  def test_apostrophe_thousands(self):
    assert usage.format_int(0) == '0'
    assert usage.format_int(5_000) == "5'000"
    assert usage.format_int(45_231) == "45'231"
    assert usage.format_int(168_892) == "168'892"
    assert usage.format_int(1_275_432) == "1'275'432"


class TestModelLabel:
  def test_known_families(self):
    assert usage.model_label(OPUS) == 'Opus 4.8'
    assert usage.model_label(HAIKU) == 'Haiku 4.5'
    assert usage.model_label('claude-sonnet-4-6') == 'Sonnet 4.6'

  def test_single_number_families(self):
    assert usage.model_label('claude-fable-5') == 'Fable 5'
    assert usage.model_label('claude-mythos-5') == 'Mythos 5'

  def test_openai_snapshot_collapses_to_family(self):
    assert usage.model_label('gpt-5-2025-08-07') == 'gpt-5'
    assert usage.model_label('gpt-5') == 'gpt-5'

  def test_unknown_slug_passes_through(self):
    assert usage.model_label('<synthetic>') == '<synthetic>'
    assert usage.model_label('claude-experimental-99-12') == 'claude-experimental-99-12'


class TestUsageFile:
  def test_publish_writes_env_pointed_file(self, tmp_path, monkeypatch):
    pointer = tmp_path / 'usage.json'
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(pointer))
    usage.publish('bro//dev', {'gpt-5': C(input=10, cache_read=4, output=2)})
    read = usage.read_usage_file(pointer)
    assert read == Usage(agent='bro//dev', per_model={'gpt-5': C(input=10, cache_read=4, output=2)})

  def test_publish_mints_pointer_when_absent(self, tmp_path, monkeypatch):
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    monkeypatch.setattr('tempfile.gettempdir', lambda: str(tmp_path))
    usage.publish('bro//dev', {'gpt-5': C(output=1)})
    minted = os.environ[usage.USAGE_FILE_VARIABLE]
    assert minted.startswith(str(tmp_path))
    assert usage.read_usage_file(Path(minted)).agent == 'bro//dev'

  def test_publish_replaces_whole_snapshot(self, tmp_path, monkeypatch):
    pointer = tmp_path / 'usage.json'
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(pointer))
    usage.publish('bro//dev', {'gpt-5': C(output=1)})
    usage.publish('bro//dev', {'gpt-5': C(output=5)})
    assert usage.read_usage_file(pointer).per_model == {'gpt-5': C(output=5)}


class TestTranscriptUsage:
  def _write(self, path, rows):
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

  def _msg(self, model, input=0, cache_write=0, cache_read=0, output=0):
    return {
      'message': {
        'model': model,
        'usage': {
          'input_tokens': input,
          'cache_creation_input_tokens': cache_write,
          'cache_read_input_tokens': cache_read,
          'output_tokens': output,
        },
      }
    }

  def test_sums_per_model_per_class(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(
      p,
      [
        self._msg(OPUS, input=2, cache_write=3, cache_read=4, output=10),
        self._msg(OPUS, input=1, cache_write=1, cache_read=1, output=5),
        self._msg(HAIKU, output=3),
      ],
    )
    assert usage.transcript_usage(p) == {
      OPUS: C(input=3, cache_write=4, cache_read=5, output=15),
      HAIKU: C(output=3),
    }

  def test_missing_fields_default_to_zero(self, tmp_path):
    p = tmp_path / 't.jsonl'
    # only output present in the usage block
    p.write_text(json.dumps({'message': {'model': OPUS, 'usage': {'output_tokens': 7}}}) + '\n')
    assert usage.transcript_usage(p) == {OPUS: C(output=7)}

  def test_skips_synthetic(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(p, [self._msg(OPUS, output=10), self._msg('<synthetic>', output=999)])
    assert usage.transcript_usage(p) == {OPUS: C(output=10)}

  def test_all_synthetic_yields_empty(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(p, [self._msg('<synthetic>', output=12), self._msg('<synthetic>', output=7)])
    assert usage.transcript_usage(p) == {}


class TestSessionTranscripts:
  SESSION = '786d6a80-6929-4c9b-aac7-4fdfbe98ec3c'

  def _session(self, tmp_path, monkeypatch, *, subagents=()):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'cw-sessions' / 'w'))
    projects = tmp_path / 'cw-sessions' / 'w' / 'projects' / '-ws'
    projects.mkdir(parents=True)
    segment = projects / f'{self.SESSION}.jsonl'
    segment.touch()
    for name in subagents:
      sidecar = projects / self.SESSION / 'subagents' / f'{name}.jsonl'
      sidecar.parent.mkdir(parents=True, exist_ok=True)
      sidecar.touch()
    return segment

  def test_session_id_resolves_the_segment_under_the_config_root(self, tmp_path, monkeypatch):
    segment = self._session(tmp_path, monkeypatch)
    monkeypatch.setenv(usage.SESSION_ID_VARIABLE, self.SESSION)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PWD', str(tmp_path))
    assert usage.session_transcripts() == [segment]

  def test_includes_subagent_sidecars(self, tmp_path, monkeypatch):
    segment = self._session(tmp_path, monkeypatch, subagents=('agent-a', 'agent-b'))
    monkeypatch.setenv(usage.SESSION_ID_VARIABLE, self.SESSION)
    sidecars = segment.parent / self.SESSION / 'subagents'
    assert usage.session_transcripts() == [
      segment,
      sidecars / 'agent-a.jsonl',
      sidecars / 'agent-b.jsonl',
    ]

  def test_falls_back_to_the_working_directory_project(self, tmp_path, monkeypatch):
    monkeypatch.delenv(usage.SESSION_ID_VARIABLE, raising=False)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'config'))
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    projects = tmp_path / 'config' / 'projects' / str(workspace).replace('/', '-')
    projects.mkdir(parents=True)
    (projects / 'older.jsonl').touch()
    newest = projects / 'newest.jsonl'
    newest.touch()
    os.utime(projects / 'older.jsonl', (0, 0))
    monkeypatch.chdir(workspace)
    monkeypatch.setenv('PWD', str(workspace))
    assert usage.session_transcripts() == [newest]

  def test_no_transcript_yields_empty(self, tmp_path, monkeypatch):
    monkeypatch.delenv(usage.SESSION_ID_VARIABLE, raising=False)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PWD', str(tmp_path))
    assert usage.session_transcripts() == []


class TestCurrentUsage:
  def test_usage_file_pointer_wins(self, tmp_path, monkeypatch):
    pointer = tmp_path / 'usage.json'
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(pointer))
    usage.publish('bro//dev', {'gpt-5': C(output=3)})
    current = usage.current_usage()
    assert current == Usage(agent='bro//dev', per_model={'gpt-5': C(output=3)})

  def test_no_pointer_no_transcript_yields_none(self, monkeypatch):
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    monkeypatch.setattr(usage, 'session_transcripts', list)
    assert usage.current_usage() is None

  def test_transcript_fallback_carries_claude_agent(self, tmp_path, monkeypatch):
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    monkeypatch.setenv('AI_AGENT', 'claude-code_2-1-201_agent')
    jsonl = tmp_path / 't.jsonl'
    jsonl.write_text(json.dumps({'message': {'model': OPUS, 'usage': {'output_tokens': 7}}}) + '\n')
    monkeypatch.setattr(usage, 'session_transcripts', lambda: [jsonl])
    current = usage.current_usage()
    assert current == Usage(agent='Claude Code 2.1.201', per_model={OPUS: C(output=7)})

  def test_sums_the_segment_and_its_subagents(self, tmp_path, monkeypatch):
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    monkeypatch.setenv('AI_AGENT', 'claude-code_2-1-201_agent')
    transcripts = []
    for name, output in (('segment', 7), ('agent-a', 11), ('agent-b', 5)):
      jsonl = tmp_path / f'{name}.jsonl'
      row = {'message': {'model': OPUS, 'usage': {'output_tokens': output}}}
      jsonl.write_text(json.dumps(row) + '\n')
      transcripts.append(jsonl)
    monkeypatch.setattr(usage, 'session_transcripts', lambda: transcripts)
    current = usage.current_usage()
    assert current == Usage(agent='Claude Code 2.1.201', per_model={OPUS: C(output=23)})


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


class TestClaudeVersion:
  def test_from_ai_agent(self, monkeypatch):
    monkeypatch.setenv('AI_AGENT', 'claude-code_2-1-181_agent')
    assert usage.claude_version() == '2.1.181'

  def test_from_versioned_execpath(self, monkeypatch):
    monkeypatch.delenv('AI_AGENT', raising=False)
    monkeypatch.setenv('CLAUDE_CODE_EXECPATH', '/home/u/.local/versions/2.1.181/claude')
    assert usage.claude_version() == '2.1.181'

  def test_non_version_execpath_falls_back(self, monkeypatch):
    monkeypatch.delenv('AI_AGENT', raising=False)
    monkeypatch.setenv('CLAUDE_CODE_EXECPATH', '/usr/lib/node_modules/.../bin/claude.exe')
    assert usage.claude_version() == 'unknown'


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
