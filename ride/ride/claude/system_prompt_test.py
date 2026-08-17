import ride.claude.system_prompt as ride_system_prompt


class TestSessionAppendPrompt:
  def test_includes_base_prompts(self):
    out = ride_system_prompt._session_append_prompt('guided', 'bro')
    assert 'Interaction policy' in out
    assert 'full authorization' not in out

  def test_non_guided_holds_add_the_authorization_block(self):
    for hold in ('unattended', 'detached', 'attended'):
      assert 'full authorization' in ride_system_prompt._session_append_prompt(hold, 'bro')

  def test_guided_fragment(self):
    out = ride_system_prompt._session_append_prompt('guided', 'bro')
    assert '# Guided session' in out
    assert '# Attended session' not in out

  def test_each_hold_gets_its_own_fragment(self):
    for hold, heading in (
      ('unattended', '# Unattended session'),
      ('detached', '# Detached session'),
      ('attended', '# Attended session'),
    ):
      out = ride_system_prompt._session_append_prompt(hold, 'bro')
      assert heading in out
      assert '# Guided session' not in out

  def test_persona_prompts_injected(self):
    out = ride_system_prompt._session_append_prompt('guided', 'dev')
    assert 'You are a software developer' in out
    assert 'dev-style-source::read' in out

  def test_other_personas_carry_their_own_prompts_only(self):
    assert 'You are a software developer' not in ride_system_prompt._session_append_prompt(
      'guided', 'bro'
    )

  def test_persona_spells_include_cast_contract(self, monkeypatch):
    monkeypatch.setattr('bro.spells.credentials.available', lambda name: True)
    out = ride_system_prompt._session_append_prompt('guided', 'dev')
    assert '## Spells' in out
    assert '[[…]]' in out
    assert '`bro::cast`' in out
    assert '`/<name>`' not in out


class TestSurfaceRendering:
  def test_tool_names_rendered_for_mcp_wire(self):
    out = ride_system_prompt._session_append_prompt('guided', 'dev')
    assert 'mcp__namespace__tool' in out
    assert 'call that wire name directly' not in out

  def test_no_directives_survive_rendering(self):
    out = ride_system_prompt._session_append_prompt('guided', 'dev')
    assert '{{iff' not in out
