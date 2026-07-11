import cw.bro
import llm.mcp
from base import credentials
from bro.registry import create_bro


class TestPopulateBroSkills:
  def test_renders_file_for_each_skill(self, tmp_path):
    # ppp-dev inherits /pr and /land from its own package via the MRO walk
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    skills_dir = tmp_path / '.claude' / 'skills'
    for name in ('pr', 'land'):
      skill_md = skills_dir / name / 'SKILL.md'
      assert skill_md.is_file()
      assert not skill_md.is_symlink()

  def test_content_is_source_rendered_for_native_claude_surface(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    src = create_bro('ppp-dev').skills['pr']
    written = (tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md').read_text()
    assert written == llm.mcp.render_text(
      src.read_text(), harness='claude', wire='mcp', creds=credentials.known_names()
    )

  def test_wipes_stale_rendered_skills_before_recreating(self, tmp_path):
    skills_dir = tmp_path / '.claude' / 'skills'
    stale = skills_dir / 'stale'
    stale.mkdir(parents=True)
    (stale / 'SKILL.md').write_text('rendered by a previous populate')
    (skills_dir / cw.bro._RENDERED_MANIFEST).write_text('stale\n')
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    assert not stale.exists()

  def test_leaves_static_skills_alone(self, tmp_path):
    skills_dir = tmp_path / '.claude' / 'skills'
    static = skills_dir / 'deploy'
    static.mkdir(parents=True)
    skill_md = static / 'SKILL.md'
    skill_md.write_text('static content')
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    assert skill_md.is_file()
    assert skill_md.read_text() == 'static content'

  def test_creates_skills_dir_if_missing(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    assert (tmp_path / '.claude' / 'skills').is_dir()

  def test_idempotent(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    first = (tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md').read_text()
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    assert (tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md').read_text() == first

  def test_manifest_names_written_skills(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    manifest = tmp_path / '.claude' / 'skills' / cw.bro._RENDERED_MANIFEST
    names = manifest.read_text().splitlines()
    assert 'pr' in names
    assert 'land' in names
