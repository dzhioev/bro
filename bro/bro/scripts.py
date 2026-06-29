import json
import os
from pathlib import Path

from base import log
from cw.constants import _CW_MODEL

_BRO_MCP_SERVER_NAME = 'bro'
# path inside the container; /host-repo is the host project bind mount (see
# _docker_create_argv). passed as claude code's apiKeyHelper so claude reads the
# api key from the `anthropic` secret without the "Detected a custom API key"
# prompt that ANTHROPIC_API_KEY would trigger.
_BRO_API_KEY_HELPER = '/host-repo/setup/print_anthropic_key.sh'


def _populate_bro_skills(proj: Path, bro_name: str) -> None:
  """populate <proj>/.claude/skills/<name>/SKILL.md as relative symlinks into the
  named bro's `bro/bros/<bro>/skills/<name>.md` files.

  two call sites:
   - container entrypoint (`cw populate-bro-skills $CW_BRO`) — `proj` is the
     workspace root; the container's ephemeral FS means no concurrency concerns.
   - host-mode `start_session` — `proj` is a per-session `tempfile.mkdtemp`
     directory passed to claude via `--add-dir`, so concurrent dive-in sessions
     on the same repo don't share `.claude/skills/`.

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
    # measure the ../-chain against the resolved parent: host mode roots this at a
    # tempfile.mkdtemp() dir, and on macOS that lands under /var/folders/… where
    # /var → /private/var. the kernel resolves the symlink from the physical
    # (one-level-deeper) path, so a relpath against the logical dir would dangle.
    rel = os.path.relpath(src, target_dir.resolve())
    link.symlink_to(rel)
    log.info('populated .claude/skills/%s/SKILL.md → %s', name, rel)


def _bro_claude_argv(bro_name: str) -> list[str]:
  """build the clean claude argv for `cw ss --bro <bro_name>`.

  resolves the bro to extract its system prompt (no shared/base prepend), wires
  its declared MCP servers + data sources through the `mcp-server bro:<name>`
  stdio shim, and uses `--bare` + `--strict-mcp-config` + `--tools ""` to start
  claude with no project/user CLAUDE.md, no host MCP servers, no built-in
  skills, and only the bro's MCP tools. supplies apiKeyHelper via `--settings`
  (flagSettings, not project/local) so claude executes it without a workspace
  trust gate.
  """
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  mcp_config = json.dumps(
    {
      'mcpServers': {
        _BRO_MCP_SERVER_NAME: {'command': 'mcp-server', 'args': [f'bro:{bro_name}']},
      },
    },
    separators=(',', ':'),
  )
  settings = json.dumps({'apiKeyHelper': _BRO_API_KEY_HELPER}, separators=(',', ':'))
  return [
    '--model',
    _CW_MODEL,
    '--bare',
    '--strict-mcp-config',
    '--mcp-config',
    mcp_config,
    '--settings',
    settings,
    '--system-prompt',
    bro.system_prompt,
    '--tools',
    '',
    '--allowed-tools',
    f'mcp__{_BRO_MCP_SERVER_NAME}__*',
  ]
