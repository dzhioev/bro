from pathlib import Path

from base import log


def _populate_bro_skills(skills_root: Path, bro_name: str) -> None:
  """write the named bro's skills into a fresh root for claude's `--add-dir` discovery.

  Skill bodies are rendered for the claude harness before being written to
  `.claude/skills/<name>/SKILL.md` in the per-session root.
  """
  import llm.mcp
  from base import credentials
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  skills_dir = skills_root / '.claude' / 'skills'
  for name, source in bro.skills.items():
    target_dir = skills_dir / name
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / 'SKILL.md').write_text(
      llm.mcp.render_text(
        source.read_text(), harness='claude', wire='mcp', creds=credentials.known_names()
      )
    )
    log.info('populated .claude/skills/%s/SKILL.md from %s', name, source)
