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
    assert '--disable-slash-commands' in argv
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
