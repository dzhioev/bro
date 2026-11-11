from pathlib import Path

from bro.datasources.file import FileSource

scenarios = FileSource(
  'browser-scenarios',
  summary=(
    'the browser scenarios for a bro that summons `@browser`: what the browser does and '
    'refuses, wording a request, the talk rights for steering and follow-ups and the '
    'owner-led `browse` session, granting a cookies profile, signing a site into it with '
    '`webview setup`, what persists, reaching the view on a remote host, and downloading '
    'files. Read before summoning the browser.'
  ),
  path=Path(__file__).resolve().parent / 'scenarios.md',
)
