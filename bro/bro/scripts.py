from pathlib import Path

from base import log

# names of the skill dirs the previous populate wrote into a skills root, one per
# line — the discriminator that lets re-population wipe its own stale output while
# leaving static skills (hand-authored SKILL.md files) untouched.
_RENDERED_MANIFEST = '.cw-rendered'


def _populate_bro_skills(project: Path, bro_name: str) -> None:
  """populate <project>/.claude/skills/<name>/SKILL.md with the named bro's
  `bro/bros/<bro>/skills/<name>.md` files, template directives rendered for
  the claude harness (`llm.mcp.render_text`) — a cw-session reads these as
  slash commands, so it must see the claude-side procedures.

  called by the in-place session runner (`cw ss --in-place`): `project` is a
  per-session `tempfile.mkdtemp` directory passed to claude via `--add-dir`, so
  concurrent sessions on the same repo don't share `.claude/skills/`.

  cleanup removes what a previous populate wrote (tracked in the skills root's
  manifest file) before recreating; static skills are left untouched.
  """
  import llm.mcp
  from base import credentials
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  skills_dir = project / '.claude' / 'skills'
  skills_dir.mkdir(parents=True, exist_ok=True)
  manifest = skills_dir / _RENDERED_MANIFEST
  if manifest.is_file():
    for stale_name in manifest.read_text().splitlines():
      stale = skills_dir / stale_name / 'SKILL.md'
      stale.unlink(missing_ok=True)
      try:
        stale.parent.rmdir()
      except OSError:
        pass
  written: list[str] = []
  for name, src in bro.skills.items():
    target_dir = skills_dir / name
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / 'SKILL.md').write_text(
      llm.mcp.render_text(
        src.read_text(), harness='claude', wire='mcp', creds=credentials.known_names()
      )
    )
    written.append(name)
    log.info('populated .claude/skills/%s/SKILL.md from %s', name, src)
  manifest.write_text(''.join(f'{name}\n' for name in written))
