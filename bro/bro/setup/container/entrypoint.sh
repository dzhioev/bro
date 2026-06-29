#!/usr/bin/env -S bash -e

# root phase: align container user with host uid/gid, then re-exec as cw
if [ "$(id -u)" = "0" ] && [ -z "${CW_ENTRYPOINT_REEXEC:-}" ]; then
  TARGET_UID="$(stat -c '%u' /workspace)"
  TARGET_GID="$(stat -c '%g' /workspace)"
  # skip remapping when detected uid is 0 — on Docker for Mac, virtiofs reports
  # bind mounts as root-owned but handles permissions transparently; remapping to
  # uid 0 would make claude refuse --dangerously-skip-permissions
  if [ "$TARGET_UID" != "0" ]; then
    if [ "$(id -u cw)" != "$TARGET_UID" ] || [ "$(id -g cw)" != "$TARGET_GID" ]; then
      groupmod -o -g "$TARGET_GID" cw
      usermod -o -u "$TARGET_UID" -g "$TARGET_GID" cw
      chown cw:cw /home/cw /home/cw/.claude
    fi
  fi
  # align the in-container `docker` group's gid with the bind-mounted host
  # socket's gid so cw can talk to the host daemon without sudo
  if [ -S /var/run/docker.sock ]; then
    SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"
    if [ "$(getent group docker | cut -d: -f3)" != "$SOCK_GID" ]; then
      groupmod -o -g "$SOCK_GID" docker
    fi
  fi
  # the scoped credential store is `docker cp`'d into /home/cw/.ppp before start
  # (cw/containers.py), landing owned by the uid baked into the tar. re-own it to cw after the
  # remap above so the resolver and install hooks (run as cw) can read the 0600
  # files — on Linux (cw remapped to the host uid) and on Docker for Mac (remap
  # skipped, cw keeps its image uid).
  if [ -d /home/cw/.ppp ]; then
    chown -R cw:cw /home/cw/.ppp
  fi
  export CW_ENTRYPOINT_REEXEC=1
  exec gosu cw "$0" "$@"
fi

# --- running as cw from here ---

# ~/.claude is not seeded from the host: cw constructs ~/.claude.json and
# settings.json and syncs credentials; host machine state stays on the host.

# seed host git config into a writable copy (the host file is bind-mounted
# read-only at /host-gitconfig; git config --global needs atomic rename).
# done before the clone so init.defaultBranch suppresses the git hint
if [ -f /host-gitconfig ] && [ ! -f "$HOME/.gitconfig" ]; then
  cp /host-gitconfig "$HOME/.gitconfig"
fi

# mark /workspace safe for git. on Docker for Mac, virtiofs reports the bind
# mount as root-owned even though cw can read/write it (see uid-remap skip in
# the root phase); without this, git refuses with "dubious ownership"
git config --global --add safe.directory /workspace

# first-run clone: /workspace starts empty; host repo is bind-mounted at /host-repo
# read-only. clone --shared reuses /host-repo/.git/objects via alternates so there's
# no disk duplication. origin is retargeted to the host's upstream (so `git push`
# goes to GitHub, matching host-mode worktrees), and we add `host` as a local
# remote for fetching commits that haven't been pushed upstream yet.
if [ ! -d /workspace/.git ]; then
  echo 'cloning host repo into /workspace' >&2
  git config --global --add safe.directory /host-repo
  cd /
  git -c protocol.file.allow=always clone --shared /host-repo /workspace >&2
  cd /workspace
  host_origin="$(git -C /host-repo config --get remote.origin.url)"
  # convert ssh URL to https so the container can push with a token
  host_origin="$(echo "$host_origin" | sed 's|^git@github\.com:|https://github.com/|')"
  git remote set-url origin "$host_origin"
  git remote add host /host-repo
  # refresh refs/remotes/origin/master (the clone copied /host-repo's possibly-stale
  # local copy) so later ancestry/clean checks and rebases compare against the real
  # upstream. ref-only — objects are already shared via alternates, no token needed.
  git fetch host '+refs/remotes/origin/master:refs/remotes/origin/master' >&2
  # branch worktree-<CW_NAME> from CW_BASE_REF: the host's current HEAD by default
  # (the clone is already checked out there, matching host-mode worktrees), or the
  # sha `cw ss --into <ref>` resolved on the host. -B resets if a stale
  # worktree-<CW_NAME> branch came through with the clone. either base's objects are
  # shared from /host-repo via the clone's alternates, so no extra fetch is needed.
  git checkout -B "worktree-$CW_NAME" "${CW_BASE_REF:-HEAD}" >&2
  # init submodules from host-local paths — .gitmodules uses ssh URLs and the
  # container has no ssh keys. skip any submodule the host hasn't initialized.
  if [ -f .gitmodules ]; then
    git config -f .gitmodules --get-regexp '^submodule\..*\.path$' \
      | while IFS=' ' read -r key path; do
          name="${key#submodule.}"; name="${name%.path}"
          if [ ! -e "/host-repo/$path/.git" ]; then
            echo "skipping submodule $name: /host-repo/$path not initialized on host" >&2
            continue
          fi
          echo "initializing submodule $name from /host-repo/$path" >&2
          git -c "submodule.$name.url=/host-repo/$path" \
              -c protocol.file.allow=always \
              submodule update --init -- "$path" >&2
        done
  fi
