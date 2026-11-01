import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from bro.launch.run import run_main


@pytest.fixture
def in_process_run(monkeypatch):
  bro = MagicMock(name='bro')
  bro.name = 'dev'
  runner = MagicMock(name='runner')
  runner.run = AsyncMock(return_value='done')
  monkeypatch.setattr('bro.launch.run.launch_llm_spec', lambda name, args: None)
  monkeypatch.setattr('bro.registry.create_bro', lambda name: bro)
  monkeypatch.setattr('bro.launch.run.Runner', lambda declared, recipe, activity_file: runner)
  monkeypatch.setattr('bro.launch.run._ask_observer', lambda name: MagicMock())
  monkeypatch.setattr('bro.launch.broxy.session_broxy', contextlib.nullcontext)
  return runner


def test_runs_in_the_calling_process(in_process_run):
  assert run_main(['bro', 'dev', 'hello'], program=['bro', 'run']) is None
  in_process_run.run.assert_awaited_once()
  assert in_process_run.run.call_args.args == ('hello',)
  assert in_process_run.run.call_args.kwargs['surface'] == 'ask'
  assert in_process_run.run.call_args.kwargs['hold'] == 'unattended'


def test_a_malformed_resolved_recipe_is_refused(monkeypatch, caplog):
  monkeypatch.setattr('bro.launch.run.Runner', lambda *args, **kwargs: pytest.fail('started'))
  argv = ['bro', 'dev', 'hello', '--resolved-llm', '{"type":']
  assert run_main(argv, program=['bro', 'run']) == 1
  assert 'invalid --resolved-llm recipe' in caplog.text


@pytest.mark.parametrize(
  'flag',
  ['--summon', '--grant', '--revoke', '--into', '--no-trails', '--timeout', '--detach'],
)
def test_runtime_flags_are_not_accepted(flag):
  with pytest.raises(SystemExit):
    run_main(['bro', 'dev', 'hello', flag], program=['bro', 'run'])


@pytest.mark.parametrize('surface', ['run', 'chat'])
@pytest.mark.parametrize('recipe', [None, ':echo', '::high', '+fast'])
def test_native_surfaces_refuse_an_absent_or_incomplete_recipe(
  surface, recipe, register_test_bros, monkeypatch, caplog
):
  from bro.bro import BaseBro
  from bro.launch.call import chat_main

  class RecipeLess(BaseBro):
    name = 'recipe-less'

  register_test_bros(RecipeLess)
  monkeypatch.setattr(
    'bro.launch.run.Runner', lambda *args, **kwargs: pytest.fail('started runner')
  )
  monkeypatch.setattr(
    'bro.launch.call.Runner', lambda *args, **kwargs: pytest.fail('started runner')
  )
  arguments = ['bro', 'recipe-less', 'hello'] + ([] if recipe is None else ['--llm', recipe])
  main = run_main if surface == 'run' else chat_main
  assert main(arguments, program=['bro', surface]) == 1
  assert 'recipe-less has no recipe for harness bro' in caplog.text
  assert '~/.bro.json' in caplog.text


def test_resolved_recipe_excludes_passthrough_piece_flag():
  with pytest.raises(SystemExit):
    run_main(
      ['bro', 'dev', 'read', '--resolved-llm', '{}', '--passthrough'], program=['bro', 'run']
    )


def test_bro_run_uses_the_live_installation_stack(monkeypatch, tmp_path, home_settings_file):
  import json

  from bro.base import host_config
  from bro.launch.run import run_llm_spec
  from bro.llm.providers import LLMSelection, bind_recipe

  home_settings_file.write_text('[llm]\nbro = "openai:gpt-5.6-terra"\n')
  text = 'openai:gpt-6.1-sol,reasoning_effort=high,compact_threshold=12345'
  path = tmp_path / 'host.json'
  path.write_text(json.dumps({'homes': {'test-home': {'bros': {'dev': {'llm': {'bro': text}}}}}}))
  monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', path)
  recipe = run_llm_spec('dev', LLMSelection())
  assert recipe == bind_recipe(text)
  assert run_llm_spec('eyebro', LLMSelection()) == bind_recipe('openai:gpt-5.6-terra')
