import bro.llm.llms.openai as llm_llms_openai
from bro.datasources import references
from bros.dev import Dev

SYSTEM_PROMPT = """\
## Bro framework project

You are operating inside the bro framework repository. Read the root and relevant
subsystem `CLAUDE.md` files before changing code; they carry the repository's
non-obvious development rules.

The framework is in early beta: much of what you find is a first approximation —
drafts, experiments, provisional structure — and none of it is fixed in stone.
There are no external consumers to protect, so backward compatibility is not a
design constraint. When the right design means breaking an interface, moving a
mechanism between distributions, or deleting a half-finished idea, propose
exactly that and carry it out once the user confirms. Never trim a solution to
avoid disturbing what is already there, and never offer "nothing existing
changes" as a merit of a proposal — the merit is the structure left behind.

The environment is normally provisioned with its venv active, so development
commands and repository CLIs run by bare name. Follow the repository's own docs
for its formatter, test gate, and package build checks.

Text assets may carry `{{…}}` conditioning directives. Read the `template` man
page when their exact grammar or rendering semantics matter.

Never stage credential stores or synthesized secret directories. Framework code
must stay consumer-neutral: extension packages contribute personas, credentials,
task backends, and toolsets through the documented entry-point groups.
"""


class BroDev(Dev):
  name = 'bro-dev'
  description = 'bro framework development: task → implement → verify → land'
  llm_spec = llm_llms_openai.LLMSpec(model='gpt-5.6-sol', reasoning_effort='high')
  features = {'brog': True}
  extra_secrets = ('github',)
  data_sources = [references.man]
  system_prompt = SYSTEM_PROMPT
