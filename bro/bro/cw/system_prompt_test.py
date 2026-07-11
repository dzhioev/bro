import cw.system_prompt


class TestSessionAppendPrompt:
  def test_includes_base_prompts(self):
    out = cw.system_prompt._session_append_prompt(False, None)
    assert 'Interaction policy' in out
    assert 'Land mode: PR' not in out

  def test_auto_adds_land_mode(self):
    assert 'Land mode: PR' in cw.system_prompt._session_append_prompt(True, None)

  def test_manual_fragment_without_auto(self):
    out = cw.system_prompt._session_append_prompt(False, None)
    assert '# Manual session' in out
    assert '# Autonomous session' not in out

  def test_autonomous_fragment_with_auto(self):
    out = cw.system_prompt._session_append_prompt(True, None)
    assert '# Autonomous session' in out
    assert '# Manual session' not in out

  def test_bro_persona_injected(self):
    out = cw.system_prompt._session_append_prompt(False, 'ppp-dev')
    assert '## PPP project' in out
    assert "wasn't in the room" in out

  def test_no_persona_without_bro(self):
    assert '## PPP project' not in cw.system_prompt._session_append_prompt(False, None)


class TestSurfaceRendering:
  def test_tool_names_rendered_for_mcp_wire(self):
    out = cw.system_prompt._session_append_prompt(False, None)
    assert 'mcp__namespace__tool' in out
    assert 'call that wire name directly' not in out

  def test_no_unrendered_directives(self):
    assert '{{' not in cw.system_prompt._session_append_prompt(False, None)
