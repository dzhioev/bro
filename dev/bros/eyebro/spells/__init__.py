from pathlib import Path

from bro.spells import SpellFile

_DIRECTORY = Path(__file__).parent

review_diff = SpellFile(_DIRECTORY / 'review-diff.md')
review = SpellFile(_DIRECTORY / 'review.md', requires=('review-diff', 'watch'))
