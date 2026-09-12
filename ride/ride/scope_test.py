import dataclasses
import json
import subprocess
from typing import ClassVar
from unittest.mock import patch

import pytest

import ride.scope
from bro.base import credentials
from bro.base.condition import when
from bro.bro import feature
from bro.datasources.web_search import WebSearch
from bro.llm.mcp import InProcessMCPServer
from bro.mcp import MCPServerSpec, ToolLayer, creds
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


class _PayloadServer(InProcessMCPServer):
  needed_secrets = ('payload',)

  def __init__(self):
    super().__init__('payload-srv', [])


class GatedBro(Bro):
  name = 'scope-gated'
  description = 'feature-gated bro for launch scope tests'
  features: ClassVar = {'x': creds.contains('gate')}
  tools: ClassVar = [
    when(feature('x'), ToolLayer(server_specs=(MCPServerSpec.of(_PayloadServer),)))
  ]


@pytest.fixture(autouse=True)
def registered_scope_bros(register_test_bros):
  register_test_bros(SearchBro, GatedBro)


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

  def test_a_url_attachment_binds_its_own_entry(self, tmp_path, monkeypatch):
    url = 'https://github.com/foo/api.git'
    config = tmp_path / 'bro.json'
    config.write_text(json.dumps({'projects': {url: {'creds': ['brog+github']}}}))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    scoped = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE, attachment=url)
    assert scoped.selection == {'brog': 'github'}

  def test_a_checkout_binds_the_entry_keyed_by_its_origin_url(self, tmp_path, monkeypatch):
    url = 'https://github.com/foo/api.git'
    checkout = tmp_path / 'api'
    subprocess.run(['git', 'init', '-q', checkout], check=True)
    subprocess.run(['git', '-C', checkout, 'remote', 'add', 'origin', url], check=True)
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps(
        {
          'projects': {
            url: {'creds': ['brog+github'], 'bros': {'bro-dev': {'creds': ['github+reviewer']}}},
            str(checkout): {'creds': ['aws+laptop']},
          }
        }
      )
    )
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

    scoped = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE, attachment=str(checkout))

    assert scoped.selection == {'brog': 'github', 'github': 'reviewer', 'aws': 'laptop'}

  def test_a_detached_scope_ignores_another_projects_selection(self, tmp_path, monkeypatch):
    config = tmp_path / 'bro.json'
    config.write_text(json.dumps({'projects': {str(tmp_path): {'creds': ['brog+github']}}}))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

    scoped = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE)

    assert 'brog' in scoped.required
    assert scoped.selection == {}

  def test_defaults_select_for_a_detached_scope(self, tmp_path, monkeypatch):
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

  @pytest.mark.parametrize('recipe', [CLAUDE_RECIPE, RAW_RECIPE, BRO_RUN_RECIPE])
  def test_unknown_bro_fails_the_scope(self, recipe):
    with pytest.raises(ride.scope.LaunchScopeError, match="unknown bro 'nonexistent-bro'"):
      ride.scope.scoped_secrets('nonexistent-bro', recipe)


class TestBindLaunchLLM:
  def _config(self, tmp_path, monkeypatch, bros: dict) -> None:
    config = tmp_path / 'bro.json'
    config.write_text(json.dumps({'projects': {str(tmp_path): {'bros': bros}}}))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

  def test_the_launch_settles_its_flags_over_the_bros_entry(self, tmp_path, monkeypatch):
    self._config(tmp_path, monkeypatch, {'bro-dev': {'llm': 'openai:sol:xhigh'}})

    assert ride.scope.bind_launch_llm(str(tmp_path), 'bro-dev', '::low') == 'openai:sol:low'

  def test_a_launch_naming_nothing_runs_the_entry(self, tmp_path, monkeypatch):
    self._config(tmp_path, monkeypatch, {'bro-dev': {'llm': 'openai:sol:xhigh'}})

    assert ride.scope.bind_launch_llm(str(tmp_path), 'bro-dev', None) == 'openai:sol:xhigh'

  def test_nothing_named_anywhere_is_none(self, tmp_path, monkeypatch):
    self._config(tmp_path, monkeypatch, {'other': {'llm': 'openai:sol:xhigh'}})

    assert ride.scope.bind_launch_llm(str(tmp_path), 'bro-dev', None) is None
    assert ride.scope.bind_launch_llm(None, 'bro-dev', None) is None

  def test_a_detached_launch_keeps_its_own_value(self, tmp_path, monkeypatch):
    self._config(tmp_path, monkeypatch, {'bro-dev': {'llm': 'openai:sol:xhigh'}})

    assert ride.scope.bind_launch_llm(None, 'bro-dev', '::low') == '::low'


