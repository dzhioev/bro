from bro.datasources.references import conditions, dive_in, environment, ride, template
from bro.local.prompts import FRAMEWORK_PROJECT
from bro.mcp import man
from bros.dev import Dev

AUTHORING = """\
Once the user confirms a design, carry it out.

Follow the repository's own docs for its formatter, test gate, and package build
checks.
"""


class BroDev(Dev):
  name = 'bro-dev'
  description = 'bro framework development: task → implement → verify → land'
  features = {'brog': True}
  extra_secrets = ('github',)
  tools = [
    man(environment),
    man(template),
    man(conditions),
    man(ride),
    man(dive_in),
  ]
  system_prompt = f'{FRAMEWORK_PROJECT}\n{AUTHORING}'
