"""The webview session command."""

from __future__ import annotations

from typing import Optional

from bro.base import log

__cli_name__ = 'webview'


def main(argv: list[str]) -> Optional[int]:
  if len(argv) == 2 and argv[1] == 'serve':
    from bro.webview import serve

    try:
      import asyncio

      asyncio.run(serve.serve())
    except Exception as error:
      log.error('webview serve failed: %s', error)
      return 1
    return 0
  log.error('usage: webview serve')
  return 2
