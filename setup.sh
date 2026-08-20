#!/usr/bin/env -S bash -e
DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$DIR"

# RIDE_VENV_MANIFEST names a directory holding, at their repository-relative paths,
# the dependency manifests that the venv already linked into this tree was
# resolved from (the ride container entrypoint links the project dependency bake and
# exports it). The link outlives the match — a rebase across a dependency bump
# moves this tree's copies — so the comparison runs on every invocation.
manifests_match() {
  local staged compared=0
  [ -n "${RIDE_VENV_MANIFEST:-}" ] || return 1
  while IFS= read -r staged; do
    cmp -s "$staged" "$DIR/${staged#"$RIDE_VENV_MANIFEST"/}" || return 1
    compared=1
  done < <(find "$RIDE_VENV_MANIFEST" -type f)
  [ "$compared" = 1 ]
}

if ! manifests_match; then
  if [ -n "${RIDE_VENV_MANIFEST:-}" ]; then
    echo 'dependency manifests differ from the linked venv; syncing it' >&2
  fi
  unset VIRTUAL_ENV
  uv sync --all-packages --all-groups --all-extras
fi

source "$DIR/.venv/bin/activate"
bro.dev.install
