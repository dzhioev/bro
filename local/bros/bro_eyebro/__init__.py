from bro.datasources.references import conditions, environment, extending, ride, template
from bro.local.prompts import FRAMEWORK_PROJECT
from bro.mcp import man
from bros.eyebro import Eyebro


class BroEyebro(Eyebro):
  name = 'bro-eyebro'
  description = 'bro framework code review: standards, guides, and quality'
  features = {'github': True}
  tools = [
    man(environment),
    man(template),
    man(conditions),
    man(ride),
    man(extending),
  ]
  system_prompt = FRAMEWORK_PROJECT
