from pathlib import Path

from bro.spells import SpellFile

_DIRECTORY = Path(__file__).parent

audit = SpellFile(_DIRECTORY / 'audit.md')
commit = SpellFile(_DIRECTORY / 'commit.md')
design = SpellFile(_DIRECTORY / 'design.md')
land = SpellFile(_DIRECTORY / 'land.md')
open_task = SpellFile(_DIRECTORY / 'open-task.md')
run_pr = SpellFile(_DIRECTORY / 'run-pr.md', requires=('commit', 'land', 'follow-pr', 'watch'))
resume_pr = SpellFile(_DIRECTORY / 'resume-pr.md', requires=('run-pr', 'follow-pr', 'land'))
reflect = SpellFile(_DIRECTORY / 'reflect.md', requires=('run-pr',))
follow_pr = SpellFile(
  _DIRECTORY / 'follow-pr.md', requires=('commit', 'watch', 'land', 'resume-pr')
)
bump_bro = SpellFile(_DIRECTORY / 'bump-bro.md', requires=('commit', 'run-pr'))
fix = SpellFile(_DIRECTORY / 'fix.md', requires=('open-task', 'design', 'commit', 'run-pr'))
plan = SpellFile(_DIRECTORY / 'plan.md', requires=('design', 'follow-pr', 'ask'))
integrate = SpellFile(_DIRECTORY / 'integrate.md', requires=('run-pr', 'land', 'follow-pr'))
