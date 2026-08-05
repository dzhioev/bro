from typing import ClassVar
from unittest.mock import patch

import pytest

import bro.launch.scope
from bro.bros.bro import Bro
from bro.datasources.web_search import WebSearch
from bro.launch.scope import Surface
from bro.workspace.store import finalize_scoped_secrets


class SearchBro(Bro):
  name = 'scope-search'
  description = 'searchable bro for launch scope tests'
  data_sources: ClassVar = [WebSearch()]
  extra_secrets = ('catalog',)


class DockerBro(Bro):
  name = 'scope-docker'
  description = 'docker bro for launch scope tests'
  needs_docker = True


@pytest.fixture(autouse=True)
def registered_scope_bros(register_test_bros):
  register_test_bros(SearchBro, DockerBro)


class TestScopedSecrets:
  def test_cw_session_set(self):
    # cw-session themed as bro-dev: baseline + the claude-harness
    # manifest — extra_secrets (github) and the session-local brog server's
    # self-contained backend config — + the claude_code OAuth token (the
    # session's only auth).
    scoped = bro.launch.scope.scoped_secrets('bro-dev', Surface.CW_SESSION, credential_instances={})
    assert {'trails', 'github', 'brog', 'claude_code'} <= scoped.required
    # required (strict), not optional: no .credentials.json fallback in the
    # container, so a missing token must fail loudly on the host
    assert 'claude_code' not in scoped.optional
    # the scripts dispatcher advertises openai best-effort; no session-wide
    # baseline adds anything on top
    assert scoped.optional == {'openai'}
    # a normal claude code session keeps the docker socket
    assert scoped.docker_sock is True

  def test_cw_session_set_covers_the_bros_manifest(self):
    scoped = bro.launch.scope.scoped_secrets(
      'scope-search', Surface.CW_SESSION, credential_instances={}
    )
    assert {'catalog', 'brave'} <= scoped.required

  def test_raw_session_uses_full_manifest_and_anthropic(self):
    # --raw serves the bro's own MCP servers, so it gets the full manifest (brog)
    # plus anthropic for the apiKeyHelper. bro-dev doesn't deploy → no docker socket.
    scoped = bro.launch.scope.scoped_secrets(
      'bro-dev', Surface.RAW_SESSION, credential_instances={}
    )
    assert {'brog', 'github', 'anthropic'} <= scoped.required
    assert scoped.docker_sock is False
    # --raw runs claude --bare, which ignores CLAUDE_CODE_OAUTH_TOKEN, so the token
    # secret is not requested on this surface
    assert 'claude_code' not in scoped.optional
    assert 'claude_code' not in scoped.required

  def test_raw_session_includes_optional_secrets(self):
    # searchable data sources advertise openai best-effort
    # for the query-focused fetch summary; --raw hydrates it as the optional tier.
    scoped = bro.launch.scope.scoped_secrets(
      'scope-search', Surface.RAW_SESSION, credential_instances={}
    )
    assert 'openai' in scoped.optional
    assert 'openai' not in scoped.required  # optional, not required

  def test_docker_socket_only_when_declared(self):
    docker_scope = bro.launch.scope.scoped_secrets(
      'scope-docker', Surface.RAW_SESSION, credential_instances={}
    )
    search_scope = bro.launch.scope.scoped_secrets(
      'scope-search', Surface.RAW_SESSION, credential_instances={}
    )
    assert docker_scope.docker_sock is True
    assert search_scope.docker_sock is False
    assert {'catalog', 'brave'} <= search_scope.required

  def test_bro_run_manifest_plus_llm_key_and_trails(self):
    # dev runs as an LLM process: its manifest plus its LLM key (chat_gpt →
    # openai, which needed_secrets() omits) and the mandatory trails sink
    scoped = bro.launch.scope.scoped_secrets('dev', Surface.BRO_RUN, credential_instances={})
    assert {'openai', 'trails'} <= scoped.required

  def test_bro_run_docker_socket_gated_on_needs_docker(self):
    assert (
      bro.launch.scope.scoped_secrets(
        'scope-docker', Surface.BRO_RUN, credential_instances={}
      ).docker_sock
      is True
    )
    assert (
      bro.launch.scope.scoped_secrets(
        'bro-dev', Surface.BRO_RUN, credential_instances={}
      ).docker_sock
      is False
    )

  def test_bro_run_optional_tier_carries_the_bros_optional_secrets(self):
    # searchable data sources advertise openai best-effort for the query-focused
    # fetch summary
    scoped = bro.launch.scope.scoped_secrets(
      'scope-search', Surface.BRO_RUN, credential_instances={}
    )
    assert 'openai' in scoped.optional

  def test_unknown_bro_falls_back_to_baseline_on_session_surfaces(self):
    scoped = bro.launch.scope.scoped_secrets(
      'nonexistent-bro', Surface.CW_SESSION, credential_instances={}
    )
    assert scoped.required == set(bro.launch.scope._SESSION_BASELINE)
    assert scoped.optional == set()
    assert scoped.docker_sock is True
    # a --raw fallback drops the socket: no bro to consult for needs_docker
    assert (
      bro.launch.scope.scoped_secrets(
        'nonexistent-bro', Surface.RAW_SESSION, credential_instances={}
      ).docker_sock
      is False
    )

  def test_unknown_bro_raises_for_bro_run(self):
    with pytest.raises(KeyError):
      bro.launch.scope.scoped_secrets('nonexistent-bro', Surface.BRO_RUN, credential_instances={})


