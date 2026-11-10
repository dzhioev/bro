from pathlib import Path

from bro.spells import SpellFile

_DIRECTORY = Path(__file__).parent

audit = SpellFile(_DIRECTORY / 'audit.md')
design = SpellFile(_DIRECTORY / 'design.md')
land = SpellFile(_DIRECTORY / 'land.md')
open_task = SpellFile(_DIRECTORY / 'open-task.md')
run_pr = SpellFile(_DIRECTORY / 'run-pr.md', requires=('land', 'follow-pr', 'watch'))
reflect = SpellFile(_DIRECTORY / 'reflect.md', requires=('run-pr',))
follow_pr = SpellFile(_DIRECTORY / 'follow-pr.md', requires=('watch', 'land'))
bump_bro = SpellFile(_DIRECTORY / 'bump-bro.md', requires=('run-pr',))
fix = SpellFile(_DIRECTORY / 'fix.md', requires=('open-task', 'design', 'run-pr'))
