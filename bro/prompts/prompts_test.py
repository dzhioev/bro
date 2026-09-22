from pathlib import Path

import pytest

from bro.broker.environment import BROKER_TALK
from bro.prompts import PromptLoader, get_prompt, get_prompt_path, hold_fragment, session_fragment
from bro.summon import MAY_SUMMON_ENV, PARTY_MEMBER_ENV, SUMMONED_ENV, encode_may_summon


class TestContainment:
  def test_traversal_raises(self):
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      get_prompt('../AGENTS.md')

  def test_absolute_path_raises(self):
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      get_prompt('/etc/passwd')

  def test_path_lookup_is_guarded_too(self):
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      get_prompt_path('../AGENTS.md')

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
      fragment = hold_fragment(hold, harness='claude')
      assert fragment.startswith(heading)
      assert '{{' not in fragment

  def test_non_guided_levels_share_the_authorization_block(self):
    for hold in ('unattended', 'detached', 'attended'):
      fragment = hold_fragment(hold, harness='claude')
      assert 'full authorization' in fragment

  def test_guided_carries_no_authorization_block(self):
    fragment = hold_fragment('guided', harness='claude')
    assert 'full authorization' not in fragment

  def test_interactive_levels_share_the_interaction_policy(self):
    for hold in ('detached', 'attended', 'guided'):
      fragment = hold_fragment(hold, harness='claude')
      assert '# Interaction policy' in fragment

  def test_unattended_carries_no_interaction_policy(self):
    fragment = hold_fragment('unattended', harness='claude')
    assert '# Interaction policy' not in fragment

  def test_unknown_hold_raises(self):
    with pytest.raises(ValueError, match='unknown hold'):
      hold_fragment('automatic', harness='claude')


class TestSessionFragment:
  def test_an_unsummoned_run_gets_the_hold_fragment_alone(self, monkeypatch):
    monkeypatch.delenv(SUMMONED_ENV, raising=False)
    monkeypatch.delenv(MAY_SUMMON_ENV, raising=False)
    monkeypatch.delenv(PARTY_MEMBER_ENV, raising=False)
    assert session_fragment('attended', harness='claude') == hold_fragment(
      'attended', harness='claude'
    )

  def test_a_party_member_is_told_the_tree_is_shared(self, monkeypatch):
    monkeypatch.setenv(PARTY_MEMBER_ENV, 'broker-CH')
    fragment = session_fragment('unattended', harness='bro')
    assert fragment.startswith('# Party member')
    assert 'shares the summoner’s working tree' in fragment

  def test_a_summoning_run_keeps_the_summon_watch_armed_on_the_claude_harness(self, monkeypatch):
    monkeypatch.delenv(SUMMONED_ENV, raising=False)
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))
    fragment = session_fragment('attended', harness='claude')
    assert fragment.startswith('# Summoning session')
    assert '{{' not in fragment
    assert fragment.endswith(hold_fragment('attended', harness='claude'))

  def test_a_summoning_native_run_starts_a_watch_job_and_chills(self, monkeypatch):
    monkeypatch.delenv(SUMMONED_ENV, raising=False)
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))
    fragment = session_fragment('attended', harness='bro')
    assert "`bro::job('quest watch', mode='watch')`" in fragment
    assert '`bro::chill`' in fragment

  @pytest.mark.parametrize(
    ('harness', 'marker'),
    (
      ('claude', 'stop the watch with `TaskStop` and end the turn'),
      ('bro', 'ends when a turn ends with nothing running and nothing in flight'),
    ),
  )
  def test_a_summoning_run_is_told_how_its_one_shot_ends_per_surface(
    self, monkeypatch, harness, marker
  ):
    monkeypatch.delenv(SUMMONED_ENV, raising=False)
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))
    fragment = session_fragment('unattended', harness=harness)
    assert marker in fragment
    assert 'watches included' not in fragment

  @pytest.mark.parametrize(
    ('harness', 'marker'),
    (
      ('claude', 'so end it through `bro::answer`'),
      ('bro', 'ends when a turn ends with nothing running and nothing in flight'),
    ),
  )
  def test_a_summoned_run_is_told_how_its_one_shot_ends_per_surface(
    self, monkeypatch, harness, marker
  ):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    fragment = session_fragment('unattended', harness=harness, talk=('worker.say',))
    assert marker in fragment

  def test_a_summoning_summoned_run_carries_both_contracts_in_order(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    monkeypatch.setenv(BROKER_TALK, 'worker.say')
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))
    fragment = session_fragment('attended', harness='claude', talk=('worker.say',))
    assert fragment.index('# Summoning session') < fragment.index('# Summoned session')

  def test_a_summoned_run_carries_the_delivery_contract_at_every_hold(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    for hold in ('unattended', 'detached', 'attended', 'guided'):
      fragment = session_fragment(hold, harness='claude', talk=('worker.say',))
      assert fragment.startswith('# Summoned session')
      assert '{{' not in fragment

  def test_the_hold_fragment_stays_the_suffix(self, monkeypatch):
    # the resumed-hold swap in `native/bro/fork.py` replaces it there
    monkeypatch.setenv(SUMMONED_ENV, '1')
    fragment = session_fragment('guided', harness='claude', talk=('worker.say',))
    assert fragment.endswith(hold_fragment('guided', harness='claude'))

  @pytest.mark.parametrize(
    ('harness', 'marker'),
    (
      ('claude', 'Arm `Monitor` once on exactly `quest watch`'),
      ('bro', "`bro::job('quest watch', mode='watch')`"),
    ),
  )
  def test_a_speaking_summoner_reaches_the_child_by_surface(self, monkeypatch, harness, marker):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    fragment = session_fragment('attended', harness=harness, talk=('owner.say', 'worker.say'))
    assert marker in fragment

  def test_a_questioning_native_child_asks_without_blocking(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    fragment = session_fragment('unattended', harness='bro', talk=('worker.say', 'worker.question'))
    assert 'call `bro::quest_ask` on `self`' in fragment
    assert 'reply arrives on the quest watch' in fragment
    assert 'bounded `wait`' not in fragment

  def test_a_questioning_claude_child_arms_the_watch_for_the_reply(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    fragment = session_fragment(
      'unattended', harness='claude', talk=('worker.say', 'worker.question')
    )
    assert "`quest ask self '<question>' --wait`" in fragment
    assert 'arm `Monitor` once on exactly `quest watch`' in fragment

  def test_a_silent_child_is_told_to_raise_instead_of_asking(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    fragment = session_fragment('unattended', harness='bro', talk=())
    assert 'quest does not permit' in fragment

  def test_a_summoned_contract_requires_the_talk_fact(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    with pytest.raises(ValueError, match='unknown variable #talk'):
      session_fragment('attended', harness='claude')
