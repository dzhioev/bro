import json
from typing import ClassVar
from unittest.mock import patch

import pytest

import ride.scope
from bro.datasources.web_search import WebSearch
from bros.bro import Bro
from ride.scope import BRO_RUN_RECIPE, ScopeRecipe

CLAUDE_RECIPE = ScopeRecipe(
  name='test-claude',
  harness='claude',
  auth_secret='claude_code',
  llm_key=False,
)
RAW_RECIPE = ScopeRecipe(
  name='test-raw',
  harness='bro',
  auth_secret='anthropic',
  llm_key=False,
)


class SearchBro(Bro):
  name = 'scope-search'
  description = 'searchable bro for launch scope tests'
  data_sources: ClassVar = [WebSearch()]
  extra_secrets = ('catalog',)


@pytest.fixture(autouse=True)
def registered_scope_bros(register_test_bros):
  register_test_bros(SearchBro)


class TestScopedSecrets:
  def test_ride_session_set(self):
    # ride-session themed as bro-dev: the claude-harness manifest — extra_secrets
    # (github) and the session-local brog server's self-contained backend config
    # — + the claude_code OAuth token (the session's only auth).
    scoped = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE)
    assert {'github', 'brog', 'claude_code'} <= scoped.required
    # required (strict), not optional: no .credentials.json fallback in the
    # container, so a missing token must fail loudly on the host
    assert 'claude_code' not in scoped.optional
    # spell casting advertises openai best-effort, and the session-wide baseline
    # adds the recording credential in the same tier
    assert scoped.optional == {'openai', 'trails'}

  def test_ride_session_set_covers_the_bros_manifest(self):
    scoped = ride.scope.scoped_secrets('scope-search', CLAUDE_RECIPE)
    assert {'catalog', 'brave'} <= scoped.required

  def test_raw_session_uses_full_manifest_and_anthropic(self):
    # --raw serves the bro's own MCP servers, so it gets the full manifest (brog)
    # plus anthropic for the apiKeyHelper.
    scoped = ride.scope.scoped_secrets('bro-dev', RAW_RECIPE)
    assert {'brog', 'github', 'anthropic'} <= scoped.required
    # --raw runs claude --bare, which ignores CLAUDE_CODE_OAUTH_TOKEN, so the token
    # secret is not requested on this surface
    assert 'claude_code' not in scoped.optional
    assert 'claude_code' not in scoped.required

  def test_raw_session_includes_optional_secrets(self):
    # searchable data sources advertise openai best-effort
    # for the query-focused fetch summary; --raw hydrates it as the optional tier.
    scoped = ride.scope.scoped_secrets('scope-search', RAW_RECIPE)
    assert 'openai' in scoped.optional
    assert 'openai' not in scoped.required  # optional, not required

  def test_bro_run_manifest_plus_llm_key(self):
    # dev runs as an LLM process: its manifest plus its LLM key (openai →
    # openai, which needed_secrets() omits)
    scoped = ride.scope.scoped_secrets('dev', BRO_RUN_RECIPE)
    assert 'openai' in scoped.required

  def test_bro_run_optional_tier_carries_the_bros_optional_secrets(self):
    # searchable data sources advertise openai best-effort for the query-focused
    # fetch summary
    scoped = ride.scope.scoped_secrets('scope-search', BRO_RUN_RECIPE)
    assert 'openai' in scoped.optional

  def test_computing_a_scope_binds_the_project_and_bro_instances(self, tmp_path, monkeypatch):
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps(
        {
          'projects': {
            str(tmp_path): {
              'creds': ['brog+github'],
              'bros': {'bro-dev': {'creds': ['github+reviewer']}},
            }
          }
        }
      )
    )
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    scoped = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE, attachment=str(tmp_path))
    assert scoped.selection == {'brog': 'github', 'github': 'reviewer'}
    assert 'brog' in scoped.required
    assert 'brog+github' not in scoped.required
    assert scoped.unbound_kinds == frozenset()

  def test_a_url_attachment_binds_its_own_entry(self, tmp_path, monkeypatch):
    url = 'https://github.com/foo/api.git'
    config = tmp_path / 'bro.json'
    config.write_text(json.dumps({'projects': {url: {'creds': ['brog+github']}}}))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    scoped = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE, attachment=url)
    assert scoped.selection == {'brog': 'github'}
    assert scoped.unbound_kinds == frozenset()

  def test_an_attachment_no_entry_names_withholds_the_hosts_project_kinds(
    self, tmp_path, monkeypatch
  ):
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps({'projects': {str(tmp_path / 'other'): {'creds': ['brog+github']}}})
    )
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    scoped = ride.scope.scoped_secrets(
      'bro-dev', CLAUDE_RECIPE, attachment='https://github.com/foo/api.git'
    )
    assert scoped.unbound_kinds == frozenset({'brog'})

  def test_a_detached_scope_withholds_the_hosts_project_kinds(self, tmp_path, monkeypatch):
    config = tmp_path / 'bro.json'
    config.write_text(json.dumps({'projects': {str(tmp_path): {'creds': ['brog+github']}}}))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    assert ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE).unbound_kinds == frozenset({'brog'})

  def test_defaults_bind_and_unpoison_a_detached_scope(self, tmp_path, monkeypatch):
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps(
        {
          'defaults': {'creds': ['brog+default']},
          'projects': {str(tmp_path): {'creds': ['brog+project']}},
        }
      )
    )
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

    scoped = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE)

    assert scoped.selection == {'brog': 'default'}
    assert scoped.unbound_kinds == frozenset()

  def test_a_single_project_host_withholds_nothing(self):
    assert ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE).unbound_kinds == frozenset()

  @pytest.mark.parametrize('recipe', [CLAUDE_RECIPE, RAW_RECIPE, BRO_RUN_RECIPE])
  def test_unknown_bro_fails_the_scope(self, recipe):
    with pytest.raises(ride.scope.LaunchScopeError, match="unknown bro 'nonexistent-bro'"):
      ride.scope.scoped_secrets('nonexistent-bro', recipe)


