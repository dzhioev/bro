from pathlib import Path

from bro.datasources.file import FileSource

harness = FileSource(
  'claude-harness',
  summary=(
    'the Claude Code harness: its pinned release, session runner, argv and native tool gates, '
    'session-local MCP serving, append prompt, watch delivery and turn ends, Claude state '
    'isolation, summoned Claude sessions, and the transcript recorder.'
  ),
  path=Path(__file__).resolve().parent / 'reference.md',
)
