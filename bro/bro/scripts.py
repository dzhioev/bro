import json
import os
from dataclasses import dataclass
from pathlib import Path

from base import log
from cw.constants import _CW_MODEL
from cw.mcp import _container_mcp_launch

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


@dataclass(frozen=True)
class _BroLaunch:
  """the two halves of a `cw ss --bro` launch: the claude argv, and the container
  env (`CW_MCP_HTTP_SPEC` / `PORT` / `TOKEN`) the entrypoint reads to start the
  session-local MCP server the argv's mcp-config points at."""

  claude_argv: list[str]
  extra_env: dict[str, str]


def _bro_launch(bro_name: str) -> _BroLaunch:
  """build the clean claude argv + container env for `cw ss --bro <bro_name>`.

  resolves the bro to extract its Claude-flavored system prompt
  (`claude_system_prompt` — the tool-name rule teaches the `mcp__ns__tool` wire
  form these MCP mounts produce; no cw base-prompt prepend) and enumerate its
  MCP namespaces. the bro's tools are served by a session-local
  HTTP MCP server (`mcp-server bro:<name> --http`) that the container entrypoint
  starts and health-gates before launching claude, so the heavy bro import
  happens off claude's critical path and the first turn — which a seeded `-p`
  prompt fires the moment the REPL is up — already has every tool connected.
  the mcp-config carries one `{type: http}` entry per namespace, mounted under
  the namespace as the server key, so tools surface as `mcp__<namespace>__<tool>`
  (the convention in prompts/tool_names.md); a per-session bearer token guards
  the endpoints. `--bare` + `--strict-mcp-config` + `--tools ""` start claude
  with no project/user CLAUDE.md, no host MCP servers, no built-in skills, and
  only the bro's MCP tools. supplies apiKeyHelper via `--settings`
  (flagSettings, not project/local) so claude executes it without a workspace
  trust gate.

  `--bare` is minimal mode: it runs no hooks at all, so these sessions never run
  the project's session-log upload hooks. `containers.run_in_container` syncs
  their transcript host-side once the container exits (`_sync_container_log`).
  """
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  namespaces = list(dict.fromkeys(s.namespace for s in bro.claude_bro_mcp_servers()))
  extra_env, mcp_config = _container_mcp_launch(f'bro:{bro_name}', namespaces)
  settings = json.dumps({'apiKeyHelper': _BRO_API_KEY_HELPER}, separators=(',', ':'))
  claude_argv = [
    '--model',
    _CW_MODEL,
    '--bare',
    '--strict-mcp-config',
    '--mcp-config',
    mcp_config,
    '--settings',
    settings,
    '--system-prompt',
    bro.claude_system_prompt,
    '--tools',
    '',
    '--allowed-tools',
    ','.join(f'mcp__{ns}__*' for ns in namespaces),
  ]
  return _BroLaunch(claude_argv=claude_argv, extra_env=extra_env)