class TestSummonedCredentialScope:
  def _host_config(self, tmp_path, monkeypatch, projects):
    config = tmp_path / 'bro.json'
    config.write_text(json.dumps({'projects': projects}))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

  def test_the_roots_attachment_selects_the_childs_project_bro_instances(
    self, tmp_path, monkeypatch
  ):
    self._host_config(
      tmp_path,
      monkeypatch,
      {
        str(tmp_path): {
          'creds': ['brog+github', 'github+dev'],
          'bros': {'bro-dev': {'creds': ['github+reviewer']}},
        }
      },
    )
    scoped = ride.scope.summoned_credential_scope(
      'bro-dev', CLAUDE_RECIPE, attachment=str(tmp_path), grant=[], revoke=[]
    )
    assert scoped.selection == {'brog': 'github', 'github': 'reviewer'}
    assert 'brog' in scoped.required

  def test_a_child_target_uses_a_different_instance_than_its_parent(self, tmp_path, monkeypatch):
    self._host_config(
      tmp_path,
      monkeypatch,
      {
        str(tmp_path): {
          'creds': ['github+dev'],
          'bros': {'bro-eyebro': {'creds': ['github+reviewer']}},
        }
      },
    )
    parent = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE, attachment=str(tmp_path))
    child = ride.scope.summoned_credential_scope(
      'bro-eyebro', CLAUDE_RECIPE, attachment=str(tmp_path), grant=[], revoke=[]
    )
    assert parent.selection['github'] == 'dev'
    assert child.selection['github'] == 'reviewer'

  def test_a_root_whose_attachment_names_no_entry_gets_no_other_projects_instance(
    self, tmp_path, monkeypatch
  ):
    self._host_config(tmp_path, monkeypatch, {str(tmp_path / 'other'): {'creds': ['brog+github']}})
    for attachment in (None, str(tmp_path), 'https://github.com/foo/api.git'):
      with pytest.raises(ValueError, match='reads brog per project'):
        ride.scope.summoned_credential_scope(
          'bro-dev', CLAUDE_RECIPE, attachment=attachment, grant=[], revoke=[]
        )

  def test_a_granted_instance_names_the_project_outright(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {str(tmp_path / 'other'): {'creds': ['brog+github']}})
    scoped = ride.scope.summoned_credential_scope(
      'bro-dev', CLAUDE_RECIPE, attachment=None, grant=['brog+github'], revoke=[]
    )
    assert 'brog' in scoped.required
    assert scoped.selection['brog'] == 'github'


