import json

import pytest

import cw


@pytest.fixture
def config_path(monkeypatch, tmp_path):
  path = tmp_path / 'anthropic.json'
  monkeypatch.setattr(cw, '_ANTHROPIC_CONFIG_PATH', path)
  return path


class TestLoadAnthropicKey:
  def test_reads_from_config(self, config_path):
    config_path.write_text(json.dumps({'api_key': 'sk-from-file'}))
    assert cw._load_anthropic_key() == 'sk-from-file'

  def test_none_when_missing(self, config_path):
    assert cw._load_anthropic_key() is None

  def test_none_when_empty_value(self, config_path):
    config_path.write_text(json.dumps({'api_key': ''}))
    assert cw._load_anthropic_key() is None

  def test_none_when_field_missing(self, config_path):
    config_path.write_text(json.dumps({'something_else': 'x'}))
    assert cw._load_anthropic_key() is None


class TestBroClaudeArgv:
  def test_basic_shape(self):
    argv = cw._bro_claude_argv('pm')
    assert '--bare' in argv
    assert '--strict-mcp-config' in argv
    # slash commands must stay enabled so the bro's skills (populated as
    # .claude/skills/<name>/SKILL.md symlinks by the entrypoint) are reachable
    assert '--disable-slash-commands' not in argv
    # tools disabled (empty string follows --tools)
    i = argv.index('--tools')
    assert argv[i + 1] == ''
    # allowlist scoped to bro:
    i = argv.index('--allowed-tools')
    assert argv[i + 1] == 'mcp__bro__*'

  def test_mcp_config_points_at_shim(self):
    argv = cw._bro_claude_argv('pm')
    i = argv.index('--mcp-config')
    cfg = json.loads(argv[i + 1])
    bro_server = cfg['mcpServers']['bro']
    assert bro_server['command'] == 'mcp-server'
    assert bro_server['args'] == ['bro:pm']

  def test_system_prompt_is_bros_own(self):
    from bro.registry import get_bro

    bro = get_bro('pm')
    argv = cw._bro_claude_argv('pm')
    i = argv.index('--system-prompt')
    assert argv[i + 1] == bro.system_prompt

  def test_unknown_bro_raises(self):
    with pytest.raises(KeyError, match='unknown bro'):
      cw._bro_claude_argv('does-not-exist')


class TestPopulateBroSkills:
  def test_creates_symlinks_for_each_skill(self, tmp_path):
    # ppp-dev inherits /pr and /land from dev via the MRO walk
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    skills_dir = tmp_path / '.claude' / 'skills'
    for name in ('pr', 'land'):
      link = skills_dir / name / 'SKILL.md'
      assert link.is_symlink()
      assert link.resolve().name == f'{name}.md'

  def test_symlinks_are_relative(self, tmp_path):
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    link = tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md'
    target = link.readlink()
    assert not target.is_absolute()

  def test_wipes_stale_symlinks_before_recreating(self, tmp_path):
    skills_dir = tmp_path / '.claude' / 'skills'
    stale = skills_dir / 'stale'
    stale.mkdir(parents=True)
    (stale / 'SKILL.md').symlink_to('/nonexistent')
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    assert not stale.exists()

  def test_leaves_static_skills_alone(self, tmp_path):
    skills_dir = tmp_path / '.claude' / 'skills'
    static = skills_dir / 'deploy'
    static.mkdir(parents=True)
    skill_md = static / 'SKILL.md'
    skill_md.write_text('static content')
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    assert skill_md.is_file()
    assert not skill_md.is_symlink()
    assert skill_md.read_text() == 'static content'

  def test_creates_skills_dir_if_missing(self, tmp_path):
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    assert (tmp_path / '.claude' / 'skills').is_dir()

  def test_idempotent(self, tmp_path):
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    link = tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md'
    assert link.is_symlink()
