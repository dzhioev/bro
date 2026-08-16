from collections.abc import Iterable
from typing import TextIO

from bro.trails.display.config import DisplayConfig
from bro.trails.display.core import DisplaySession
from bro.trails.display.records import DisplayRecord
from bro.trails.display.terminal import RetainedRenderer


def retained_document(
  records: Iterable[DisplayRecord],
  configuration: DisplayConfig,
  *,
  target: TextIO | None = None,
) -> str:
  renderer = RetainedRenderer(target=target)
  with DisplaySession(configuration, renderer) as session:
    session.consume(records)
  return renderer.document()
