import json

import pytest
from pydantic import BaseModel

import bro.llm.mu as mu_module
from bro.base import credentials
from bro.llm.llm import LLMStopped
from bro.llm.mu import mu
from bro.llm.wire import File, Text, Wire


class _Answer(BaseModel):
  answer: str


@pytest.fixture
def material(monkeypatch):
  value = {
    'llm': 'openai:gpt-5.6-terra,reasoning_effort=low',
    'credential': {'api_key': 'default-key'},
  }

  def read(name):
    assert name == 'mu'
    return value

  monkeypatch.setattr(credentials, 'get_json', read)
  return value


@pytest.fixture
def calls(monkeypatch):
  calls = []

  class _Wire(Wire):
    def __init__(self, recipe, secrets):
      self.recipe = recipe
      self.secrets = secrets

    async def exchange(self, inputs, tools, request_timeout):
      raise AssertionError('mu must use structured')

    def restore(self, prefix):
      raise AssertionError('mu must be stateless')

    async def structured(self, instructions, result, content, request_timeout):
      calls.append((self.recipe, self.secrets, instructions, content, request_timeout))
      return result(answer='42')

  monkeypatch.setattr(mu_module.wires, 'open', _Wire)
  return calls


def test_call_resolves_the_default_recipe_and_encodes_neutral_text(material, calls):
  assert (
    mu('what is it?', _Answer, Text('the record'), File(b'pdf', 'application/pdf')).answer == '42'
  )
  recipe, secrets, prompt, contents, timeout = calls[0]
  assert recipe.model == 'gpt-5.6-terra'
  assert recipe.request_options()['reasoning']['effort'] == 'low'
  assert secrets == material['credential']
  assert prompt == 'what is it?'
  assert contents == (Text('the record'), File(b'pdf', 'application/pdf'))
  assert timeout is None


@pytest.mark.asyncio
async def test_aio_selects_a_defined_role_and_defaults_for_an_unknown_role(material, calls):
  material['roles'] = {
    'reader': {
      'llm': 'openai:gpt-5.6-luna,reasoning_effort=high',
      'credential': {'api_key': 'reader-key'},
    }
  }
  assert (await mu.aio('question', _Answer, Text('input'), role='reader')).answer == '42'
  await mu.aio('question', _Answer, Text('input'), role='dispatch')
  assert calls[0][0].model == 'gpt-5.6-luna'
  assert calls[0][0].request_options()['reasoning']['effort'] == 'high'
  assert calls[0][1] == {'api_key': 'reader-key'}
  assert calls[1][1] == material['credential']


@pytest.mark.parametrize(
  ('value', 'fault'),
  [
    ([], 'dictionary'),
    ({}, 'llm'),
    ({'llm': 7, 'credential': {'api_key': 'key'}}, 'llm'),
    ({'llm': 'unknown:model', 'credential': {'api_key': 'key'}}, 'unknown provider'),
    ({'llm': 'openai:a:b:c', 'credential': {'api_key': 'key'}}, 'not listed'),
    ({'llm': '::high', 'credential': {'api_key': 'key'}}, 'expected provider:model'),
    ({'llm': 'openai', 'credential': {}}, 'credential'),
    ({'llm': 'openai', 'credential': []}, 'credential'),
    ({'llm': 'openai'}, 'credential'),
    ({'llm': 'openai', 'credential': {'api_key': 'key'}, 'roles': {'reader': {}}}, 'roles'),
    ({'llm': 'openai', 'credential': {'api_key': 'key'}, 'roles': []}, 'roles'),
  ],
)
def test_bad_material_names_mu_and_never_reaches_a_wire(monkeypatch, value, fault):
  monkeypatch.setattr(credentials, 'get_json', lambda name: value)
  monkeypatch.setattr(mu_module.wires, 'open', lambda *args: pytest.fail('bad material'))
  with pytest.raises(ValueError, match=f'(?s)mu credential:.*{fault}'):
    mu('question', _Answer, Text('input'), role='reader')


@pytest.mark.parametrize(
  ('recipe', 'secret', 'fault'),
  [
    ('anthropic:claude-opus-5-5', {'api_key': 'key'}, 'has no client'),
    ('echo:echo', {'api_key': 'key'}, 'has no structured'),
    ('openai:gpt-5.6-terra', {'wrong': 'key'}, 'api_key'),
  ],
)
def test_unusable_wire_or_provider_credential_is_a_mu_error(material, recipe, secret, fault):
  material.update(llm=recipe, credential=secret)
  with pytest.raises(ValueError, match=f'(?s)mu credential:.*{fault}'):
    mu('question', _Answer, Text('input'))


def test_missing_material_names_mu(monkeypatch):
  def read(name):
    raise ValueError('not found')

  monkeypatch.setattr(credentials, 'get_json', read)
  with pytest.raises(ValueError, match='mu credential:.*not found'):
    mu('question', _Answer, Text('input'))


def test_a_wire_stop_propagates_unchanged(material, monkeypatch):
  class _Stopped:
    async def structured(self, *args, **kwargs):
      raise LLMStopped('filtered', 'content_filter')

  monkeypatch.setattr(mu_module.wires, 'open', lambda *args: _Stopped())
  with pytest.raises(LLMStopped, match='filtered.*content_filter'):
    mu('question', _Answer, Text('input'))


@pytest.mark.parametrize(
  ('reference', 'selection', 'expected'),
  [
    ('openai+', {'openai': 'work'}, 'empty-key'),
    ('openai+work', {}, 'work-key'),
    ('openai', {'openai': 'work'}, 'work-key'),
    ('openai', {}, 'empty-key'),
  ],
)
def test_references_follow_explicit_instances_or_the_hydrating_selection(
  tmp_path, calls, reference, selection, expected
):
  directory = tmp_path / 'host'
  (directory / 'creds').mkdir(parents=True)
  (directory / 'creds' / 'openai.cred').write_text('{"api_key":"empty-key"}')
  (directory / 'creds' / 'openai+work.cred').write_text('{"api_key":"work-key"}')
  (directory / 'creds' / 'mu+cheap.cred').write_text(
    json.dumps({'llm': 'openai:gpt-5.6-terra', 'credential': {'$cred': reference}})
  )
  host = credentials.Store(credentials.default_registry(), directory, {'mu': 'cheap', **selection})
  files, hydrated = credentials.build_scoped_store(host, ['mu'])
  assert hydrated == {'mu'}
  assert json.loads(files['creds/mu+cheap.cred'])['credential'] == {'api_key': expected}
  session_directory = tmp_path / 'session'
  for name, content in files.items():
    path = session_directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
  session = credentials.Store(credentials.default_registry(), session_directory, {})
  with credentials.as_default_store(session):
    assert mu('question', _Answer, Text('input')).answer == '42'
  assert calls[0][1] == {'api_key': expected}


def test_empty_content_is_refused_before_configuration(monkeypatch):
  monkeypatch.setattr(credentials, 'get_json', lambda name: pytest.fail('empty content'))
  with pytest.raises(ValueError, match='must not be empty'):
    mu('question', _Answer)
