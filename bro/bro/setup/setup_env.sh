#!/usr/bin/env -S bash -e

version_gte() {
  python -c "exit(0 if tuple(map(int, '$1'.split('.'))) >= tuple(map(int, '$2'.split('.'))) else 1)"
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
STOW_MIN_VERSION="2.4.0"
TMUX_MIN_VERSION="3.5"

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
  STOW_VERSION=$(stow --version | head -n1 | awk '{print $NF}')
  if ! version_gte "$STOW_VERSION" "$STOW_MIN_VERSION"; then
    echo "stow version $STOW_VERSION is lower than required $STOW_MIN_VERSION"
    return 1
  fi
  echo "stow version: $STOW_VERSION"
  return 0
}

install_stow() {
  if check_stow_version; then
    return
  fi

  echo "Installing stow..."
  if [ "$PLATFORM" = "macOS" ]; then
    check_brew
    brew install stow
  else
    "$SCRIPT_DIR/ubuntu/install_stow.sh"
  fi
}

check_tmux_version() {
  if ! command -v tmux &> /dev/null; then
    return 1
  fi
  # strip the patch-letter suffix (3.5a -> 3.5) for the numeric comparison
  TMUX_VERSION=$(tmux -V | awk '{print $2}' | sed 's/[a-z]*$//')
  if ! version_gte "$TMUX_VERSION" "$TMUX_MIN_VERSION"; then
    echo "tmux version $TMUX_VERSION is lower than required $TMUX_MIN_VERSION"
    return 1
  fi
  echo "tmux version: $TMUX_VERSION"
  return 0
}

install_tmux() {
  if check_tmux_version; then
    return
  fi

  echo "Installing tmux"
  if [ "$PLATFORM" = "macOS" ]; then
    check_brew
    brew install tmux
  else
    "$SCRIPT_DIR/ubuntu/install_tmux.sh"
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
