"""launch claude, optionally in an isolated docker container.

host mode (default): cw owns the worktree lifecycle — it creates the worktree
(`var/cw/worktrees/<name>`, `worktree-<name>` branch + submodule alternates),
provisions it with the shared setup/provision_repo.sh (same as the container
entrypoint), then spawns the worktree's own `cw ss --in-place` (the in-place
session runner, cw/runner.py), which runs plain `claude` from inside it (not
`claude -w`, so no claude-side worktree/provisioning hooks). On exit it drops the
worktree (`--drop`) or, interactively, offers to. cw writes its pid to the
per-worktree git admin dir so `cw list`/`clean` can tell a session is live.

container mode (--container): /workspace is a fresh clone, not a worktree — the
gitfile-based worktree layout doesn't survive the container boundary, and this
keeps the container's git state genuinely isolated. layout:

  - var/cw/containers/<name>/ on the host → /workspace rw
    (empty on first run; entrypoint clones host repo into it)
  - host project root → /host-repo ro
    (clone --shared reads objects from here via alternates; also the source for
    local submodule clones to avoid needing ssh keys in the container)
  - ~/.claude.json: not bind-mounted from host. Constructed per workspace from
    an explicit config + the host's account-identity fields into a container-
    private file at cw-sessions/<name>/.claude.json and bind-mounted from there,
    so per-project state mutations (mcpServers, allowedTools,
    hasTrustDialogAccepted) stay in the container and can't be used to escalate
    to code execution in the next host claude session.
  - ~/.claude: not seeded from host. cw-sessions/<name>/ is mounted as the
    container's ~/.claude and gets the constructed settings.json; host machine
    state stays on the host.
  - a per-launch scoped credential store at /home/cw/.ppp: the host resolves only
    the secrets the session uses into an in-memory tar and `docker cp`s it into
    the container before it starts (no host-side store, no bind mount), with a
    credentials.json that bounds the container's registry to them. Living in the
    container's own writable layer, the store is removed with the container on
    --rm exit (or by `cw clean`), so plaintext secrets never linger on the host.
    github, aws, and the claude_code OAuth token arrive as declared secrets in
    this store, each wired into its consumer by an install hook (git / the aws
    CLI / the CLAUDE_CODE_OAUTH_TOKEN env a native claude code session
    authenticates with). So there is no out-of-band github-token bind-mount, no
    ~/.aws mount, and no OAuth credentials file: one stable bearer per session
    means no token to refresh or sync between sessions. (`--bro` / `do`
    containers run claude --bare against the anthropic api key instead.)

network is not restricted by design.

This package re-exports below exactly the cross-package surface that `do/` and
`dive_in.py` consume, so those callers keep `import cw` / `from cw import …`
unchanged. Intra-package code imports submodule → submodule (never through this
hub — partial-init hazard); see cw/CLAUDE.md.
"""

from cw.banner import render_banner
from cw.cli import build_parser
from cw.containers import run_in_container
from cw.flags import add_forwarded_flags, extract_forwarded_argv
from cw.git import resolve_ref
from cw.paths import _containers_dir, _project_root, _worktrees_dir
from cw.secrets import bro_run_secrets
from cw.summon import summon_allow_list
