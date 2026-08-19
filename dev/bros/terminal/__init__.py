from bro.base.condition import when
from bro.bro import BaseBro
from bro.llm.llms import openai
from bro.mcp import harness, mount
from bros.dev import mcp as dev_mcp


class Terminal(BaseBro):
  name = 'terminal'
  description = 'software developer working alone inside a container'
  tools = [when(harness == 'bro', mount(dev_mcp.toolset))]
  llm_spec = openai.LLMSpec(compact_threshold=200_000)
  system_prompt = """\
You are a software developer working alone inside a container, with tools to read,
search, edit files, and run shell commands. Use them to complete the user's task.
Begin directly with the task rather than reading a development policy. No human
channel exists, so act without waiting for confirmation.
"""
