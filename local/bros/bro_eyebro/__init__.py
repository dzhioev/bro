import bro.llm.llms.openai as llm_llms_openai
from bro.datasources.references import man
from bro.local.prompts import FRAMEWORK_PROJECT
from bros.eyebro import Eyebro


class BroEyebro(Eyebro):
  name = 'bro-eyebro'
  description = 'bro framework code review: standards, guides, and quality'
  llm_spec = llm_llms_openai.LLMSpec(model='gpt-5.6-sol', reasoning_effort='xhigh')
  extra_secrets = ('github',)
  data_sources = [
    man('environment'),
    man('template'),
    man('conditions'),
    man('ride'),
  ]
  system_prompt = FRAMEWORK_PROJECT
