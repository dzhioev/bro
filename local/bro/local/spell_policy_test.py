"""a `spells` declaration names its files, so a spell file no bro declares is
content nothing serves; one test holds every checked-in spell file to a
declaration."""

from pathlib import Path

from bro import spells as spell_store
from bro.dev.packaging_policy import distribution_roots
from bro.local.run_tests import BENCHMARK
from bro.registry import list_classes

_ROOT = Path(__file__).resolve().parents[3]


def _declared_spell_files() -> set[Path]:
  files: set[Path] = set()
  for cls in list_classes():
    for base in cls.__mro__:
      declaration = vars(base).get('spells')
      if declaration is not None:
        files.update(spell_store.declared_spells(base, declaration).values())
  return files


def test_every_checked_in_spell_file_is_declared_by_a_bro():
  checked_in = {
    path.resolve()
    for root in distribution_roots(_ROOT, (BENCHMARK,))
    for path in root.glob('bros/*/spells/**/*.md')
  }
  assert len(checked_in) > 0
  undeclared = sorted(str(path.relative_to(_ROOT)) for path in checked_in - _declared_spell_files())
  assert undeclared == []
