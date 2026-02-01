#!/bin/bash -e

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

install_stow
