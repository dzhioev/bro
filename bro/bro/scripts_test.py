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

  def test_content_is_rendered_for_the_claude_harness(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    source = create_bro('ppp-dev').skills['pr']
    written = (tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md').read_text()
    assert written == llm.mcp.render_text(
      source.read_text(), harness='claude', wire='mcp', creds=credentials.known_names()
    )

  def test_creates_skills_dir_if_missing(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    assert (tmp_path / '.claude' / 'skills').is_dir()
