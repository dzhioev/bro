import os

import cw.bro


class TestPopulateBroSkills:
  def test_creates_symlinks_for_each_skill(self, tmp_path):
    # ppp-dev inherits /pr and /land from dev via the MRO walk
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    skills_dir = tmp_path / '.claude' / 'skills'
    for name in ('pr', 'land'):
      link = skills_dir / name / 'SKILL.md'
      assert link.is_symlink()
      assert link.resolve().name == f'{name}.md'

  def test_symlinks_are_relative(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    link = tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md'
    target = link.readlink()
    assert not target.is_absolute()

  def test_relative_symlinks_resolve_through_var_style_symlink(self, tmp_path):
    # macOS tempfile.mkdtemp() returns /var/folders/… where /var → /private/var,
    # so the symlink target sits one dir deeper than its logical path. mirror that
    # with a `var` → `private/var` indirection: a relpath against the logical path
    # would be off by one level and the skill symlink would dangle.
    real = tmp_path / 'private' / 'var'
    real.mkdir(parents=True)
    project = tmp_path / 'var'
    project.symlink_to(real)
    cw.bro._populate_bro_skills(project, 'ppp-dev')
    link = project / '.claude' / 'skills' / 'pr' / 'SKILL.md'
    assert link.is_symlink()
    assert not link.readlink().is_absolute()
    assert os.path.exists(link)
    assert len(link.read_text()) > 0

  def test_wipes_stale_symlinks_before_recreating(self, tmp_path):
    skills_dir = tmp_path / '.claude' / 'skills'
    stale = skills_dir / 'stale'
    stale.mkdir(parents=True)
    (stale / 'SKILL.md').symlink_to('/nonexistent')
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
    assert not skill_md.is_symlink()
    assert skill_md.read_text() == 'static content'

  def test_creates_skills_dir_if_missing(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    assert (tmp_path / '.claude' / 'skills').is_dir()

  def test_idempotent(self, tmp_path):
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    cw.bro._populate_bro_skills(tmp_path, 'ppp-dev')
    link = tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md'
    assert link.is_symlink()
