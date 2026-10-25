from pathlib import Path

from bro.datasources.file import FileSource

scenarios = FileSource(
  'browser-scenarios',
  summary=(
    'the browser scenarios for a bro that summons `@browser`: granting a cookies profile, '
    'signing a site into it with `webview setup`, what persists, browsing by hand, '
    'reaching the view on a remote host, and downloading files. Read before summoning '
    'the browser with a profile or downloads.'
  ),
  path=Path(__file__).resolve().parent / 'scenarios.md',
)
