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
  monkeypatch.setattr('bro.registry.create_bro', lambda name, llm_spec: bro)
  monkeypatch.setattr('bro.launch.run.Runner', lambda declared, activity_file: runner)
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
  assert 'recipe-less declares no LLM recipe: pass --llm provider:model' in caplog.text
  assert '~/.bro.json' not in caplog.text


def test_in_process_selection_does_not_read_the_managed_host_list(register_test_bros, monkeypatch):
  from bro.bro import BaseBro
  from bro.launch.run import run_llm_spec
  from bro.llm.providers import LLMSelection, parse
  from bro.native.echo_declaration import Recipe
  from ride.authority_reading import Reading

  class RecipeLess(BaseBro):
    name = 'recipe-less'

  register_test_bros(RecipeLess)
  monkeypatch.setattr(Reading, 'take', lambda *_: pytest.fail('read managed configuration'))
  assert run_llm_spec('recipe-less', parse('echo:chosen+passthrough')) == Recipe(
    model='chosen', passthrough=True
  )
  with pytest.raises(ValueError, match='declares no LLM recipe'):
    run_llm_spec('recipe-less', LLMSelection())


@pytest.mark.parametrize('surface', ['run', 'chat'])
@pytest.mark.parametrize('encoded', [False, True])
def test_native_launch_refuses_an_unlisted_driven_recipe_before_the_runner(
  surface, encoded, monkeypatch, register_test_bros, caplog
):
  import json

  from bro.bro import BaseBro
  from bro.launch.call import chat_main
  from bro.openai.declaration import Recipe

  class Reader(BaseBro):
    name = 'unlisted-reader'
    llm_spec = Recipe(model='gpt-7')

  register_test_bros(Reader)
  monkeypatch.setattr('bro.launch.run.Runner', lambda *args, **kwargs: pytest.fail('started'))
  monkeypatch.setattr('bro.launch.call.Runner', lambda *args, **kwargs: pytest.fail('started'))
  main = run_main if surface == 'run' else chat_main
  arguments = ['bro', Reader.name, 'read']
  if encoded:
    declared = Reader.llm_spec
    assert declared is not None
    arguments += ['--resolved-llm', json.dumps(declared.dump())]
  assert main(arguments, program=['bro', surface]) == 1
  assert 'gpt-7' in caplog.text and '+passthrough' in caplog.text


def test_native_unlisted_declaration_accepts_a_listed_override_or_flag(register_test_bros):
  from bro.bro import BaseBro
  from bro.launch.run import run_llm_spec
  from bro.llm.providers import parse
  from bro.openai.declaration import Recipe

  class Reader(BaseBro):
    name = 'unlisted-reader'
    llm_spec = Recipe(model='gpt-7')

  register_test_bros(Reader)
  spec = run_llm_spec(Reader.name, parse(':sol'))
  assert spec is not None and spec.admitted() and not spec.passthrough
  spec = run_llm_spec(Reader.name, parse('+passthrough'))
  assert spec is not None and spec.passthrough and spec.selection()['model'] == 'gpt-7'


def test_resolved_recipe_excludes_passthrough_piece_flag():
  with pytest.raises(SystemExit):
    run_main(
      ['bro', 'dev', 'read', '--resolved-llm', '{}', '--passthrough'], program=['bro', 'run']
    )
