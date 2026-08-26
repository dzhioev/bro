import pytest

import bro.harness.claude as claude
from bro.mcp import ToolLayer, select

_GROUPS = {'FILES': claude.FILES, 'SHELL': claude.SHELL, 'DELEGATION': claude.DELEGATION}


class TestGroups:
  @pytest.mark.parametrize('name', sorted(_GROUPS))
  def test_group_is_a_tuple_of_names(self, name):
    group = _GROUPS[name]
    assert isinstance(group, tuple)
    assert len(group) > 0
    assert all(isinstance(tool, str) and len(tool) > 0 for tool in group)

  def test_groups_do_not_overlap(self):
    # personas splat several groups into one `block(...)`, and a layer with a
    # duplicated name raises — so an overlap breaks every such declaration
    names = [name for group in _GROUPS.values() for name in group]
    assert len(names) == len(set(names))


class TestBlock:
  def test_selected_on_claude_only(self):
    entry = claude.block('Read', 'Bash')
    assert select([entry], harness='claude') == [
      ToolLayer(blocked_native_tool_names=('Read', 'Bash'))
    ]
    assert select([entry], harness='bro') == []

  def test_rejects_a_duplicate_name(self):
    with pytest.raises(ValueError, match='duplicate'):
      claude.block('Read', 'Read')


class TestWatch:
  def test_selected_on_claude_only(self):
    entry = claude.watch('summon watch')
    assert select([entry], harness='claude') == [
      ToolLayer(
        native_tool_commands=(('Monitor', 'summon watch'),),
        served_native_tool_names=claude._TASK_CONTROL,
      )
    ]
    assert select([entry], harness='bro') == []

  def test_hands_back_tools_the_shell_group_withholds(self):
    [layer] = select([claude.watch('summon watch')], harness='claude')
    handed_back = {name for name, _ in layer.native_tool_commands}
    handed_back |= set(layer.served_native_tool_names)
    assert handed_back <= set(claude.SHELL)
