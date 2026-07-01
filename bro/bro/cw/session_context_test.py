from pathlib import Path

from cw.session_context import build_session_context, encode_session_context


def _proj_with_claude_md(tmp_path: Path, body: str = '# CLAUDE.md\nrules') -> Path:
  (tmp_path / 'CLAUDE.md').write_text(body)
  return tmp_path


def _by_kind(records: list[dict]) -> dict:
  return {r['kind']: r for r in records}


class TestBuildSessionContext:
  def test_themed_session_records(self, tmp_path):
    records = build_session_context(
      system_prompt='injected text',
      bro_mode=False,
      branch='worktree-foo',
      base_sha='abc123',
      base_ref=None,
      mcp='http',
      bro=None,
      proj_root=_proj_with_claude_md(tmp_path),
    )
    by = _by_kind(records)
    assert by['system_prompt']['subtype'] == 'cw_injected'
    assert by['system_prompt']['content'] == 'injected text'
    assert by['git']['fields'] == {'branch': 'worktree-foo', 'base_sha': 'abc123'}
    assert by['mcp']['fields'] == {'mode': 'http', 'servers': ['flow']}
    assert by['claude_md']['subtype'] == 'root'
    assert 'rules' in by['claude_md']['content']

  def test_bro_session_system_prompt_subtype(self, tmp_path):
    records = build_session_context(
      system_prompt='full bro prompt',
      bro_mode=True,
      branch='worktree-foo',
      base_sha=None,
      base_ref=None,
      mcp=None,
      bro='ppp-dev',
      proj_root=_proj_with_claude_md(tmp_path),
    )
    by = _by_kind(records)
    assert by['system_prompt']['subtype'] == 'bro'
    assert by['mcp']['fields'] == {'mode': 'bro', 'servers': ['bro:ppp-dev']}

  def test_base_ref_included_when_set(self, tmp_path):
    records = build_session_context(
      system_prompt='x',
      bro_mode=False,
      branch='worktree-foo',
      base_sha='sha',
      base_ref='origin/master',
      mcp=None,
      bro=None,
      proj_root=_proj_with_claude_md(tmp_path),
    )
    assert _by_kind(records)['git']['fields']['base_ref'] == 'origin/master'

  def test_claude_md_omitted_when_absent(self, tmp_path):
    records = build_session_context(
      system_prompt='x',
      bro_mode=False,
      branch='worktree-foo',
      base_sha='sha',
      base_ref=None,
      mcp=None,
      bro=None,
      proj_root=tmp_path,
    )
    assert 'claude_md' not in _by_kind(records)

  def test_encode_roundtrips(self, tmp_path):
    import json

    records = build_session_context(
      system_prompt='x',
      bro_mode=False,
      branch='worktree-foo',
      base_sha='sha',
      base_ref=None,
      mcp='local',
      bro=None,
      proj_root=_proj_with_claude_md(tmp_path),
    )
    assert json.loads(encode_session_context(records)) == records
