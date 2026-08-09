import bro.llm.llms.chat_gpt as llm_llms_chat_gpt
from bro.bros.dev import Dev
from bro.datasources import references

SYSTEM_PROMPT = """\
## Bro framework project

You are operating inside the bro framework repository. Read the root and relevant
subsystem `CLAUDE.md` files before changing code; they carry the repository's
non-obvious development rules.

The environment is normally provisioned with its venv active, so development
commands and repository CLIs run by bare name. Follow the repository's own docs
for its formatter, test gate, and package build checks.

Text assets may carry `{{…}}` conditioning directives. Use the template reference
source when their exact grammar or rendering semantics matter.

Never stage credential stores or synthesized secret directories. Framework code
must stay consumer-neutral: extension packages contribute personas, credentials,
task backends, and toolsets through the documented entry-point groups.
"""


class BroDev(Dev):
  name = 'bro-dev'
  description = 'bro framework development: task → implement → verify → land'
  llm_spec = llm_llms_chat_gpt.LLMSpec(model='gpt-5.6', reasoning_effort='high')
  features = {'brog': True}
  extra_secrets = ('github',)
  data_sources = [references.environment, references.template, references.conditions]
  system_prompt = SYSTEM_PROMPT
