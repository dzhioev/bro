import cw.system_prompt


class TestSessionAppendPrompt:
  def test_includes_base_prompts(self):
    out = cw.system_prompt._session_append_prompt(False, 'bro')
    assert 'Interaction policy' in out
    assert 'Land mode: PR' not in out

  def test_auto_adds_land_mode(self):
    assert 'Land mode: PR' in cw.system_prompt._session_append_prompt(True, 'bro')

  def test_manual_fragment_without_auto(self):
    out = cw.system_prompt._session_append_prompt(False, 'bro')
    assert '# Manual session' in out
    assert '# Autonomous session' not in out

  def test_autonomous_fragment_with_auto(self):
    out = cw.system_prompt._session_append_prompt(True, 'bro')
    assert '# Autonomous session' in out
    assert '# Manual session' not in out

  def test_persona_prompts_injected(self):
    out = cw.system_prompt._session_append_prompt(False, 'ppp-dev')
    assert '## PPP project' in out
    assert 'dev-style-source::read' in out

  def test_other_personas_carry_their_own_prompts_only(self):
    assert '## PPP project' not in cw.system_prompt._session_append_prompt(False, 'bro')


class TestSurfaceRendering:
  def test_tool_names_rendered_for_mcp_wire(self):
    out = cw.system_prompt._session_append_prompt(False, 'ppp-dev')
    assert 'mcp__namespace__tool' in out
    assert 'call that wire name directly' not in out

  def test_no_directives_survive_rendering(self):
    # the prose literal `{{…}}` describing the syntax stays; the persona points
    # every surface at the reference tools
    out = cw.system_prompt._session_append_prompt(False, 'ppp-dev')
    assert '{{iff' not in out
    assert 'template-source::read' in out
