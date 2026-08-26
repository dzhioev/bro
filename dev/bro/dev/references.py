from pathlib import Path

from bro.datasources.file import FileSource

dev_style = FileSource(
  'dev-style',
  summary=(
    'the development style policy: naming, scope, comments and docs, '
    'fail-fast, teardown, test assertions, verification. Read at session '
    'start; re-read when auditing a diff against policy.'
  ),
  path=Path(__file__).resolve().parents[1] / 'prompts' / 'dev' / 'style.md',
)
