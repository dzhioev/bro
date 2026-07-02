import json
import os

import pytest

import cw.bro


def _pm_namespaces() -> list[str]:
  from bro.registry import create_bro

  return list(dict.fromkeys(s.namespace for s in create_bro('pm').claude_bro_mcp_servers()))


class TestBroLaunch:
  def test_basic_shape(self):
    argv = cw.bro._bro_launch('pm').claude_argv
    assert '--bare' in argv
    assert '--strict-mcp-config' in argv
    # skills reach a --bro session through the `bro::skill` MCP tool (--bare
    # skips .claude/skills/ discovery); built-in slash commands stay enabled
    assert '--disable-slash-commands' not in argv
    # tools disabled (empty string follows --tools)
    i = argv.index('--tools')
    assert argv[i + 1] == ''

  def test_allowed_tools_cover_each_namespace(self):
    argv = cw.bro._bro_launch('pm').claude_argv
    i = argv.index('--allowed-tools')
    assert argv[i + 1] == ','.join(f'mcp__{ns}__*' for ns in _pm_namespaces())

  def test_mcp_config_one_http_entry_per_namespace(self):
    launch = cw.bro._bro_launch('pm')
    argv = launch.claude_argv
    i = argv.index('--mcp-config')
    cfg = json.loads(argv[i + 1])
    namespaces = _pm_namespaces()
    # the service server's `skill` tool rides the `bro` namespace
    assert 'bro' in namespaces
    assert list(cfg['mcpServers']) == namespaces
    assert launch.extra_env['CW_MCP_HTTP_SPEC'] == 'bro:pm'
    token = launch.extra_env['CW_MCP_HTTP_TOKEN']
    port = launch.extra_env['CW_MCP_HTTP_PORT']
    for ns, entry in cfg['mcpServers'].items():
      assert entry['type'] == 'http'
      assert entry['url'] == f'http://127.0.0.1:{port}/{ns}'
      assert entry['headers'] == {'Authorization': f'Bearer {token}'}

  def test_token_is_per_launch(self):
    first = cw.bro._bro_launch('pm').extra_env['CW_MCP_HTTP_TOKEN']
    second = cw.bro._bro_launch('pm').extra_env['CW_MCP_HTTP_TOKEN']
    assert first != second

  def test_system_prompt_is_bros_claude_flavor(self):
    from bro.registry import create_bro

    bro = create_bro('pm')
    argv = cw.bro._bro_launch('pm').claude_argv
    i = argv.index('--system-prompt')
    assert argv[i + 1] == bro.claude_system_prompt
    # the flavor whose tool-name rule matches the mcp__<ns>__<tool> mounts
    assert '`mcp__namespace__tool`' in argv[i + 1]

  def test_unknown_bro_raises(self):
    with pytest.raises(KeyError, match='unknown bro'):
      cw.bro._bro_launch('does-not-exist')


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
    proj = tmp_path / 'var'
    proj.symlink_to(real)
    cw.bro._populate_bro_skills(proj, 'ppp-dev')
    link = proj / '.claude' / 'skills' / 'pr' / 'SKILL.md'
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
