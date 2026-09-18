from typing import get_args

import bro.mcp as mcp
from bro.bro import BaseBro
from bro.harness import claude
from bro.llm import providers
from bro.llm.llms import openai
from bros.bro import Bro
from bros.terminal import Terminal


def test_terminal_opts_out_of_the_shared_bro_defaults():
  terminal = Terminal()
  assert isinstance(terminal, BaseBro)
  assert not isinstance(terminal, Bro)
  assert terminal._data_sources == []
  assert terminal._features == {}
  assert terminal.spell_descriptions() == []


def test_llm_model_selection_preserves_terminal_compaction():
  declared = Terminal.llm_spec
  assert isinstance(declared, openai.LLMSpec)
  assert declared.compact_threshold is not None
  spec = providers.resolve(declared, providers.parse(':sol'))
  assert isinstance(spec, openai.LLMSpec)
  assert spec.model == openai.MODELS['sol']
  assert spec.compact_threshold == declared.compact_threshold


def test_terminal_declares_an_unrestricted_shell_on_both_harnesses():
  for harness in get_args(mcp.Harness):
    assert Terminal()._selected_tools_for(harness).shell_unrestricted is True


def test_terminal_withholds_claudes_own_delegation():
  blocked = Terminal().blocked_tool_names('claude')

  assert set(claude.DELEGATION) <= set(blocked)
  assert Terminal().blocked_tool_names('bro') == ()
