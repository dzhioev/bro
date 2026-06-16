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
  # the clone copied /host-repo's local master into refs/remotes/origin/master,
  # which is often behind the host's freshly-fetched origin/master. pull the
  # fresh ref through the local `host` remote — no token needed, and objects
  # are already shared via alternates so this is ref-only.
  git fetch host '+refs/remotes/origin/master:refs/remotes/origin/master' >&2
  # -B resets if a stale worktree-<CW_NAME> branch came through with the clone.
  git checkout -B "worktree-$CW_NAME" origin/master >&2
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

if [ ! -x .venv/bin/python ] && [ "${CW_SKIP_VENV:-}" != "1" ]; then
  echo 'provisioning linux venv' >&2
  uv sync --all-groups >&2
fi

# activate venv so all child processes (hooks, MCP servers, Bash tool) inherit it
if [ "${CW_SKIP_VENV:-}" != "1" ]; then
  source /workspace/.venv/bin/activate
fi

# secrets resolve from the scoped credential store bind-mounted at ~/.ppp (see
# cw.py). wire each into the tool that consumes it from outside the resolver (git
# credential helper, AWS_SHARED_CREDENTIALS_FILE, ...) via its registry-declared
# install hook — one generic step, no per-secret logic here. env exports must
# persist into `exec`, so this is eval'd in the entrypoint shell.
if [ "${CW_SKIP_VENV:-}" != "1" ]; then
  eval "$(credentials install-hooks)"
fi

# in --bro mode, surface the bro's skills to Claude Code by symlinking them into
# .claude/skills/; --bare keeps slash-command resolution working so /skill-name
# from chat picks them up. must run after venv activation (needs `cw` on PATH).
if [ -n "${CW_BRO:-}" ] && [ "${CW_SKIP_VENV:-}" != "1" ]; then
  cw populate-bro-skills "$CW_BRO" >&2
fi

exec "$@"
