from unittest.mock import patch

import pytest

import llm.llms.chat_gpt
import llm.llms.echo
from cw import EFFORT_LEVELS
from cw.constants import bro_git_identity_env
from cw.docker import Launch
from do._cli import create_bro_for_run, maybe_containerize

# the run bro's own CW_BRO rides in explicitly (never as an ambient forward), so
# a calling session's theming cannot leak into the container
_RUN_ENV = {'CW_BRO': 'ppp-dev', **bro_git_identity_env()}


@pytest.fixture(autouse=True)
def scoped_store_preflight(monkeypatch):
  monkeypatch.setattr('do._cli.credentials.build_scoped_store', lambda names, optional=(): {})


def test_maybe_containerize_skips_when_inside_container():
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('cw.run_in_container') as run,
  ):
    rc = maybe_containerize(
      cli_name='call', bro_name='ppp-dev', inner_args=['hi'], in_place=False, no_trails=False
    )
  assert rc is None
  assert run.call_count == 0


def test_maybe_containerize_skips_with_in_place():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call', bro_name='ppp-dev', inner_args=['hi'], in_place=True, no_trails=False
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
      in_place=False,
      no_trails=False,
    )
  assert rc == 7
  launch = run.call_args.args[0]
  assert launch.name.startswith('call-ppp-dev-')
  assert launch.command == ['call', 'ppp-dev', 'hi', '--slow', '--in-place']
  assert run.call_args.kwargs['drop'] is True
  # ppp-dev's manifest (github + brog) + its llm key + the mandatory trails sink
  assert {'github', 'brog', 'trails'} <= launch.secrets
  # ppp-dev doesn't deploy → no docker socket
  assert launch.docker_sock is False
  # the bro's may_summon seed reaches the broker root unchanged when no flags adjust it
  assert run.call_args.kwargs['may_summon'] == {'devoops'}
  # recording on and no --into: identity + the run bro's CW_BRO is all the env
  # carries — the clone bases on the entrypoint's HEAD fallback
  assert launch.env == _RUN_ENV


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
      in_place=False,
      no_trails=True,
    )
  launch = run.call_args.args[0]
  # the env var carries the effect in, so --no-trails isn't forwarded into the inner argv
  assert launch.command == ['call', 'ppp-dev', 'hi', '--in-place']
  assert 'trails' not in launch.secrets
  assert launch.env == {'TRAILS_DISABLED': '1', **_RUN_ENV}


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
      in_place=False,
      grant_cred=['gmail_creds'],
    )
  assert rc == 0
  assert 'gmail_creds' in run.call_args.args[0].secrets


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
      in_place=False,
      revoke_cred=['github'],
    )
  assert rc == 0
  # github is in ppp-dev's manifest; the revoke drops it from the scoped set
  assert 'github' not in run.call_args.args[0].secrets


def test_maybe_containerize_revoke_removes_optional_secret():
  launch = Launch(
    name='call-ppp-dev-test',
    command=['call', 'ppp-dev', 'hi', '--in-place'],
    env=_RUN_ENV,
    secrets={'github'},
    docker_sock=False,
    tty=True,
    forward_env=True,
    optional_secrets={'openai'},
  )
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.bro_run.describe', return_value=launch),
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      in_place=False,
      revoke_cred=['openai'],
    )
  assert rc == 0
  launched = run.call_args.args[0]
  assert launched.secrets == {'github'}
  assert launched.optional_secrets == set()


def test_maybe_containerize_missing_secret_fails_before_launch(monkeypatch, capsys):
  def missing(names, optional=()):
    raise ValueError("unknown secret 'github' declared in manifest")

  monkeypatch.setattr('do._cli.credentials.build_scoped_store', missing)
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(cli_name='call', bro_name='ppp-dev', inner_args=['hi'], in_place=False)
  assert rc == 1
  assert run.call_count == 0
  assert "unknown secret 'github'" in capsys.readouterr().err


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
      in_place=False,
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
      in_place=False,
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
      in_place=False,
      grant_summon=['pm'],
      revoke_summon=['devoops'],
    )
  assert rc == 0
  assert run.call_args.kwargs['may_summon'] == {'pm'}


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
      in_place=False,
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
      in_place=False,
      grant_summon=['devoop'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'unknown summon target' in capsys.readouterr().err


def test_maybe_containerize_grant_summon_with_in_place_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      in_place=True,
      grant_summon=['devoops'],
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'require containerization' in capsys.readouterr().err


def test_maybe_containerize_grant_with_in_place_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      in_place=True,
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
      in_place=False,
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
      in_place=False,
      into='feature-branch',
    )
  assert rc == 0
  assert resolve.call_args[0][1] == 'feature-branch'
  assert run.call_args.args[0].env == {'CW_BASE_REF': 'REF-SHA', **_RUN_ENV}


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
      in_place=False,
      into='no-such-ref',
    )
  assert rc == 1
  assert run.call_count == 0
  assert 'cannot resolve --into ref: no-such-ref' in capsys.readouterr().err


def test_maybe_containerize_into_with_in_place_errors(capsys):
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = maybe_containerize(
      cli_name='call',
      bro_name='ppp-dev',
      inner_args=['hi'],
      in_place=True,
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


def test_create_bro_for_run_effort_composes_with_fast(monkeypatch):
  class _Cls:
    llm_spec = llm.llms.chat_gpt.LLMSpec(model='gpt-5.4-mini')

    @classmethod
    def create(cls, spec):
      return spec

  monkeypatch.setattr('bro.registry.get_class', lambda name: _Cls)
  spec = create_bro_for_run('x', fast=True, effort='max')
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  assert spec.service_tier == 'priority'
  # max caps at the provider top
  assert spec.reasoning_effort == 'xhigh'
  # class default untouched
  assert _Cls.llm_spec.reasoning_effort is None


def test_create_bro_for_run_effort_applies_without_fast(monkeypatch):
  class _Cls:
    llm_spec = llm.llms.chat_gpt.LLMSpec(model='gpt-5.4-mini')

    @classmethod
    def create(cls, spec):
      return spec

  monkeypatch.setattr('bro.registry.get_class', lambda name: _Cls)
  spec = create_bro_for_run('x', fast=False, effort='low')
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  assert spec.reasoning_effort == 'low'
  assert spec.service_tier is None


def test_create_bro_for_run_effort_unsupported_provider_raises(monkeypatch):
  # --effort is an explicit ask — unlike implicit fast, a provider without the
  # knob must raise, not silently fall back to the plain spec.
  class _Cls:
    llm_spec = llm.llms.echo.LLMSpec()

    @classmethod
    def create(cls, spec):
      raise AssertionError('create() should not run when effort is unsupported')

  monkeypatch.setattr('bro.registry.get_class', lambda name: _Cls)
  with pytest.raises(NotImplementedError, match='does not support an effort override'):
    create_bro_for_run('x', fast=True, effort='high')


def test_chat_gpt_accepts_every_cli_effort_level():
  # the --effort choices come from cw's EFFORT_LEVELS; the chat_gpt mapping must
  # cover the full vocabulary so no accepted flag value fails at spec build.
  for level in EFFORT_LEVELS:
    llm.llms.chat_gpt.LLMSpec().with_effort(level)


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
