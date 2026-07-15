"""ready-made `FileSource` instances over the repo's canonical reference docs.

A bro mounts one by listing it in `data_sources` (`references.environment`);
the instances are stateless, so sharing them across bros is fine.
"""

import prompts
from base.project_root import PROJECT_ROOT
from bro.datasources.file import FileSource

environment = FileSource(
  'environment',
  summary=(
    'session-banner playbook: how to interpret the `bro::banner` session '
    'facts (`launch_command` / `kind` / `name`). Read at session start.'
  ),
  path=prompts.get_prompt_path('environment.md'),
)

template = FileSource(
  'template',
  summary=(
    'the `{{…}}` template-directive reference: grammar, directive semantics, '
    'rendering surfaces. Read it when the meaning of a directive matters; '
    'builds on the `conditions` source.'
  ),
  path=PROJECT_ROOT / 'reference' / 'template.md',
  # the payload is the directive syntax itself; rendering would execute it
  render=False,
)

conditions = FileSource(
  'conditions',
  summary=(
    'the declarative conditioning model: typed variables, condition '
    'combinators (`eq` / `contains`), `when` / `select` for declarative '
    'lists, the facts triple.'
  ),
  path=PROJECT_ROOT / 'reference' / 'conditions.md',
  # carries directive examples; rendering would execute them
  render=False,
)

dev_style = FileSource(
  'dev-style',
  summary=(
    'the development style policy: naming, scope, comments and docs, '
    'fail-fast, teardown, verification. Read at session start; re-read '
    'when auditing a diff against policy.'
  ),
  path=prompts.get_prompt_path('dev/style.md'),
)