class TestPreflightScopedLaunch:
  # summon_allow_list is patched to keep the bro-registry import out; the
  # override semantics of each step have their own tests
  def _preflight(self, scoped, **overrides):
    kwargs = {'grant': [], 'revoke': []}
    kwargs.update(overrides)
    return ride.scope.preflight_scoped_launch(scoped, 'bro-dev', **kwargs)

  def test_returns_finalized_scope_allow_list_and_store(self):
    # one unified grant list: plain names finalize the credential scope, @names
    # feed the summon allow-list
    with (
      patch('ride.summon_control.summon_allow_list', return_value={'dev'}) as allow_list,
      patch(
        'ride.scope.credentials.build_scoped_store',
        return_value=({'creds/x.cred': b'v'}, frozenset({'x'})),
      ) as build,
    ):
      scoped, may_summon, store = self._preflight(
        ride.scope.ScopedSecrets({'github'}, {'openai'}),
        grant=['gmail_creds', '@dev'],
        revoke=['@bro'],
      )
    assert scoped == ride.scope.ScopedSecrets({'github', 'gmail_creds'}, {'openai'})
    assert may_summon == {'dev'}
    assert store == {'creds/x.cred': b'v'}
    assert store.kinds == frozenset({'x'})
    assert allow_list.call_args == (('bro-dev',), {'grant': ['dev'], 'revoke': ['bro']})
    # the store is hydrated from the finalized tiers, not the incoming ones
    assert build.call_args.args[1] == {'github', 'gmail_creds'}
    assert build.call_args.kwargs == {'optional': {'openai'}}

  def test_grant_replaces_a_selected_same_kind_credential(self):
    with (
      patch('ride.summon_control.summon_allow_list', return_value=set()),
      patch('ride.scope.credentials.build_scoped_store', return_value=({}, frozenset())),
    ):
      scoped, _, _ = self._preflight(
        ride.scope.ScopedSecrets({'brog', 'github'}, set(), {'brog': 'linear'}),
        grant=['brog+github'],
      )
    assert scoped.required == {'brog', 'github'}
    assert scoped.selection['brog'] == 'github'

  def test_bad_credential_override_raises_launch_scope_error(self):
    with pytest.raises(ride.scope.LaunchScopeError, match='already in the scoped credential set'):
      self._preflight(ride.scope.ScopedSecrets({'github'}, set()), grant=['github'])

  def test_bare_bro_mark_raises_launch_scope_error(self):
    with pytest.raises(ride.scope.LaunchScopeError, match="malformed grant/revoke '@'"):
      self._preflight(ride.scope.ScopedSecrets(set(), set()), grant=['@'])

  def test_bad_summon_target_raises_launch_scope_error(self):
    with (
      patch(
        'ride.summon_control.summon_allow_list',
        side_effect=ValueError('unknown summon target(s)'),
      ),
      patch('ride.scope.credentials.build_scoped_store', return_value=({}, frozenset())),
    ):
      with pytest.raises(ride.scope.LaunchScopeError, match='unknown summon target'):
        self._preflight(ride.scope.ScopedSecrets(set(), set()), grant=['@devoop'])

  def test_an_unbound_project_kind_raises_launch_scope_error(self):
    with pytest.raises(ride.scope.LaunchScopeError, match='reads brog per project'):
      self._preflight(
        ride.scope.ScopedSecrets({'brog', 'github'}, set(), unbound_kinds=frozenset({'brog'}))
      )

  def test_naming_the_instance_satisfies_an_unbound_project_kind(self):
    with (
      patch('ride.summon_control.summon_allow_list', return_value=set()),
      patch('ride.scope.credentials.build_scoped_store', return_value=({}, frozenset())),
    ):
      scoped, _, _ = self._preflight(
        ride.scope.ScopedSecrets({'brog'}, set(), unbound_kinds=frozenset({'brog'})),
        grant=['brog+github'],
      )
    assert scoped.required == {'brog'}
    assert scoped.selection['brog'] == 'github'

  def test_unresolvable_secret_raises_launch_scope_error(self):
    from bro.base import credentials

    with (
      patch('ride.summon_control.summon_allow_list', return_value=set()),
      patch(
        'ride.scope.credentials.build_scoped_store',
        side_effect=credentials.SecretNotFound('github'),
      ),
    ):
      with pytest.raises(ride.scope.LaunchScopeError, match="secret 'github' not found"):
        self._preflight(ride.scope.ScopedSecrets({'github'}, set()))


class TestLaunchViewStore:
  def test_finalized_overrides_bind_the_view(self):
    # the same unified values the preflight takes: plain names finalize the
    # credential tiers, @names are the summon side and don't reach the view
    with patch('ride.scope.credentials.scoped_view_store', return_value='the-view') as view:
      store = ride.scope.launch_view_store(
        ride.scope.ScopedSecrets({'brog', 'github'}, {'openai'}),
        grant=['brog+github', '@dev'],
        revoke=[],
      )
    assert store == 'the-view'
    assert view.call_args.args[0].selection['brog'] == 'github'
    assert view.call_args.args[1] == {'brog', 'github'}
    assert view.call_args.kwargs == {'optional': {'openai'}}

  def test_bad_override_raises_launch_scope_error(self):
    with pytest.raises(ride.scope.LaunchScopeError, match='already in the scoped credential set'):
      ride.scope.launch_view_store(
        ride.scope.ScopedSecrets({'github'}, set()), grant=['github'], revoke=[]
      )
