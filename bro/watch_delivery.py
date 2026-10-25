"""How a batch of watch lines reaches a model: each watch's lines kept whole in the
results store and delivered as their head."""

from dataclasses import dataclass

from bro import results, watches
from bro.base.text_window import BYTE_LIMIT

KEPT_TOOL = 'watch'


@dataclass(frozen=True)
class KeptLines:
  """The lines of one watch a delivery cut from its message, kept in the results store."""

  command: str
  result: str
  hidden: int


@dataclass(frozen=True)
class Delivery:
  """What the model reads of a batch, the lines that text shows whole, and the
  watches whose remaining lines it cuts."""

  text: str
  shown: tuple[watches.BatchLine, ...]
  kept: tuple[KeptLines, ...]


def deliver(batch: watches.Batch, store: results.Store) -> Delivery:
  """`batch` as the model reads it: each watch's lines are kept whole in `store`,
  and the message carries them up to `BYTE_LIMIT` characters, closing on the
  `bro::page` call that reads on where they are cut."""
  parts: list[str] = []
  shown: list[watches.BatchLine] = []
  kept: list[KeptLines] = []
  for group in batch.by_command():
    text = '\n'.join(line.text for line in group)
    command = group[0].command
    result_id = store.keep(KEPT_TOOL, {'command': command}, text)
    parts.append(results.window(result_id, text, 0, BYTE_LIMIT))
    size = -1
    visible = 0
    for line in group:
      size += len(line.text) + 1
      if size > BYTE_LIMIT:
        break
      visible += 1
    shown.extend(group[:visible])
    if visible < len(group):
      kept.append(KeptLines(command or '', result_id, len(group) - visible))
  return Delivery('\n'.join(parts), tuple(shown), tuple(kept))
