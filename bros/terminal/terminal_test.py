from bro.bro import BaseBro
from bro.llm import providers
from bro.llm.llms import openai
from bros.bro import Bro
from bros.terminal import Terminal


def test_terminal_is_a_standalone_container_developer():
  terminal = Terminal()
  assert isinstance(terminal, BaseBro)
  assert not isinstance(terminal, Bro)
  assert terminal.name == 'terminal'
  assert terminal._data_sources == []
  assert terminal._features == {}
  assert terminal.spell_descriptions() == []
  assert 'working alone inside a container' in terminal.description
  assert 'without waiting for confirmation' in terminal.system_prompt
  assert 'rather than reading a development policy' in terminal.system_prompt


def test_terminal_compacts_long_openai_runs():
  spec = Terminal.llm_spec
  assert isinstance(spec, openai.LLMSpec)
  assert spec.compact_threshold == 200_000


def test_llm_model_selection_preserves_terminal_compaction():
  spec = providers.resolve(Terminal.llm_spec, providers.parse(':sol'))
  assert isinstance(spec, openai.LLMSpec)
  assert spec.model == 'gpt-5.6-sol'
  assert spec.compact_threshold == 200_000
