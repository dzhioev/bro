"""per-session claude state, shared by both session modes.

Owns a session's private claude state dir (`~/.claude/ride-sessions/<name>`) —
its path derivations, contents (the seeded `.claude.json`, the constructed
`settings.json`, the plugin seed, the legacy-transcript migration), the
container mounts that overlay it as `~/.claude`, its readers (the projects dir,
a session's subject line), and its teardown next to the bro.workspace.A container
reaches the dir through docker mounts, a host session through
`CLAUDE_CONFIG_DIR`. Why sessions are isolated from the host `~/.claude` — and
what the dir deliberately excludes — is reference/ride.md, "Host claude-state
isolation".
"""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.monitor import encode_project_path, trail_pointer
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace


def _session_claude_dir(name: str) -> Path:
  """the per-session claude state dir on the host — mounted as a container
  session's ~/.claude overlay."""
  return Path.home() / '.claude' / 'ride-sessions' / name


def session_trail_pointer(name: str) -> Path:
  """the host-side path of the session's current-trail pointer — where the
  recorder publishes the trail id summon control attributes the session's
  children to (`monitor/trail_pointer.py` owns the file)."""
  return _session_claude_dir(name) / trail_pointer.FILENAME


def _latest_jsonl(projects_dir: Path) -> Optional[Path]:
  if not projects_dir.is_dir():
    return None
  transcripts = [path for path in projects_dir.iterdir() if path.suffix == '.jsonl']
  if len(transcripts) == 0:
    return None
  return max(transcripts, key=lambda path: path.stat().st_mtime)


def workspace_projects_dir(workspace: Workspace) -> Path:
  """the host-side claude projects dir of a workspace's sessions."""
  session_dir = _session_claude_dir(workspace.name)
  if workspace.kind is WorkspaceKind.CONTAINER:
    return session_dir / 'projects' / '-workspace'
  # transcripts live in the session's private state dir; a worktree whose
  # sessions were recorded before the dir existed (against the host ~/.claude)
  # is read from the legacy location until a launch migrates it
  # (_migrate_legacy_transcripts)
  private = session_dir / 'projects' / encode_project_path(workspace.tree)
  if private.is_dir():
    return private
  legacy = Path.home() / '.claude' / 'projects' / encode_project_path(workspace.tree)
  if legacy.is_dir():
    return legacy
  return private


def read_subject(workspace: Workspace) -> Optional[str]:
  """the first user prompt of the workspace's latest claude session."""
  latest = _latest_jsonl(workspace_projects_dir(workspace))
  if latest is None:
    return None
  try:
    transcript = latest.open()
  except OSError:
    return None
  with transcript:
    for line in transcript:
      try:
        record = json.loads(line)
      except json.JSONDecodeError:
        continue
      if record.get('type') != 'user' or record.get('isSidechain') is True:
        continue
      content = record.get('message', {}).get('content')
      text: Optional[str] = None
      if isinstance(content, str):
        text = content
      elif isinstance(content, list):
        for block in content:
          if isinstance(block, dict) and block.get('type') == 'text':
            text = block.get('text')
            break
      if text is None:
        continue
      stripped = text.lstrip()
      if stripped.startswith('<'):
        continue
      first_line = stripped.split('\n', 1)[0].strip()
      if len(first_line) > 0:
        return first_line
  return None


def drop_workspace(workspace: Workspace) -> None:
  """remove a workspace together with its claude session state — the state dir
  must not outlive the workspace, even when the workspace removal raises."""
  try:
    workspace.remove()
  finally:
    session_dir = _session_claude_dir(workspace.name)
    if session_dir.is_dir():
      shutil.rmtree(session_dir, ignore_errors=True)


# the shared base of the session ~/.claude/settings.json, written fresh each
# launch by _write_session_settings: UX prefs only; the repo's own
# .claude/settings.json layers on top.
_SESSION_SETTINGS_JSON: dict = {
  'spinnerVerbs': {'mode': 'replace', 'verbs': ['Thinking']},
  'spinnerTipsEnabled': False,
  'prefersReducedMotion': True,
  'feedbackSurveyRate': 0,
  'tui': 'fullscreen',
  # enable the pyright-lsp Python language server. the plugin itself is provided
  # by the session dir's plugin seed (image stage in a container, the host claude
  # install's plugins dir on host); enabling alone is not enough (claude would
  # prompt to install it on .py files).
  'enabledPlugins': {'pyright-lsp@claude-plugins-official': True},
  # keep transcripts forever (no disable value exists); they back the
  # session recording
  'cleanupPeriodDays': 36500,
}

# the explicit per-session ~/.claude.json base: no onboarding prompts, no
# marketplace re-fetch, no auto-updates racing the host install. per-mode fields
# (installMethod, the trusted project entry) and the host account identity are
# layered on in _seed_claude_json.
_SESSION_CLAUDE_JSON: dict = {
  'autoUpdates': False,
  'hasCompletedOnboarding': True,
  # the official plugin marketplace is provisioned out of band (the plugin seed
  # — image stage in a container, first-run copy of the host claude install's
  # plugins on host), so claude must not re-run the auto-install network fetch
  # at session start.
  'officialMarketplaceAutoInstallAttempted': True,
  'officialMarketplaceAutoInstalled': True,
}
# account-identity keys carried over from the host so the session starts logged
# in (the OAuth bearer itself arrives via CLAUDE_CODE_OAUTH_TOKEN; these hold the
# matching account metadata claude renders the logged-in account from).
_CLAUDE_JSON_IDENTITY_KEYS = ('oauthAccount', 'userID')


