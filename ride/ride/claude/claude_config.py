"""per-session claude state, shared by both session modes.

Owns the claude state dir a workspace's sessions run against — its path
derivations, contents (the seeded `.claude.json`, the constructed
`settings.json`, the plugin seed), and its readers (the projects dir, a
session's subject line). Both session modes name the dir to claude through
`CLAUDE_CONFIG_DIR`; a container additionally bind-mounts it, since the dir
lives host-side and the container is `--rm`'d at exit. Why sessions are
isolated from the host `~/.claude` — and what the dir deliberately excludes —
is reference/ride.md, "Host claude-state isolation".
"""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.monitor import CLAUDE_CONFIG_DIR_ENV, encode_project_path, workspace_claude_dir
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace

_CONTAINER_CLAUDE_DIR = '/home/ride/.claude'
_CONTAINER_PLUGIN_SEED = Path('/opt/claude-plugins-seed')


def latest_jsonl(projects_dir: Path) -> Optional[Path]:
  if not projects_dir.is_dir():
    return None
  transcripts = [path for path in projects_dir.iterdir() if path.suffix == '.jsonl']
  if len(transcripts) == 0:
    return None
  return max(transcripts, key=lambda path: path.stat().st_mtime)


def workspace_projects_dir(workspace: Workspace) -> Path:
  """the host-side claude projects dir of a workspace's sessions."""
  session_dir = workspace_claude_dir(workspace.path)
  if workspace.isolation is Isolation.BOXED:
    return session_dir / 'projects' / '-workspace'
  return session_dir / 'projects' / encode_project_path(workspace.tree)


def read_subject(workspace: Workspace) -> Optional[str]:
  """the first user prompt of the workspace's latest claude session."""
  latest = latest_jsonl(workspace_projects_dir(workspace))
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


# the session ~/.claude/settings.json, written fresh each launch: UX prefs and
# the session-behavior opt-outs, nothing machine-specific.
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
  # claude's own default is on
  'autoMemoryEnabled': False,
  'env': {'DISABLE_AUTOUPDATER': '1'},
}

# the explicit per-session ~/.claude.json base: no onboarding prompts, no
# marketplace re-fetch. per-mode fields (installMethod, the trusted project
# entry) and the host account identity are layered on in _seed_claude_json.
_SESSION_CLAUDE_JSON: dict = {
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


def _seed_claude_json(
  claude_dir: Path, host_file: Path, *, install_method: Optional[str], trusted_paths: Sequence[str]
) -> None:
  """seed-once per-session private `.claude.json` — claude reads it from the
  state dir named by `CLAUDE_CONFIG_DIR`.

  built from the explicit session config plus the host's account-identity
  fields — no host machine state copied. `install_method` names the claude
  install the session runs (`global` for the image's npm install); None carries
  the launcher's own value, for an unboxed session running its Claude install.
  Each `trusted_paths` entry pre-accepts the trust dialog.
  When the launcher has no host file, the explicit session base is sufficient;
  a present host file must carry complete identity.
  Subsequent sessions keep whatever the session last wrote.
  """
  seed = claude_dir / '.claude.json'
  if not seed.exists():
    host = json.loads(host_file.read_text()) if host_file.is_file() else None
    data = dict(_SESSION_CLAUDE_JSON)
    if install_method is None and host is not None:
      install_method = host.get('installMethod')
    if install_method is not None:
      data['installMethod'] = install_method
    data['projects'] = {path: {'hasTrustDialogAccepted': True} for path in trusted_paths}
    if host is not None:
      for key in _CLAUDE_JSON_IDENTITY_KEYS:
        if key not in host:
          raise SystemExit(f'{host_file} has no {key!r} — log in with claude on the host first')
        data[key] = host[key]
    seed.write_text(json.dumps(data))
    seed.chmod(0o600)


def seed_session_plugins(claude_dir: Path, *, container: bool) -> None:
  """Seed a session's plugin records once from its installation."""
  if (claude_dir / 'plugins' / 'installed_plugins.json').is_file():
    return
  source = _CONTAINER_PLUGIN_SEED if container else Path.home() / '.claude' / 'plugins'
  if not source.is_dir():
    return
  shutil.copytree(source, claude_dir / 'plugins', dirs_exist_ok=True)


def _provision_session_claude_dir(
  workspace: Path,
  *,
  install_method: Optional[str],
  trusted_paths: Sequence[str],
  preaccept_bypass_dialog: bool,
) -> Path:
  """provision a session's private claude state dir and return it. idempotent
  because both launch layers apply it: the `.claude.json` is seeded once, the
  settings rewritten every time."""
  claude_dir = workspace_claude_dir(workspace)
  log.verbose('provisioning the session claude state dir at %s', claude_dir)
  claude_dir.mkdir(parents=True, exist_ok=True)
  _seed_claude_json(
    claude_dir,
    Path.home() / '.claude.json',
    install_method=install_method,
    trusted_paths=trusted_paths,
  )
  settings = dict(_SESSION_SETTINGS_JSON)
  if preaccept_bypass_dialog:
    settings['skipDangerousModePermissionPrompt'] = True
  (claude_dir / 'settings.json').write_text(json.dumps(settings))
  return claude_dir


def container_claude_state(workspace: Path) -> tuple[list[str], dict[str, str]]:
  """provision the workspace's claude state for a container launch and return
  the (extra_mounts, extra_env) a claude-running container adds to its `Launch`.

  the mount carries the host-side state dir into the container, where the state
  must not live: the container is `--rm`'d at exit while the workspace outlives
  it. the env names that dir to claude and turns off its installation checks
  (doctor would flag the absent host-native ~/.local/bin/claude).

  the workspace is an isolated clone, so the `--dangerously-skip-permissions`
  acceptance dialog is pre-answered."""
  claude_dir = _provision_session_claude_dir(
    workspace,
    install_method='global',
    trusted_paths=['/workspace'],
    preaccept_bypass_dialog=True,
  )
  mounts = [f'{claude_dir}:{_CONTAINER_CLAUDE_DIR}']
  env = {
    CLAUDE_CONFIG_DIR_ENV: _CONTAINER_CLAUDE_DIR,
    'DISABLE_INSTALLATION_CHECKS': '1',
  }
  return mounts, env


def provision_unboxed_claude_dir(workspace: Path, tree: Path) -> Path:
  """Provision an unboxed session's private Claude state directory."""
  claude_dir = _provision_session_claude_dir(
    workspace,
    install_method=None,
    trusted_paths=[str(tree)],
    preaccept_bypass_dialog=False,
  )
  return claude_dir
