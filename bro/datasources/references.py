"""ready-made sources over the repo's canonical reference docs.

Each doc is declared once as a `FileSource`. A bro mounts one directly in
`data_sources` for a dedicated `read` tool (`references.dev_style`), or mounts
`references.man` for the whole corpus behind one topic-keyed tool. The
instances are stateless, so sharing them across bros is fine.
"""

from bro import prompts, reference
from bro.datasources.file import FileSource
from bro.datasources.man import ManSource

environment = FileSource(
  'environment',
  summary=(
    'session-banner playbook: how to interpret the `bro::banner` session '
    'facts (`kind` / `name` / workspace paths). Read at session start.'
  ),
  path=prompts.get_prompt_path('environment.md'),
)

template = FileSource(
  'template',
  summary=(
    'the `{{…}}` template-directive reference: grammar, directive semantics, '
    'rendering surfaces. Read it when the meaning of a directive matters; '
    'builds on the `conditions` reference.'
  ),
  path=reference.DIRECTORY / 'template.md',
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
  path=reference.DIRECTORY / 'conditions.md',
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

ride = FileSource(
  'ride',
  summary=(
    'the managed-workspace runtime: harness selection and seam, interactive along mode, '
    'lifecycle verbs, workspace policy, and compatibility behavior.'
  ),
  path=reference.DIRECTORY / 'ride.md',
)

cw = FileSource(
  'cw',
  summary=(
    'the session launcher: workspaces, host vs container mode, scoped '
    'credentials, the flags that shape a `cw ss` session, and the env vars '
    'it forwards.'
  ),
  path=reference.DIRECTORY / 'cw.md',
)

dive_in = FileSource(
  'dive-in',
  summary=(
    'the task → ready-to-go session wrapper: its modes, workspace naming, the '
    'spell command it seeds as the first message, and `--host`.'
  ),
  path=reference.DIRECTORY / 'dive_in.md',
)

man = ManSource(
  'man',
  summary='the framework reference pages, read on demand by topic',
  pages=[environment, dev_style, template, conditions, ride, cw, dive_in],
)