def _write_session_settings(claude_dir: Path, *, container: bool) -> None:
  settings = dict(_SESSION_SETTINGS_JSON)
  if container:
    settings['skipDangerousModePermissionPrompt'] = True
  (claude_dir / 'settings.json').write_text(json.dumps(settings))


def _seed_claude_json(
  claude_dir: Path, host_file: Path, *, install_method: Optional[str], trusted_paths: Sequence[str]
) -> Path:
  """seed-once per-session private ~/.claude.json.

  built from the explicit session config plus the host's account-identity
  fields — no host machine state copied. `install_method` names the claude
  install the session runs (`global` for the image's npm install); None carries
  the host's own value, for a session running the host claude. each
  `trusted_paths` entry pre-accepts the trust dialog — a host session needs the
  main repo root alongside the worktree, since claude resolves a linked
  worktree's trust against the repository root. missing identity is fatal.
  subsequent sessions keep whatever the session last wrote.
  """
  seed = claude_dir / '.claude.json'
  if not seed.exists():
    if not host_file.is_file():
      raise SystemExit(f'missing {host_file} — log in with claude on the host first')
    host = json.loads(host_file.read_text())
    data = dict(_SESSION_CLAUDE_JSON)
    if install_method is None:
      install_method = host.get('installMethod')
    if install_method is not None:
      data['installMethod'] = install_method
    data['projects'] = {path: {'hasTrustDialogAccepted': True} for path in trusted_paths}
    for key in _CLAUDE_JSON_IDENTITY_KEYS:
      if key not in host:
        raise SystemExit(f'{host_file} has no {key!r} — log in with claude on the host first')
      data[key] = host[key]
    seed.write_text(json.dumps(data))
    seed.chmod(0o600)
  return seed


def _seed_host_plugins(claude_dir: Path) -> None:
  """first-run copy of the host claude install's plugins into the session dir —
  the host twin of the container entrypoint's /opt/claude-plugins-seed copy
  (same guard file), so the pyright-lsp enable in the session settings has its
  matching install records. a host with no plugins dir is left alone: claude
  then offers the plugin install itself."""
  if (claude_dir / 'plugins' / 'installed_plugins.json').is_file():
    return
  host_plugins = Path.home() / '.claude' / 'plugins'
  if not host_plugins.is_dir():
    return
  shutil.copytree(host_plugins, claude_dir / 'plugins', dirs_exist_ok=True)


def _migrate_legacy_transcripts(claude_dir: Path, worktree: Path) -> None:
  """copy transcripts recorded before the session had a private config dir.

  claude resolves `--resume` ids against `$CLAUDE_CONFIG_DIR/projects`; a host
  worktree whose sessions were recorded against the host `~/.claude` keeps them
  under `~/.claude/projects/<encoded-worktree-path>`. one-shot: once the session
  dir has its own projects entry, the legacy location is never consulted again.
  """
  destination = claude_dir / 'projects' / encode_project_path(worktree)
  if destination.is_dir():
    return
  legacy = Path.home() / '.claude' / 'projects' / encode_project_path(worktree)
  if not legacy.is_dir():
    return
  transcripts = [path for path in legacy.iterdir() if path.suffix == '.jsonl']
  if len(transcripts) == 0:
    return
  destination.mkdir(parents=True)
  for transcript in transcripts:
    shutil.copy2(transcript, destination / transcript.name)


def container_claude_state(name: str) -> tuple[list[str], dict[str, str]]:
  """provision the session's claude state for a container launch and return the
  (extra_mounts, extra_env) a claude-running container adds to its `Launch`.

  the mounts overlay the per-session state dir as the container's `~/.claude`
  and the seeded container-private `.claude.json` on top of `$HOME`; the env
  turns off claude's auto-updater (the image owns the install) and its
  installation checks (doctor would flag the absent host-native
  ~/.local/bin/claude)."""
  claude_dir = _session_claude_dir(name)
  claude_dir.mkdir(parents=True, exist_ok=True)
  # seed-once container-private ~/.claude.json: installMethod matches the image's
  # npm-global claude; the trusted project entry is the clone's mount point
  claude_json = _seed_claude_json(
    claude_dir, Path.home() / '.claude.json', install_method='global', trusted_paths=['/workspace']
  )
  _write_session_settings(claude_dir, container=True)
  mounts = [
    f'{claude_json}:/home/ride/.claude.json',
    f'{claude_dir}:/home/ride/.claude',
  ]
  env = {'DISABLE_AUTOUPDATER': '1', 'DISABLE_INSTALLATION_CHECKS': '1'}
  return mounts, env


def _provision_host_claude_dir(name: str, worktree: Path, project: Path) -> Path:
  """provision a host session's private claude state dir and return it — the
  value the launch points CLAUDE_CONFIG_DIR at. `project` is the main repo root
  the worktree links to. idempotent because the outer launch and the in-place
  runner both call it."""
  claude_dir = _session_claude_dir(name)
  log.verbose('provisioning the session claude state dir at %s', claude_dir)
  claude_dir.mkdir(parents=True, exist_ok=True)
  trusted_paths = [str(worktree)]
  if str(project) != str(worktree):
    trusted_paths.append(str(project))
  _seed_claude_json(
    claude_dir, Path.home() / '.claude.json', install_method=None, trusted_paths=trusted_paths
  )
  _write_session_settings(claude_dir, container=False)
  _seed_host_plugins(claude_dir)
  _migrate_legacy_transcripts(claude_dir, worktree)
  return claude_dir
