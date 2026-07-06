from unittest.mock import patch

import llm.llms.chat_gpt
from do._cli import create_bro_for_run, maybe_containerize


def test_maybe_containerize_skips_when_inside_container():
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('cw.run_in_container') as run,
  ):
    rc = maybe_containerize(
      cli_name='call', bro_name='ppp-dev', inner_args=['hi'], no_container=False, no_trails=False
    )
  assert rc is None
  assert run.call_count == 0


def test_maybe_containerize_skips_with_no_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call', bro_name='ppp-dev', inner_args=['hi'], no_container=True, no_trails=False
    )
  assert rc is None
  assert run.call_count == 0


def test_maybe_containerize_hops_and_scopes_to_bro():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=7) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi', '--slow'],
      no_container=False,
      no_trails=False,
    )
  assert rc == 7
  (workspace, command), kwargs = run.call_args
  assert workspace.startswith('call-ppp-dev-')
  # the container re-runs the same CLI with CW_IN_CONTAINER set, which short-circuits
  # the re-hop so the inner process runs the bro in-process
  assert command == ['call', 'ppp-dev', 'hi', '--slow']
  assert kwargs['drop'] is True
  # ppp-dev's manifest (github + notion via flow) + its llm key + the mandatory trails sink
  assert {'github', 'notion', 'trails'} <= kwargs['secrets']
  # ppp-dev doesn't deploy → no docker socket
  assert kwargs['docker_sock'] is False
  # LLM-process container, not Claude Code: the ambient CW_BRO must not leak in
  assert kwargs['forward_bro'] is False
  # the bro's may_summon seed reaches the broker root unchanged when no flags adjust it
  assert kwargs['may_summon'] == {'devoops'}
  # recording on and no --into: nothing to put in the env — the clone bases on
  # the entrypoint's HEAD fallback
  assert kwargs['extra_env'] is None


def test_maybe_containerize_no_trails_drops_secret_and_disables_recording():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      no_trails=True,
    )
  (_workspace, command), kwargs = run.call_args
  # the env var carries the effect in, so --no-trails isn't forwarded into the inner argv
  assert command == ['call', 'ppp-dev', 'hi']
  assert 'trails' not in kwargs['secrets']
  assert kwargs['extra_env'] == {'TRAILS_DISABLED': '1'}


def test_maybe_containerize_grant_adds_secret():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      grant_cred=['gmail_creds'],
    )
  assert rc == 0
  _, kwargs = run.call_args
  assert 'gmail_creds' in kwargs['secrets']


def test_maybe_containerize_revoke_removes_secret():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      revoke_cred=['github'],
    )
  assert rc == 0
  _, kwargs = run.call_args
  # github is in ppp-dev's manifest; the revoke drops it from the scoped set
  assert 'github' not in kwargs['secrets']


def test_maybe_containerize_grant_already_present_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    # trails is always in the bro-run set, so granting it is a no-op → error
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      grant_cred=['trails'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'already in the scoped' in capsys.readouterr().err


def test_maybe_containerize_revoke_absent_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      revoke_cred=['nonexistent'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'not in the scoped' in capsys.readouterr().err


def test_maybe_containerize_grant_summon_extends_the_allow_list():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      grant_summon=['pm'],
      revoke_summon=['devoops'],
    )
  assert rc == 0
  _, kwargs = run.call_args
  assert kwargs['may_summon'] == {'pm'}


def test_maybe_containerize_summon_grant_already_allowed_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      grant_summon=['devoops'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'already in the summon allow-list' in capsys.readouterr().err


def test_maybe_containerize_unregistered_summon_target_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      grant_summon=['devoop'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'unknown summon target' in capsys.readouterr().err


def test_maybe_containerize_grant_summon_with_no_container_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=True,
      grant_summon=['devoops'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'require containerization' in capsys.readouterr().err


def test_maybe_containerize_grant_with_no_container_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=True,
      grant_cred=['gmail_creds'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'require containerization' in capsys.readouterr().err


def test_maybe_containerize_grant_inside_container_errors(capsys):
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('cw.run_in_container') as run,
  ):
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      revoke_cred=['github'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'require containerization' in capsys.readouterr().err


def test_maybe_containerize_into_bases_the_clone_on_the_ref():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
    patch('cw.resolve_ref', return_value='REF-SHA') as resolve,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      into='feature-branch',
    )
  assert rc == 0
  assert resolve.call_args[0][1] == 'feature-branch'
  _, kwargs = run.call_args
  assert kwargs['extra_env'] == {'CW_BASE_REF': 'REF-SHA'}


def test_maybe_containerize_unresolvable_into_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
    patch('cw.resolve_ref', return_value=None),
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=False,
      into='no-such-ref',
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'cannot resolve --into ref: no-such-ref' in capsys.readouterr().err


def test_maybe_containerize_into_with_no_container_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      no_container=True,
      into='feature-branch',
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'require containerization' in capsys.readouterr().err


def test_create_bro_for_run_without_fast_uses_create_bro(monkeypatch):
  captured: list[str] = []

  def fake_create_bro(name):
    captured.append(name)
    return 'plain-bro'

  monkeypatch.setattr('bro.registry.create_bro', fake_create_bro)
  result = create_bro_for_run('mybro', fast=False)
  assert result == 'plain-bro'
  assert captured == ['mybro']


def test_create_bro_for_run_with_fast_applies_fast_spec(monkeypatch):
  class _Cls:
    llm_spec = llm.llms.chat_gpt.LLMSpec(model='gpt-5.4-mini')

    @classmethod
    def create(cls, spec):
      # stand in for BaseBro.create: return the spec so the test can inspect it
      return spec

  monkeypatch.setattr('bro.registry.get_class', lambda name: _Cls)
  spec = create_bro_for_run('x', fast=True)
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  assert spec.service_tier == 'priority'
  # class default untouched — fast() returns a fresh spec
  assert _Cls.llm_spec.service_tier is None


def test_create_bro_for_run_unsupported_fast_falls_back_to_plain(monkeypatch):
  # fast is the implicit default for these CLIs, so a provider with no fast mode
  # must degrade to the plain spec rather than raise — the user never asked for fast.
  class _NoFastSpec:
    def fast(self):
      raise NotImplementedError('_NoFastSpec does not support fast mode')

  class _Cls:
    llm_spec = _NoFastSpec()

    @classmethod
    def create(cls, spec):
      raise AssertionError('create() should not run when fast is unsupported')

  monkeypatch.setattr('bro.registry.get_class', lambda name: _Cls)
  monkeypatch.setattr('bro.registry.create_bro', lambda name: f'plain-{name}')
  assert create_bro_for_run('x', fast=True) == 'plain-x'
