container_git_url() {
  local url="$1"
  printf '%s\n' "$url" | sed 's|^git@github\.com:|https://github.com/|'
}

initialize_container_submodules() {
  local workspace="$1"
  local host_repository="$2"
  local quiet=(-q)
  if log_enabled VERBOSE; then quiet=(); fi

  if [ ! -f "$workspace/.gitmodules" ]; then
    return
  fi

  git -C "$workspace" config -f .gitmodules --get-regexp '^submodule\..*\.path$' \
    | while IFS=' ' read -r key path; do
        local name="${key#submodule.}"
        name="${name%.path}"
        if [ ! -e "$host_repository/$path/.git" ]; then
          if [ "$(git -C "$host_repository" rev-parse --is-bare-repository)" != "true" ]; then
            log VERBOSE "skipping submodule $name: $host_repository/$path not initialized on host"
            continue
          fi
          local mirror_upstream
          mirror_upstream="$(git -C "$workspace" config -f .gitmodules --get "submodule.$name.url")"
          mirror_upstream="$(container_git_url "$mirror_upstream")"
          log VERBOSE "initializing submodule $name from $mirror_upstream"
          git -C "$workspace" \
              -c "submodule.$name.url=$mirror_upstream" \
              submodule "${quiet[@]}" update --init -- "$path" >&2
          continue
        fi

        log VERBOSE "initializing submodule $name from $host_repository/$path"
        git -C "$workspace" \
            -c "submodule.$name.url=$host_repository/$path" \
            -c protocol.file.allow=always \
            submodule "${quiet[@]}" update --init -- "$path" >&2

        local upstream_url
        upstream_url="$(git -C "$host_repository/$path" config --get remote.origin.url)"
        upstream_url="$(container_git_url "$upstream_url")"
        git -C "$workspace/$path" remote set-url origin "$upstream_url"
        git -C "$workspace/$path" remote add host "$host_repository/$path"
      done
}