class TestLaunchLLMSpec:
  def test_a_selection_only_the_settled_recipe_reads_is_read(self, tmp_path, monkeypatch):
    from bro.llm.llms.echo import LLMSpec as EchoLLMSpec
    from bro.registry import get_class
    from ride.bro import BRO

    monkeypatch.setattr(get_class('bro-dev'), 'llm_spec', EchoLLMSpec())
    # spells put the cast key in the optional tier, which would read the
    # selection on its own
    for base in get_class('bro-dev').__mro__:
      if 'spells' in vars(base):
        monkeypatch.setattr(base, 'spells', ())
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps(
        {
          'projects': {
            str(tmp_path): {'bros': {'bro-dev': {'creds': ['openai+work'], 'llm': 'openai:sol'}}}
          }
        }
      )
    )
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    attachment = str(tmp_path)

    llm_spec = ride.scope.launch_llm_spec(BRO, attachment, 'bro-dev', None)
    scoped = ride.scope.scoped_secrets(
      'bro-dev', BRO_RUN_RECIPE, attachment=attachment, llm_spec=llm_spec
    )

    assert 'openai' in scoped.required
    assert scoped.selection['openai'] == 'work'
    with pytest.raises(ride.scope.LaunchScopeError, match='move the entry from "creds" to "grant"'):
      ride.scope.scoped_secrets('bro-dev', BRO_RUN_RECIPE, attachment=attachment)

  def test_an_unknown_bro_fails_the_launch(self):
    from ride.bro import BRO

    with pytest.raises(ride.scope.LaunchScopeError, match="unknown bro 'no-such-bro'"):
      ride.scope.launch_llm_spec(BRO, None, 'no-such-bro', None)


class TestHostConfigBroLayer:
  # scope-search (above) declares no github and reads openai best-effort
  def _host_config(self, tmp_path, monkeypatch, entry):
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps(
        {
          'projects': {
            str(tmp_path): {'creds': ['github+project'], 'bros': {'scope-search': entry}}
          }
        }
      )
    )
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

  def _scope(self, tmp_path, recipe=CLAUDE_RECIPE):
    return ride.scope.scoped_secrets('scope-search', recipe, attachment=str(tmp_path))

  def test_a_grant_adds_an_undeclared_kind_to_the_required_tier_under_its_instance(
    self, tmp_path, monkeypatch
  ):
    self._host_config(tmp_path, monkeypatch, {'grant': ['github+reviewer']})

    scoped = self._scope(tmp_path)

    assert 'github' in scoped.required
    assert scoped.selection['github'] == 'reviewer'

  def test_a_bare_grant_reads_the_projects_instance(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {'grant': ['github']})

    scoped = self._scope(tmp_path)

    assert 'github' in scoped.required
    assert scoped.selection['github'] == 'project'

  def test_a_grant_promotes_an_optional_kind(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {'grant': ['openai+work']})

    scoped = self._scope(tmp_path)

    assert 'openai' in scoped.required
    assert 'openai' not in scoped.optional
    assert scoped.selection['openai'] == 'work'

  def test_a_grant_of_a_declared_kind_selects_its_instance(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {'grant': ['catalog+shared']})

    scoped = self._scope(tmp_path)

    assert 'catalog' in scoped.required
    assert scoped.selection['catalog'] == 'shared'

  def test_a_flag_grant_of_a_granted_kind_stays_a_no_op(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {'grant': ['github+reviewer']})

    with pytest.raises(ValueError, match='already selected'):
      ride.scope.scoped_secrets(
        'scope-search',
        CLAUDE_RECIPE,
        attachment=str(tmp_path),
        grant=['github+reviewer'],
        revoke=[],
      )

  def test_a_selection_of_an_unread_kind_fails_naming_grant(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {'creds': ['github+reviewer']})

    with pytest.raises(
      ride.scope.LaunchScopeError,
      match=r'bros\.scope-search selects github\+reviewer \(project-path-bro\).*"grant"',
    ):
      self._scope(tmp_path)

  def test_an_unchecked_scope_carries_the_unread_selection(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {'creds': ['github+reviewer']})

    scoped = ride.scope.scoped_secrets(
      'scope-search',
      CLAUDE_RECIPE,
      attachment=str(tmp_path),
      grant=[],
      revoke=[],
      check_selection=False,
    )

    assert 'github' not in scoped.required | scoped.optional
    assert scoped.selection['github'] == 'reviewer'

  def test_a_selection_of_an_optional_kind_is_read(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {'creds': ['openai+work']})

    scoped = self._scope(tmp_path)

    assert 'openai' in scoped.optional
    assert scoped.selection['openai'] == 'work'

  def test_a_trails_selection_survives_a_dropped_baseline(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {'creds': ['trails+review']})

    scoped = self._scope(
      tmp_path, dataclasses.replace(CLAUDE_RECIPE, optional_baseline=frozenset())
    )

    assert 'trails' not in scoped.optional
    assert scoped.selection['trails'] == 'review'

  def test_a_project_selection_of_an_unread_kind_is_carried(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {})

    assert self._scope(tmp_path).selection == {'github': 'project'}

  def test_a_malformed_host_config_fails_the_scope(self, tmp_path, monkeypatch):
    config = tmp_path / 'bro.json'
    config.write_text('{"projects": []}')
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

    with pytest.raises(ride.scope.LaunchScopeError, match='projects must be a json object'):
      self._scope(tmp_path)


