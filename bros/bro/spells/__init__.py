from pathlib import Path

from bro.spells import SpellFile

_DIRECTORY = Path(__file__).parent

ask = SpellFile(_DIRECTORY / 'ask.md')
watch = SpellFile(_DIRECTORY / 'watch.md')
