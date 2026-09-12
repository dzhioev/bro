#!/usr/bin/env -S bash -e
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/prelude.sh"
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# a container's environment comes baked into its image
if [ -f /.dockerenv ]; then
  log VERBOSE "inside a container; skipping environment setup"
  exit 0
fi

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *)
      echo "usage: $0 [--force]" >&2
      exit 2
      ;;
  esac
done

# profile: a checkout vendoring the framework as a submodule needs only the tools ride
# operates with; framework development itself needs the full set
if [ -n "$(git -C "$SCRIPT_DIR/../.." rev-parse --show-superproject-working-tree)" ]; then
  PROFILE=core
else
  PROFILE=full
fi

# skip when nothing that defines the environment changed since the last
# successful run on this host: the stamp records a hash of this script and the
# pinned versions it enforces. system drift behind an unchanged stamp is not
# re-checked — run with --force to re-verify the installed tools.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/bro"
STAMP="$STATE_DIR/setup-env-$PROFILE.stamp"
INPUTS_HASH="$(
  cat "$SCRIPT_DIR/setup_env.sh" "$SCRIPT_DIR/install_awscli.sh" "$SCRIPT_DIR/uv-version" \
    "$SCRIPT_DIR/versions.sh" "$SCRIPT_DIR"/ubuntu/*.sh \
    | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
)"
if [ "$FORCE" != "1" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$INPUTS_HASH" ]; then
  log VERBOSE "environment current; run with --force to re-verify"
  exit 0
fi

# compares versions of the form N.N[letter]... (3.7 < 3.7a < 3.7b < 3.8)
version_gte() {
  python3 - "$1" "$2" <<'EOF'
import re
import sys


def parse(version):
  components = []
  for component in version.split('.'):
    match = re.fullmatch(r'(\d+)([a-z]?)', component)
    if match is None:
      sys.exit(f'unparseable version: {version!r}')
    components.append((int(match.group(1)), match.group(2)))
  return components


sys.exit(0 if parse(sys.argv[1]) >= parse(sys.argv[2]) else 1)
EOF
}

detect_platform() {
  case "$(uname -s)" in
    Darwin)
      echo "macOS"
      ;;
    Linux)
      if [ -f /etc/os-release ]; then
        . /etc/os-release
        if [ "$ID" = "ubuntu" ]; then
          echo "Ubuntu"
        else
          echo "Linux"
        fi
      else
        echo "Linux"
      fi
      ;;
    *)
      echo "Unknown"
      ;;
  esac
}

PLATFORM=$(detect_platform)

if [ "$PLATFORM" != "macOS" ] && [ "$PLATFORM" != "Ubuntu" ]; then
  echo "Unsupported platform: ${PLATFORM}. Only macOS and Ubuntu are supported."
  exit 1
fi

echo "Setting up dev environment on ${PLATFORM} ($PROFILE profile)"

source "$SCRIPT_DIR/versions.sh"
UV_VERSION="$(cat "$SCRIPT_DIR/uv-version")"

check_brew() {
  if ! command -v brew &> /dev/null; then
    echo "Homebrew is not installed. Please install it first:"
    echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    exit 1
  fi
}

check_stow_version() {
  if ! command -v stow &> /dev/null; then
    return 1
  fi
  local installed_version
  installed_version=$(stow --version | head -n1 | awk '{print $NF}')
  if ! version_gte "$installed_version" "$STOW_VERSION"; then
    echo "stow version $installed_version is older than pinned $STOW_VERSION"
    return 1
  fi
  echo "stow version: $installed_version"
  return 0
}

install_stow() {
  if check_stow_version; then
    return
  fi

  echo "Installing stow ${STOW_VERSION}"
  if [ "$PLATFORM" = "macOS" ]; then
    check_brew
    brew install stow
  else
    "$SCRIPT_DIR/ubuntu/install_stow.sh"
  fi
  if ! check_stow_version; then
    echo "stow is still older than pinned ${STOW_VERSION} after install" >&2
    exit 1
  fi
}

check_tmux_version() {
  if ! command -v tmux &> /dev/null; then
    return 1
  fi
  local installed_version
  installed_version=$(tmux -V | awk '{print $2}')
  if ! version_gte "$installed_version" "$TMUX_VERSION"; then
    echo "tmux version $installed_version is older than pinned $TMUX_VERSION"
    return 1
  fi
  echo "tmux version: $installed_version"
  return 0
}

install_tmux() {
  if check_tmux_version; then
    return
  fi

  echo "Installing tmux ${TMUX_VERSION}"
  if [ "$PLATFORM" = "macOS" ]; then
    check_brew
    brew install tmux
  else
    "$SCRIPT_DIR/ubuntu/install_tmux.sh"
  fi
  if ! check_tmux_version; then
    echo "tmux is still older than pinned ${TMUX_VERSION} after install" >&2
    exit 1
  fi
}

check_claude_code() {
  if ! command -v claude &> /dev/null; then
    echo "Claude Code CLI is not installed."
    return 1
  fi

  CLAUDE_PATH=$(command -v claude)

  # Check if it's a node script (npm-installed) rather than a native binary
  if head -1 "$CLAUDE_PATH" 2>/dev/null | grep -q "node"; then
    echo "Claude Code CLI is installed via npm, but native installation is required."
    return 1
  fi

  CLAUDE_VERSION=$(claude --version 2>/dev/null | head -n1)
  echo "Claude Code CLI: $CLAUDE_VERSION (native, $CLAUDE_PATH)"
  return 0
}

install_claude_code() {
  if check_claude_code; then
    return
  fi

  echo "Installing Claude Code CLI (native) via npx..."
  npx @anthropic-ai/claude-code install
}

# the compose plugin ships separately from the engine, and benchmark jobs drive
# every task container through it
check_docker_compose() {
  if docker compose version &> /dev/null; then
    echo "docker compose: $(docker compose version --short)"
    return
  fi
  echo "docker compose plugin missing; benchmark runs need it (Ubuntu: docker-compose-plugin)"
}

check_docker_cli() {
  if ! command -v docker &> /dev/null; then
    return 1
  fi
  echo "docker CLI: $(docker --version) ($(command -v docker))"
  return 0
}

install_docker_cli() {
  if check_docker_cli; then
    return
  fi

  check_brew
  echo "Installing the docker CLI..."
  brew install colima docker docker-buildx docker-compose
  # `brew install` is a no-op on a formula that is installed but unlinked, so a
  # docker sitting unlinked in the Cellar survives it and stays off PATH
  if ! command -v docker &> /dev/null; then
    echo "docker is installed but not linked; linking it"
    if ! brew link docker; then
      echo "cannot link docker; resolve the conflict brew reports above" >&2
      exit 1
    fi
  fi
  if ! check_docker_cli; then
    echo "docker is still not on PATH; check that $(brew --prefix)/bin is on it" >&2
    exit 1
  fi
}

start_docker_daemon() {
  if docker info &> /dev/null; then
    echo "docker daemon: answering"
    return
  fi
  if ! command -v colima &> /dev/null; then
    echo "no docker daemon answers and colima is not installed" >&2
    exit 1
  fi
  echo "Starting the docker daemon (via Colima)..."
  brew services start colima 2> /dev/null || colima start
}

# the brew-installed buildx and compose plugins live outside the directories the
# CLI searches by default
configure_docker_plugins() {
  if ! command -v brew &> /dev/null; then
    return
  fi
  mkdir -p ~/.docker
  if [ ! -f ~/.docker/config.json ]; then
    echo '{}' > ~/.docker/config.json
  fi
  if ! grep -q cliPluginsExtraDirs ~/.docker/config.json; then
    python3 -c "
import json
with open('$HOME/.docker/config.json', 'r') as f:
    config = json.load(f)
config['cliPluginsExtraDirs'] = ['$(brew --prefix)/lib/docker/cli-plugins']
with open('$HOME/.docker/config.json', 'w') as f:
    json.dump(config, f, indent=2)
"
  fi
}

# macOS ships no docker of its own: Homebrew provides the CLI and colima the VM
# running the daemon it talks to. The two go missing independently, so each is
# checked and installed on its own.
install_docker() {
  if [ "$PLATFORM" != "macOS" ]; then
    check_docker_compose
    return
  fi

  install_docker_cli
  start_docker_daemon
  configure_docker_plugins
  check_docker_compose
}

install_tkinter() {
  if [ "$PLATFORM" != "macOS" ]; then
    return
  fi

  if python3 -c 'import tkinter' > /dev/null 2>&1; then
    echo "tkinter is already available"
    return
  fi
  check_brew
  brew install python-tk
}

install_uv() {
  local installed_version
  if command -v uv &> /dev/null; then
    installed_version="$(uv --version | awk '{print $2}')"
    if [ "$installed_version" = "$UV_VERSION" ]; then
      echo "uv is already installed: $(uv --version)"
      return
    fi
    echo "Updating uv from $installed_version to $UV_VERSION..."
  else
    echo "Installing uv $UV_VERSION..."
  fi

  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
  export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
  installed_version="$(uv --version | awk '{print $2}')"
  if [ "$installed_version" != "$UV_VERSION" ]; then
    echo "uv $UV_VERSION was installed, but PATH selects $installed_version" >&2
    return 1
  fi
}

install_claude_code
install_docker
install_uv
if [ "$PROFILE" = "full" ]; then
  install_stow
  install_tmux
  install_tkinter
  "$SCRIPT_DIR/install_awscli.sh"
fi

mkdir -p "$STATE_DIR"
printf '%s\n' "$INPUTS_HASH" > "$STAMP"
