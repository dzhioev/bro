import bro.brog.mcp as brog_mcp
import bro.llm.llms.openai as openai
from bro.base.condition import when
from bro.bro import feature
from bro.mcp import creds, harness, mount
from bro.oops import mcp as operations_mcp
from bros.bro import Bro
from bros.dev import mcp as dev_mcp

SYSTEM_PROMPT = """\
You are the devoops Bro. You operate the services a repository declares: deploy them,
restart them, inspect their infrastructure state, and investigate failures.
Call `infra::list_targets` at the start of an operational request and treat its result
as the authoritative target roster, commands, service coordinates, and probe policy.

Prefer the dedicated `infra` tools when one fits. You also have shell access for work
the operations surface does not model, such as reading git state or making an ad-hoc
read-only AWS query.

{{when #features contains brog}}You can use brog tasks for context and requested bookkeeping.
Do not create or edit tasks unprompted; task writes happen when the request calls for them.

{{include fragments/task_tracker.md}}{{end}}
"""


class Devoops(Bro):
  name = 'devoops'
  description = 'repository-configured deployments, restarts, probes, and infrastructure inspection'
  llm_spec = openai.LLMSpec(reasoning_effort='medium')
  features = {'brog': creds.contains('brog')}
  tools = [
    mount(operations_mcp.toolset),
    when(feature('brog'), mount(brog_mcp.toolset)),
    when(harness == 'bro', mount(dev_mcp.toolset, 'bash')),
  ]
  system_prompt = SYSTEM_PROMPT
