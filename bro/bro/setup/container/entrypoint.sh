#!/usr/bin/env -S bash -e

# root phase: align container user with host uid/gid, then re-exec as cw
if [ "$(id -u)" = "0" ]; then
  TARGET_UID="$(stat -c '%u' /workspace)"
  TARGET_GID="$(stat -c '%g' /workspace)"
  if [ "$(id -u cw)" != "$TARGET_UID" ] || [ "$(id -g cw)" != "$TARGET_GID" ]; then
    groupmod -o -g "$TARGET_GID" cw
    usermod -o -u "$TARGET_UID" -g "$TARGET_GID" cw
    chown cw:cw /home/cw /home/cw/.claude
  fi
  exec gosu cw "$0" "$@"
fi

# --- running as cw from here ---

# seed per-worktree ~/.claude/ from host on first run; skip sensitive transcript
# data so prior sessions from other repos don't leak into this container
if [ ! -f "$HOME/.claude/settings.json" ] && [ -d /host-claude ]; then
  echo 'seeding ~/.claude from host' >&2
  find /host-claude -mindepth 1 -maxdepth 1 \
    ! -name sessions ! -name projects ! -name history.jsonl ! -name cw-sessions \
    -exec cp -rn {} "$HOME/.claude/" \;
fi

# first-run clone: /workspace starts empty; host repo is bind-mounted at /host-repo
# read-only. clone --shared reuses /host-repo/.git/objects via alternates so there's
# no disk duplication. origin is retargeted to the host's upstream (so `git push`
# goes to GitHub, matching host-mode worktrees), and we add `host` as a local
# remote for fetching commits that haven't been pushed upstream yet.
if [ ! -d /workspace/.git ]; then
  echo 'cloning host repo into /workspace' >&2
  cd /
  git -c protocol.file.allow=always clone --shared /host-repo /workspace >&2
  cd /workspace
  host_origin="$(git -C /host-repo config --get remote.origin.url)"
  git remote set-url origin "$host_origin"
  git remote add host /host-repo
  branch="worktree-$CW_NAME"
  if git show-ref --verify --quiet "refs/heads/$branch"; then
    git checkout "$branch" >&2
  else
    git checkout -b "$branch" >&2
  fi
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

# pre-create the project directory so Claude Code considers /workspace trusted
mkdir -p "$HOME/.claude/projects/-workspace"

if [ ! -x "$UV_PROJECT_ENVIRONMENT/bin/python" ]; then
  echo 'provisioning linux venv' >&2
  uv sync --all-groups >&2
fi

exec "$@"
