import bro.llm.llms.openai as llm_llms_openai
from bro.local.prompts import FRAMEWORK_PROJECT
from bro.mcp import man
from bros.eyebro import Eyebro


class BroEyebro(Eyebro):
  name = 'bro-eyebro'
  description = 'bro framework code review: standards, guides, and quality'
  llm_spec = llm_llms_openai.LLMSpec(model='gpt-6.1-sol', reasoning_effort='xhigh')
  features = {'github': True}
  tools = [
    man('environment'),
    man('template'),
    man('conditions'),
    man('ride'),
    man('extending'),
  ]
  system_prompt = FRAMEWORK_PROJECT
