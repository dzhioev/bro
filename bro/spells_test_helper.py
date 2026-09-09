"""a package on disk for a test bro to declare spells from."""

import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from bro.bro import BaseBro


@dataclass(frozen=True)
class SpellPackage:
  name: str
  # the `spells` declaration naming every file written under the package
  spells: tuple[str, ...]

  def bro_class(self, parent: type[BaseBro] = BaseBro, **attributes: Any) -> type[BaseBro]:
    return type(
      f'BroFor{self.name}',
      (parent,),
      {
        '__module__': self.name,
        'name': self.name.removeprefix('_'),
        'description': 'test bro',
        'spells': self.spells,
        **attributes,
      },
    )


def spell_package(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
  name: str,
  spells: Optional[dict[str, str]] = None,
) -> SpellPackage:
  """write package `name` under `tmp_path`, its spells given as path under
  `spells/` without the `.md` suffix → file content, and register it as an
  importable module for the test's duration."""
  package_dir = tmp_path / name
  package_dir.mkdir()
  init_path = package_dir / '__init__.py'
  init_path.write_text('')
  declared: list[str] = []
  if spells is not None:
    for relative, content in spells.items():
      path = package_dir / 'spells' / f'{relative}.md'
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(content)
      declared.append(f'{relative}.md')
  module = types.ModuleType(name)
  module.__file__ = str(init_path)
  monkeypatch.setitem(sys.modules, name, module)
  return SpellPackage(name, tuple(declared))
