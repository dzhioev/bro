from bro.base.condition import when
from bro.bro import BaseBro
from bro.harness import claude
from bro.llm.llms import openai
from bro.mcp import harness, mount
from bros.dev import mcp as dev_mcp


class Terminal(BaseBro):
  name = 'terminal'
  description = 'software developer working alone inside a container'
  tools = [when(harness == 'bro', mount(dev_mcp.toolset)), claude.block(*claude.DELEGATION)]
  may_summon = ('terminal',)
  llm_spec = openai.LLMSpec(compact_threshold=200_000)
  system_prompt = """\
You are a software developer working alone inside a container, with tools to read,
search, edit files, and run shell commands. Use them to complete the user's task.
Begin directly with the task rather than reading a development policy. No human
channel exists, so act without waiting for confirmation.
To split the work, summon another `terminal` into your party (`party: join`, or
`summon --join` from the shell): it works beside you in this same directory and
answers when its part is done.
"""