fi

# block non-fast-forward pushes and (for bro sessions) direct pushes to master
hooks_dir="$(git -C /workspace rev-parse --git-dir)/hooks"
mkdir -p "$hooks_dir"
cat > "$hooks_dir/pre-push" << 'HOOK'
#!/usr/bin/env -S bash -e
while read -r _ local_sha remote_ref remote_sha; do
  case "$remote_ref" in
    refs/heads/master|refs/heads/main)
      if [ "${GIT_AUTHOR_EMAIL:-}" = "dzhioev+bro@gmail.com" ]; then
        echo "error: bro cannot push directly to $remote_ref — create a PR instead" >&2
        exit 1
      fi
      ;;
  esac
  [ "$remote_sha" = "0000000000000000000000000000000000000000" ] && continue
  if ! git merge-base --is-ancestor "$remote_sha" "$local_sha"; then
    echo "error: non-fast-forward push to $remote_ref is blocked" >&2
    exit 1
  fi
done
HOOK
chmod +x "$hooks_dir/pre-push"

# pre-create the /workspace transcript directory (trust is granted in the
# constructed ~/.claude.json, not here)
mkdir -p "$HOME/.claude/projects/-workspace"

# seed the pre-installed plugins baked into the image (pyright-lsp). ~/.claude is
# bind-mounted from a fresh per-session dir, so the build-time install staged at
# /opt is copied in on first run. settings.json enables the plugin (cw/docker.py); this
# provides the matching install records so claude doesn't prompt to install it on
# .py files.
if [ -d /opt/claude-plugins-seed ] && [ ! -f "$HOME/.claude/plugins/installed_plugins.json" ]; then
  mkdir -p "$HOME/.claude/plugins"
  cp -r /opt/claude-plugins-seed/. "$HOME/.claude/plugins/"
fi

# reuse the venv baked into the image (deps + editable project already installed,
# its module finder pointing at /workspace — see the Dockerfile) instead of a fresh
# `uv sync` (~3.4s). symlink it in and stamp provision_repo.sh's skip marker newer
# than the just-cloned uv.lock/pyproject so the sync is skipped: the image tag pins
# both, so the baked env always matches this clone's deps. provision still runs to
# regenerate the console-script bridge + git hooks. absent /opt/cw-venv (older
# image) or a pre-existing /workspace/.venv (reused workspace) falls through to a
# normal sync.
if [ "${CW_SKIP_VENV:-}" != "1" ] && [ -d /opt/cw-venv ] && [ ! -e /workspace/.venv ]; then
  ln -s /opt/cw-venv /workspace/.venv
  touch /workspace/.venv/.provision-stamp
  # the baked venv also carries a `_entrypoints.py` bridge generated from this
  # image's [project.scripts]; the tag pins that table, so it matches this clone.
  # tell provision_repo.sh to skip the regen (the only other thing it does is the
  # console-script bridge + git hooks).
  export CW_VENV_BAKED=1
fi

# provision the cloned repo (venv sync if stale, console-script bridge, post-commit
# hook, git alias) — shared with host setup_repo.sh and the worktree session-start
# hook. then activate the venv so child processes (hooks, MCP servers, Bash tool)
# inherit it. CW_SKIP_VENV (smoke test only) skips the whole venv-dependent block.
if [ "${CW_SKIP_VENV:-}" != "1" ]; then
  /workspace/setup/provision_repo.sh >&2
  source /workspace/.venv/bin/activate
fi

# secrets resolve from the scoped credential store bind-mounted at ~/.ppp (see
# cw/containers.py). wire each into the tool that consumes it from outside the resolver (git
# credential helper, the aws CLI's ~/.aws/credentials, ...) via its registry-declared
# install hook — one generic step, no per-secret logic here. env exports must
# persist into `exec`, so this is eval'd in the entrypoint shell.
#
# capture before eval: `eval "$(credentials install-hooks)"` would mask a generator
# crash — the failed command substitution yields empty stdout, `eval ""` exits 0, and
# set -e never fires, so claude would launch with credentials unwired. a plain
# assignment propagates the substitution's exit status to set -e, aborting the launch.
if [ "${CW_SKIP_VENV:-}" != "1" ]; then
  install_hooks="$(credentials install-hooks)"
  eval "$install_hooks"
fi

# themed native sessions (dive-in / `cw ss` with CW_BRO set) surface the bro's
# skills as Claude Code slash commands by symlinking them into .claude/skills/.
# a `--bro` session runs `claude --bare`, which skips skills auto-discovery —
# there the bro's skills reach the agent through its `skill` MCP tool, so the
# symlinks would be dead weight; skip the populate. must run after venv
# activation (needs `cw` on PATH).
case " $* " in
  *" --bare "*) bare_session=1 ;;
  *) bare_session=0 ;;
esac
if [ -n "${CW_BRO:-}" ] && [ "${CW_SKIP_VENV:-}" != "1" ] && [ "$bare_session" = "0" ]; then
  cw populate-bro-skills "$CW_BRO" >&2
fi

exec "$@"
