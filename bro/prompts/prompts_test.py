from pathlib import Path

import pytest

from bro.prompts import PromptLoader, get_prompt, get_prompt_path, hold_fragment


class TestContainment:
  def test_traversal_raises(self):
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      get_prompt('../CLAUDE.md')

  def test_absolute_path_raises(self):
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      get_prompt('/etc/passwd')

  def test_path_lookup_is_guarded_too(self):
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      get_prompt_path('../CLAUDE.md')

  def test_contained_dotdot_is_allowed(self):
    # containment is the invariant, not name syntax: a `..` that stays inside resolves
    assert get_prompt_path('shared/../tool_names.md') == get_prompt_path('tool_names.md')


class TestLoading:
  def test_plain_prompt_reads(self):
    assert len(get_prompt('tool_names.md')) > 0

  def test_template_requires_kwargs(self):
    with pytest.raises(ValueError, match='requires format arguments'):
      get_prompt('source_summary.prompt.template')

  def test_non_template_rejects_kwargs(self):
    with pytest.raises(ValueError, match='not a template'):
      get_prompt('tool_names.md', unexpected='x')

  def test_loader_binds_to_another_package_directory(self, tmp_path: Path):
    (tmp_path / 'one.prompt').write_text('one')
    loader = PromptLoader(tmp_path)
    assert loader.get_prompt('one.prompt') == 'one'
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      loader.get_prompt('../outside.prompt')


class TestHoldFragment:
  def test_each_level_selects_its_own_file(self):
    for hold, heading in (
      ('unattended', '# Unattended session'),
      ('detached', '# Detached session'),
      ('attended', '# Attended session'),
      ('guided', '# Guided session'),
    ):
      fragment = hold_fragment(hold, harness='claude', wire='mcp')
      assert fragment.startswith(heading)
      assert '{{' not in fragment

  def test_non_guided_levels_share_the_authorization_block(self):
    for hold in ('unattended', 'detached', 'attended'):
      fragment = hold_fragment(hold, harness='claude', wire='mcp')
      assert 'full authorization' in fragment

  def test_guided_carries_no_authorization_block(self):
    fragment = hold_fragment('guided', harness='claude', wire='mcp')
    assert 'full authorization' not in fragment

  def test_interactive_levels_share_the_interaction_policy(self):
    for hold in ('detached', 'attended', 'guided'):
      fragment = hold_fragment(hold, harness='claude', wire='mcp')
      assert '# Interaction policy' in fragment

  def test_unattended_carries_no_interaction_policy(self):
    fragment = hold_fragment('unattended', harness='claude', wire='mcp')
    assert '# Interaction policy' not in fragment

  def test_unknown_hold_raises(self):
    with pytest.raises(ValueError, match='unknown hold'):
      hold_fragment('automatic', harness='claude', wire='mcp')
