from pathlib import Path

from bro.spells import SpellFile

orchestrate = SpellFile(Path(__file__).parent / 'orchestrate.md', requires=('ask', 'open-task'))