class TestCredentialInstances:
  def test_substitutes_a_mapped_kind_in_the_required_tier(self):
    scoped = bro.launch.scope.scoped_secrets(
      'bro-dev', Surface.CW_SESSION, credential_instances={'brog': 'github'}
    )
    assert 'brog+github' in scoped.required
    assert 'brog' not in scoped.required
    # unmapped kinds pass through untouched
    assert 'github' in scoped.required

  def test_substitutes_a_mapped_kind_in_the_optional_tier(self):
    scoped = bro.launch.scope.scoped_secrets(
      'scope-search', Surface.RAW_SESSION, credential_instances={'openai': 'work'}
    )
    assert 'openai+work' in scoped.optional
    assert 'openai' not in scoped.optional

  def test_mapping_for_another_persona_is_ignored(self):
    scoped = bro.launch.scope.scoped_secrets(
      'bro', Surface.CW_SESSION, credential_instances={'brog': 'github'}
    )
    assert 'brog' not in scoped.required
    assert 'brog+github' not in scoped.required

  def test_mapping_of_an_unknown_kind_raises(self):
    with pytest.raises(
      bro.launch.scope.LaunchScopeError, match=r"creds maps kind\(s\).*'nonesuch'"
    ):
      bro.launch.scope.scoped_secrets(
        'bro-dev', Surface.CW_SESSION, credential_instances={'nonesuch': 'x'}
      )

  def test_substitution_applies_on_the_unknown_bro_fallback_scope(self):
    scoped = bro.launch.scope.scoped_secrets(
      'nonexistent-bro', Surface.CW_SESSION, credential_instances={'trails': 'eu'}
    )
    assert 'trails+eu' in scoped.required
    assert 'trails' not in scoped.required

  def test_overrides_see_the_substituted_names(self):
    # --grant/--revoke run after substitution, so they address kind+instance;
    # the pre-substitution kind name is no longer in the set
    scoped = bro.launch.scope.scoped_secrets(
      'bro-dev', Surface.CW_SESSION, credential_instances={'brog': 'github'}
    )
    finalized = finalize_scoped_secrets(scoped, grant=[], revoke=['brog+github'])
    assert 'brog+github' not in finalized.required
    with pytest.raises(ValueError, match="cannot revoke 'brog'"):
      finalize_scoped_secrets(scoped, grant=[], revoke=['brog'])


class TestPreflightScopedLaunch:
  # summon_allow_list is patched to keep the bro-registry import out; the
  # override semantics of each step have their own tests
  def _preflight(self, scoped, **overrides):
    kwargs = {'grant': [], 'revoke': []}
    kwargs.update(overrides)
    return bro.launch.scope.preflight_scoped_launch(scoped, 'bro-dev', **kwargs)

  def test_returns_finalized_scope_allow_list_and_store(self):
    # one unified grant list: plain names finalize the credential scope, @names
    # feed the summon allow-list
    with (
      patch('bro.launch.summon_control.summon_allow_list', return_value={'dev'}) as allow_list,
      patch(
        'bro.launch.scope.credentials.build_scoped_store', return_value={'x.cred': b'v'}
      ) as build,
    ):
      scoped, may_summon, store = self._preflight(
        bro.launch.scope.ScopedSecrets({'github'}, {'openai'}, True),
        grant=['gmail_creds', '@dev'],
        revoke=['@bro'],
      )
    assert scoped == bro.launch.scope.ScopedSecrets({'github', 'gmail_creds'}, {'openai'}, True)
    assert may_summon == {'dev'}
    assert store == {'x.cred': b'v'}
    assert allow_list.call_args == (('bro-dev',), {'grant': ['dev'], 'revoke': ['bro']})
    # the store is hydrated from the finalized tiers, not the incoming ones
    assert build.call_args == (({'github', 'gmail_creds'},), {'optional': {'openai'}})

  def test_bad_credential_override_raises_launch_scope_error(self):
    with pytest.raises(
      bro.launch.scope.LaunchScopeError, match='already in the scoped credential set'
    ):
      self._preflight(bro.launch.scope.ScopedSecrets({'github'}, set(), True), grant=['github'])

  def test_bare_bro_mark_raises_launch_scope_error(self):
    with pytest.raises(bro.launch.scope.LaunchScopeError, match="malformed grant/revoke '@'"):
      self._preflight(bro.launch.scope.ScopedSecrets(set(), set(), True), grant=['@'])

  def test_bad_summon_target_raises_launch_scope_error(self):
    with (
      patch(
        'bro.launch.summon_control.summon_allow_list',
        side_effect=ValueError('unknown summon target(s)'),
      ),
      patch('bro.launch.scope.credentials.build_scoped_store', return_value={}),
    ):
      with pytest.raises(bro.launch.scope.LaunchScopeError, match='unknown summon target'):
        self._preflight(bro.launch.scope.ScopedSecrets(set(), set(), True), grant=['@devoop'])

  def test_unresolvable_secret_raises_launch_scope_error(self):
    from bro.base import credentials

    with (
      patch('bro.launch.summon_control.summon_allow_list', return_value=set()),
      patch(
        'bro.launch.scope.credentials.build_scoped_store',
        side_effect=credentials.SecretNotFound('github'),
      ),
    ):
      with pytest.raises(bro.launch.scope.LaunchScopeError, match="secret 'github' not found"):
        self._preflight(bro.launch.scope.ScopedSecrets({'github'}, set(), True))
