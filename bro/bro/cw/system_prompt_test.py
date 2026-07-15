import cw.system_prompt


class TestSessionAppendPrompt:
  def test_includes_base_prompts(self):
    out = cw.system_prompt._session_append_prompt('guided', 'bro')
    assert 'Interaction policy' in out
    assert 'Land mode: PR' not in out

  def test_non_guided_modes_add_land_mode(self):
    for mode in ('unattended', 'detached', 'attended'):
      assert 'Land mode: PR' in cw.system_prompt._session_append_prompt(mode, 'bro')

  def test_guided_fragment(self):
    out = cw.system_prompt._session_append_prompt('guided', 'bro')
    assert '# Guided session' in out
    assert '# Attended session' not in out

  def test_each_mode_gets_its_own_fragment(self):
    for mode, heading in (
      ('unattended', '# Unattended session'),
      ('detached', '# Detached session'),
      ('attended', '# Attended session'),
    ):
      out = cw.system_prompt._session_append_prompt(mode, 'bro')
      assert heading in out
      assert '# Guided session' not in out

  def test_persona_prompts_injected(self):
    out = cw.system_prompt._session_append_prompt('guided', 'ppp-dev')
    assert '## PPP project' in out
    assert 'dev-style-source::read' in out

  def test_other_personas_carry_their_own_prompts_only(self):
    assert '## PPP project' not in cw.system_prompt._session_append_prompt('guided', 'bro')


class TestSurfaceRendering:
  def test_tool_names_rendered_for_mcp_wire(self):
    out = cw.system_prompt._session_append_prompt('guided', 'ppp-dev')
    assert 'mcp__namespace__tool' in out
    assert 'call that wire name directly' not in out

  def test_no_directives_survive_rendering(self):
    # the prose literal `{{…}}` describing the syntax stays; the persona points
    # every surface at the reference tools
    out = cw.system_prompt._session_append_prompt('guided', 'ppp-dev')
    assert '{{iff' not in out
    assert 'template-source::read' in out
