import json
from pathlib import Path

from ride.claude.session_context import build_session_context, encode_session_context


def _proj_with_instructions(tmp_path: Path, body: str = '# AGENTS.md\nrules') -> Path:
  (tmp_path / 'AGENTS.md').write_text(body)
  return tmp_path


def _by_kind(records: list[dict]) -> dict:
  return {r['kind']: r for r in records}


class TestBuildSessionContext:
  def test_ride_session_records(self, tmp_path):
    records = build_session_context(
      system_prompt='injected text',
      bro='bro-dev',
      raw=False,
      proj_root=_proj_with_instructions(tmp_path),
    )
    by = _by_kind(records)
    assert by['system_prompt']['subtype'] == 'ride_injected'
    assert by['system_prompt']['content'] == 'injected text'
    assert by['mcp']['fields'] == {'mode': 'persona', 'servers': ['persona:bro-dev']}
    assert by['instructions']['subtype'] == 'root'
    assert 'rules' in by['instructions']['content']

  def test_raw_session_system_prompt_subtype(self, tmp_path):
    records = build_session_context(
      system_prompt='full bro prompt',
      bro='bro-dev',
      raw=True,
      proj_root=_proj_with_instructions(tmp_path),
    )
    by = _by_kind(records)
    assert by['system_prompt']['subtype'] == 'bro'
    assert by['mcp']['fields'] == {'mode': 'bro', 'servers': ['bro:bro-dev']}

  def test_instructions_omitted_when_absent(self, tmp_path):
    records = build_session_context(
      system_prompt='x',
      bro='bro-dev',
      raw=False,
      proj_root=tmp_path,
    )
    assert 'instructions' not in _by_kind(records)

  def test_claude_md_read_when_it_is_the_only_instructions_file(self, tmp_path):
    (tmp_path / 'CLAUDE.md').write_text('# CLAUDE.md\nrules')
    records = build_session_context(
      system_prompt='x',
      bro='bro-dev',
      raw=False,
      proj_root=tmp_path,
    )
    assert _by_kind(records)['instructions']['title'] == 'CLAUDE.md (root)'

  def test_agents_md_wins_over_claude_md(self, tmp_path):
    (tmp_path / 'CLAUDE.md').write_text('@AGENTS.md')
    records = build_session_context(
      system_prompt='x',
      bro='bro-dev',
      raw=False,
      proj_root=_proj_with_instructions(tmp_path),
    )
    instructions = _by_kind(records)['instructions']
    assert instructions['title'] == 'AGENTS.md (root)'
    assert 'rules' in instructions['content']

  def test_encode_roundtrips(self, tmp_path):
    records = build_session_context(
      system_prompt='x',
      bro='bro-dev',
      raw=False,
      proj_root=_proj_with_instructions(tmp_path),
    )
    assert json.loads(encode_session_context(records)) == records
