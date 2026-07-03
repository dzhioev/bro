import os
from pathlib import Path

from base import log


def _populate_bro_skills(proj: Path, bro_name: str) -> None:
  """populate <proj>/.claude/skills/<name>/SKILL.md as relative symlinks into the
  named bro's `bro/bros/<bro>/skills/<name>.md` files.

  called by the in-place session runner (`cw ss --in-place`): `proj` is a
  per-session `tempfile.mkdtemp` directory passed to claude via `--add-dir`, so
  concurrent sessions on the same repo don't share `.claude/skills/`.

  cleanup is symlink-aware: any existing `<name>/SKILL.md` that's a symlink is
  removed (and its parent dir cleaned up if empty) before recreating. static
  skills (regular SKILL.md files) are left untouched.
  """
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  skills_dir = proj / '.claude' / 'skills'
  skills_dir.mkdir(parents=True, exist_ok=True)
  for child in skills_dir.iterdir():
    if not child.is_dir():
      continue
    skill_md = child / 'SKILL.md'
    if skill_md.is_symlink():
      skill_md.unlink()
      try:
        child.rmdir()
      except OSError:
        pass
  for name, src in bro.skills.items():
    target_dir = skills_dir / name
    target_dir.mkdir(parents=True, exist_ok=True)
    link = target_dir / 'SKILL.md'
    # measure the ../-chain against the resolved parent: the runner roots this at a
    # tempfile.mkdtemp() dir, and on macOS that lands under /var/folders/… where
    # /var → /private/var. the kernel resolves the symlink from the physical
    # (one-level-deeper) path, so a relpath against the logical dir would dangle.
    rel = os.path.relpath(src, target_dir.resolve())
    link.symlink_to(rel)
    log.info('populated .claude/skills/%s/SKILL.md → %s', name, rel)