class TestScopeOverrides:
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
    scoped = ride.scope.scoped_secrets(
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
    child = ride.scope.scoped_secrets(
      'bro-eyebro', CLAUDE_RECIPE, attachment=str(tmp_path), grant=[], revoke=[]
    )
    assert parent.selection['github'] == 'dev'
    assert child.selection['github'] == 'reviewer'

  def test_a_root_whose_attachment_names_no_entry_gets_no_other_projects_instance(
    self, tmp_path, monkeypatch
  ):
    self._host_config(tmp_path, monkeypatch, {str(tmp_path / 'other'): {'creds': ['brog+github']}})
    for attachment in (None, str(tmp_path), 'https://github.com/foo/api.git'):
      scoped = ride.scope.scoped_secrets(
        'bro-dev', CLAUDE_RECIPE, attachment=attachment, grant=[], revoke=[]
      )
      assert 'brog' in scoped.required
      assert scoped.selection == {}

  def test_a_granted_instance_selects_it_for_the_child(self, tmp_path, monkeypatch):
    self._host_config(tmp_path, monkeypatch, {str(tmp_path / 'other'): {'creds': ['brog+github']}})
    scoped = ride.scope.scoped_secrets(
      'bro-dev', CLAUDE_RECIPE, attachment=None, grant=['brog+github'], revoke=[]
    )
    assert 'brog' in scoped.required
    assert scoped.selection['brog'] == 'github'

  def test_a_bare_grant_of_a_scoped_kind_is_a_no_op_failure(self):
    with pytest.raises(ValueError, match='already in the scoped credential set'):
      ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE, grant=['github'])

  def test_the_bro_halves_of_the_overrides_do_not_reach_the_scope(self):
    scoped = ride.scope.scoped_secrets('bro-dev', CLAUDE_RECIPE, grant=['@dev'], revoke=['@bro'])
    assert '@dev' not in scoped.required
    assert 'github' in scoped.required


