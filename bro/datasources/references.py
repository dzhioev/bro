"""ready-made sources over the repo's canonical reference docs.

Each doc is declared once as a `FileSource`. A bro lists it in `data_sources`
either directly, for a dedicated `read` tool of its own, or as `man('<topic>')`,
joining the manual the bro's declared pages fold into. The instances are
stateless, so sharing them across bros is fine.
"""

from bro import prompts, reference
from bro.base.name_map import NameMap
from bro.datasources.file import FileSource
from bro.datasources.man import ManPage

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

ride = FileSource(
  'ride',
  summary=(
    'the managed-workspace runtime: harness selection and seam, solo/along modes, lifecycle '
    'verbs, workspace policy, credential scoping, container/host launch, and session state.'
  ),
  path=reference.DIRECTORY / 'ride.md',
)

dive_in = FileSource(
  'dive-in',
  summary=(
    'the task → ready-to-go session wrapper: its modes, workspace naming, the '
    'spell command it seeds as the first message, and `--host`.'
  ),
  path=reference.DIRECTORY / 'dive_in.md',
)

_PAGES = NameMap({page.name: page for page in (environment, template, conditions, ride, dive_in)})


def man(topic: str) -> ManPage:
  """declare `topic` into the manual of the bro whose `data_sources` this entry
  joins. An unknown topic raises here, at declaration."""
  return ManPage(_PAGES.resolve(topic))
