import bro.llm.llms.openai as llm_llms_openai
from bro.datasources.references import man
from bro.local.prompts import FRAMEWORK_PROJECT
from bros.dev import Dev

AUTHORING = """\
Once the user confirms a design, carry it out.

Follow the repository's own docs for its formatter, test gate, and package build
checks.
"""


class BroDev(Dev):
  name = 'bro-dev'
  description = 'bro framework development: task → implement → verify → land'
  llm_spec = llm_llms_openai.LLMSpec(model='gpt-5.6-sol', reasoning_effort='high')
  features = {'brog': True}
  extra_secrets = ('github',)
  data_sources = [
    man('environment'),
    man('template'),
    man('conditions'),
    man('ride'),
    man('dive-in'),
  ]
  system_prompt = f'{FRAMEWORK_PROJECT}\n{AUTHORING}'
