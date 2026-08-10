#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

# Remove the pre-unification workspace layout from a project's var/cw: the
# `worktrees/` and `containers/` namespaces and the parallel `lock/`, `exit/`,
# `resume/` and `log/` record dirs, plus the summon files left behind for
# workspaces that no longer exist. `workspaces/` and `broker/` are kept.
#
# Prints what it would remove and exits; `--apply` performs the removal. Runs
# against the host checkout — old-layout sessions hold their state here, so it
# refuses while any of them is still live.
#
# The per-session claude state under ~/.claude/cw-sessions is deliberately left
# alone: it holds session transcripts, which are worth more than the disk.

usage() {
  echo "usage: $(basename "$0") [--apply] [<project>]" >&2
  exit 2
}

APPLY=0
PROJECT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    -h | --help) usage ;;
    -*) usage ;;
    *)
      [ -n "$PROJECT" ] && usage
      PROJECT="$1"
      ;;
  esac
  shift
done

if [ -f /.dockerenv ]; then
  log ERROR 'run this on the host: a container sees the project read-only at /host-repo'
  exit 1
fi

if [ -z "$PROJECT" ]; then
  PROJECT="$(dirname "$(cd "$(git rev-parse --git-common-dir)" && pwd)")"
fi
CW="$PROJECT/var/cw"
if [ ! -d "$CW" ]; then
  log INFO "no $CW; nothing to clean"
  exit 0
fi

STALE_DIRS=()
for name in worktrees containers lock exit resume log; do
  [ -d "$CW/$name" ] && STALE_DIRS+=("$CW/$name")
done

STALE_SUMMONS=()
if [ -d "$CW/summon" ]; then
  for file in "$CW/summon"/*; do
    [ -e "$file" ] || continue
    name="$(basename "$file")"
    name="${name%.jsonl}"
    name="${name%.status.json}"
    [ -d "$CW/workspaces/$name" ] || STALE_SUMMONS+=("$file")
  done
fi

if [ ${#STALE_DIRS[@]} -eq 0 ] && [ ${#STALE_SUMMONS[@]} -eq 0 ]; then
  log INFO "$CW carries no pre-unification state"
  exit 0
fi

# a container bound to an old workspace mount is a session still running out of
# the tree we are about to delete
if [ -d "$CW/containers" ]; then
  BOUND="$(
    docker ps -q \
      | xargs -r docker inspect --format '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}' \
      | grep -F "$CW/containers/" || true
  )"
  if [ -n "$BOUND" ]; then
    log ERROR 'containers are still bound to old workspaces; finish those sessions first:'
    echo "$BOUND" >&2
    exit 1
  fi
fi

# an old-layout host session holds an exclusive flock on its lock file
if [ -d "$CW/lock" ]; then
  HELD=()
  for lock in "$CW/lock"/*; do
    [ -e "$lock" ] || continue
    flock -n "$lock" true 2>/dev/null || HELD+=("$(basename "$lock")")
  done
  if [ ${#HELD[@]} -gt 0 ]; then
    log ERROR "host sessions are still live on: ${HELD[*]}"
    exit 1
  fi
fi

WORKTREES=()
if [ -d "$CW/worktrees" ]; then
  while read -r line; do
    case "$line" in
      "worktree $CW/worktrees/"*) WORKTREES+=("${line#worktree }") ;;
    esac
  done < <(git -C "$PROJECT" worktree list --porcelain)
fi

if [ "$APPLY" -eq 0 ]; then
  log INFO 'dry run; pass --apply to remove'
  for worktree in "${WORKTREES[@]}"; do echo "would release worktree $worktree"; done
  for dir in "${STALE_DIRS[@]}"; do echo "would remove $dir"; done
  for file in "${STALE_SUMMONS[@]}"; do echo "would remove $file"; done
  exit 0
fi

# through git, so the .git/worktrees admin entry goes with the tree
for worktree in "${WORKTREES[@]}"; do
  log INFO "releasing worktree $worktree"
  git -C "$PROJECT" worktree remove --force "$worktree"
done
[ ${#WORKTREES[@]} -gt 0 ] && git -C "$PROJECT" worktree prune

for dir in "${STALE_DIRS[@]}"; do
  log INFO "removing $dir"
  # container processes can leave files owned by an in-container uid that the
  # host user cannot unlink; sudo is the escalation, as the removal path in
  # bro/workspace/model.py does with a root container
  rm -rf "$dir" || {
    log ERROR "cannot remove $dir — it holds files owned by another uid; retry with sudo"
    exit 1
  }
done

for file in "${STALE_SUMMONS[@]}"; do
  log INFO "removing $file"
  rm -f "$file"
done

log INFO 'done; local worktree-* branches and ~/.claude/cw-sessions are untouched'
