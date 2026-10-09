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

rebase_conflicts = FileSource(
  'rebase-conflicts',
  summary=(
    'how to resolve a rebase that stops on a conflict: in-band resolution, '
    'the escalation bar, and what an unattended session does before it '
    'raises. Read when a rebase conflicts.'
  ),
  path=Path(__file__).resolve().parents[1] / 'prompts' / 'dev' / 'rebase_conflicts.md',
)
