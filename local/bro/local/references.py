from pathlib import Path

from bro.datasources.file import FileSource

bump_claude_code = FileSource(
  'bump-claude-code',
  summary=(
    'the [[bump]] playbook for Claude Code, the release every managed Claude session runs: '
    'its pin and bump script, its changelog range, the surfaces the harness reaches it '
    'through, its live-probe gate, and its commit form.'
  ),
  path=Path(__file__).resolve().parent / 'bump_claude_code.md',
)
