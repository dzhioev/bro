from unittest.mock import patch

import pytest

import llm.llms.chat_gpt
from do._cli import create_bro_for_run, maybe_containerize


def test_maybe_containerize_skips_when_inside_container():
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('cw.run_in_container') as run,
  ):
    rc = maybe_containerize(
      cli_name='call', bro_name='ppp-dev', inner_args=['hi'], no_container=False
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
      cli_name='call', bro_name='ppp-dev', inner_args=['hi'], no_container=True
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
      cli_name='call', bro_name='ppp-dev', inner_args=['hi', '--fast'], no_container=False
    )
  assert rc == 7
  (workspace, command), kwargs = run.call_args
  assert workspace.startswith('call-ppp-dev-')
  # the container re-runs the same CLI with CW_IN_CONTAINER set, which short-circuits
  # the re-hop so the inner process runs the bro in-process
  assert command == ['call', 'ppp-dev', 'hi', '--fast']
  assert kwargs['drop'] is True
  # ppp-dev's manifest (github + notion via flow) + its llm key + the mandatory trails sink
  assert {'github', 'notion', 'trails'} <= kwargs['secrets']
  # ppp-dev doesn't deploy → no docker socket
  assert kwargs['docker_sock'] is False


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


def test_create_bro_for_run_propagates_unsupported_fast(monkeypatch):
  class _NoFastSpec:
    def fast(self):
      raise NotImplementedError('_NoFastSpec does not support fast mode')

  class _Cls:
    llm_spec = _NoFastSpec()

    @classmethod
    def create(cls, spec):
      return spec

  monkeypatch.setattr('bro.registry.get_class', lambda name: _Cls)
  with pytest.raises(NotImplementedError, match='fast mode'):
    create_bro_for_run('x', fast=True)
