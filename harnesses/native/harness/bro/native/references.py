from pathlib import Path

from bro.datasources.file import FileSource

harness = FileSource(
  'bro-harness',
  summary=(
    "the bro harness, bro-native's own LLM loop: its session runner, the tool groups it serves, "
    'watch delivery and turn ends, recording and usage, resume, and the in-process `bro run` '
    'and `bro chat` surfaces.'
  ),
  path=Path(__file__).resolve().parent / 'reference.md',
)