class TestScopeEvaluatesUnderTheLaunchSelection:
  # the gate credential is stored under one instance only, so it resolves for a
  # launch exactly when a layer or a grant selects that instance
  def _host(self, tmp_path, monkeypatch, *, bros):
    store = tmp_path / 'store'
    material = store / credentials.MATERIAL_DIR / f'gate+reviewer{credentials.MATERIAL_SUFFIX}'
    material.parent.mkdir(parents=True)
    material.write_text('secret')
    monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
    monkeypatch.setattr(
      credentials,
      'default_registry',
      lambda: {
        name: credentials.CredentialKind(name, f'{name} credential') for name in ('gate', 'payload')
      },
    )
    config = tmp_path / 'bro.json'
    config.write_text(json.dumps({'projects': {str(tmp_path): {'bros': bros}}}))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

  def test_a_gate_the_bros_layer_resolves_selects_its_component(self, tmp_path, monkeypatch):
    self._host(tmp_path, monkeypatch, bros={'scope-gated': {'creds': ['gate+reviewer']}})
    scoped = ride.scope.scoped_secrets('scope-gated', CLAUDE_RECIPE, attachment=str(tmp_path))
    assert 'payload' in scoped.required
    assert 'gate' in scoped.optional

  def test_a_gate_no_layer_resolves_leaves_its_component_out(self, tmp_path, monkeypatch):
    self._host(tmp_path, monkeypatch, bros={})
    scoped = ride.scope.scoped_secrets('scope-gated', CLAUDE_RECIPE, attachment=str(tmp_path))
    assert 'payload' not in scoped.required
    assert 'gate' in scoped.optional

  def test_an_instance_grant_selects_the_gated_component(self, tmp_path, monkeypatch):
    self._host(tmp_path, monkeypatch, bros={})
    scoped = ride.scope.scoped_secrets(
      'scope-gated', CLAUDE_RECIPE, attachment=str(tmp_path), grant=['gate+reviewer']
    )
    assert {'gate', 'payload'} <= scoped.required
    assert scoped.selection['gate'] == 'reviewer'

  def test_a_revoked_gate_leaves_its_component_out(self, tmp_path, monkeypatch):
    self._host(tmp_path, monkeypatch, bros={'scope-gated': {'creds': ['gate+reviewer']}})
    scoped = ride.scope.scoped_secrets(
      'scope-gated', CLAUDE_RECIPE, attachment=str(tmp_path), revoke=['gate']
    )
    assert 'payload' not in scoped.required
    assert 'gate' not in scoped.required | scoped.optional

  def test_the_process_store_is_restored_after_the_computation(self, tmp_path, monkeypatch):
    self._host(tmp_path, monkeypatch, bros={'scope-gated': {'creds': ['gate+reviewer']}})
    ride.scope.scoped_secrets('scope-gated', CLAUDE_RECIPE, attachment=str(tmp_path))
    assert not credentials.available('gate')


class TestPreflightScopedLaunch:
  # summon_allow_list is patched to keep the bro-registry import out; the
  # override semantics of each step have their own tests
  def _preflight(self, scoped, **overrides):
    kwargs = {'grant': [], 'revoke': []}
    kwargs.update(overrides)
    return ride.scope.preflight_scoped_launch(scoped, 'bro-dev', **kwargs)

  def test_returns_the_allow_list_and_the_store(self):
    # one unified grant list: the @names feed the summon allow-list, the plain
    # names already shaped the scope and are not reapplied
    with (
      patch('ride.summon_control.summon_allow_list', return_value={'dev'}) as allow_list,
      patch(
        'ride.scope.credentials.build_scoped_store',
        return_value=({'creds/x.cred': b'v'}, frozenset({'x'})),
      ) as build,
    ):
      may_summon, store = self._preflight(
        ride.scope.ScopedSecrets({'github', 'gmail_creds'}, {'openai'}),
        grant=['gmail_creds', '@dev'],
        revoke=['@bro'],
      )
    assert may_summon == {'dev'}
    assert store == {'creds/x.cred': b'v'}
    assert store.kinds == frozenset({'x'})
    assert allow_list.call_args == (('bro-dev',), {'grant': ['dev'], 'revoke': ['bro']})
    assert build.call_args.args[1] == {'github', 'gmail_creds'}
    assert build.call_args.kwargs == {'optional': {'openai'}}

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
  def test_binds_the_view_to_the_scope(self):
    with patch('ride.scope.credentials.scoped_view_store', return_value='the-view') as view:
      store = ride.scope.launch_view_store(
        ride.scope.ScopedSecrets({'brog', 'github'}, {'openai'}, {'brog': 'github'})
      )
    assert store == 'the-view'
    assert view.call_args.args[0].selection['brog'] == 'github'
    assert view.call_args.args[1] == {'brog', 'github'}
    assert view.call_args.kwargs == {'optional': {'openai'}}
