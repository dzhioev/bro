from pathlib import Path

from bro.datasources.file import FileSource

_PROMPTS = Path(__file__).resolve().parents[1] / 'prompts' / 'dev'

dev_style = FileSource(
  'dev-style',
  summary=(
    'the development style policy: naming, scope, comments and docs, '
    'fail-fast, teardown, verification, and the guide pages it points to. '
    'Read at session start; re-read when auditing a diff against policy.'
  ),
  path=_PROMPTS / 'style.md',
)

concurrency_guide = FileSource(
  'concurrency',
  summary=(
    'the concurrency guide: synchronizing on an event itself rather than a timeout, '
    'a sleep, or an observable that usually comes first, in code and in tests. Read '
    'when writing or auditing code or tests that wait on threads, processes, or async events.'
  ),
  path=_PROMPTS / 'concurrency.md',
)

testing_guide = FileSource(
  'testing',
  summary=(
    'the testing guide: what a test may assert, and when a copied value or roster is '
    'right. Read when writing or auditing tests.'
  ),
  path=_PROMPTS / 'testing.md',
)

markdown_guide = FileSource(
  'markdown',
  summary=(
    'the markdown guide: semantic line breaks in markdown prose, and one line per '
    'paragraph in GitHub bodies. Read when writing or auditing markdown, or posting an '
    'issue, pull request, or comment body.'
  ),
  path=_PROMPTS / 'markdown.md',
)

guides = (concurrency_guide, testing_guide, markdown_guide)

rebase_conflicts = FileSource(
  'rebase-conflicts',
  summary=(
    'how to resolve a rebase that stops on a conflict: in-band resolution, '
    'the escalation bar, and what an unattended session does before it '
    'raises. Read when a rebase conflicts.'
  ),
  path=_PROMPTS / 'rebase_conflicts.md',
)
