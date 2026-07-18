#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/prelude.sh"

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

echo "Setting up dev environment on ${PLATFORM}"

SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source "$SCRIPT_DIR/versions.sh"

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

install_docker() {
  if [ "$PLATFORM" != "macOS" ]; then
    return
  fi

  if command -v docker &> /dev/null && docker info &> /dev/null; then
    echo "Docker is already installed and running"
    return
  fi

  check_brew
  echo "Installing Docker (via Colima)..."
  brew install colima docker docker-buildx
  brew services start colima 2>/dev/null || colima start

  # Configure Docker to find Homebrew plugins
  mkdir -p ~/.docker
  if [ ! -f ~/.docker/config.json ]; then
    echo '{}' > ~/.docker/config.json
  fi
  if ! grep -q cliPluginsExtraDirs ~/.docker/config.json; then
    python3 -c "
import json
with open('$HOME/.docker/config.json', 'r') as f:
    config = json.load(f)
config['cliPluginsExtraDirs'] = ['/opt/homebrew/lib/docker/cli-plugins']
with open('$HOME/.docker/config.json', 'w') as f:
    json.dump(config, f, indent=2)
"
  fi
}

install_awscli() {
  if command -v aws &> /dev/null; then
    echo "AWS CLI is already installed"
    return
  fi

  if [ "$PLATFORM" = "macOS" ]; then
    check_brew
    echo "Installing AWS CLI..."
    brew install awscli
  fi
}

install_uv() {
  if command -v uv &> /dev/null; then
    echo "uv is already installed: $(uv --version)"
    return
  fi

  echo "Installing uv..."
  if [ "$PLATFORM" = "macOS" ]; then
    check_brew
    brew install uv
  else
    # Ubuntu: install via pipx for an isolated, easily-uninstallable install
    # (uv is published as a PyPI wheel; pipx puts it in a managed venv)
    if ! command -v pipx &> /dev/null; then
      sudo apt-get update
      sudo apt-get install -y pipx
      pipx ensurepath
    fi
    pipx install uv
  fi
}

install_stow
install_tmux
install_claude_code
install_docker
install_awscli
install_uv
