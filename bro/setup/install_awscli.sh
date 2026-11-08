#!/usr/bin/env -S bash -e
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/prelude.sh"

if command -v aws &> /dev/null; then
  log INFO "AWS CLI is already installed: $(aws --version)"
  exit 0
fi

case "$(uname -s)" in
  Darwin)
    if ! command -v brew &> /dev/null; then
      log ERROR "Homebrew is required to install the AWS CLI on macOS"
      exit 1
    fi
    brew install awscli
    ;;
  Linux)
    privilege=()
    if [ "$(id -u)" -ne 0 ]; then
      if ! command -v sudo &> /dev/null; then
        log ERROR "installing the AWS CLI on Linux requires root or sudo"
        exit 1
      fi
      privilege=(sudo)
    fi

    temporary_directory="$(mktemp -d)"
    trap 'rm -rf "$temporary_directory"' EXIT
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" \
      -o "$temporary_directory/awscli.zip"
    unzip -q "$temporary_directory/awscli.zip" -d "$temporary_directory"
    "${privilege[@]}" "$temporary_directory/aws/install"
    ;;
  *)
    log ERROR "unsupported platform for AWS CLI installation: $(uname -s)"
    exit 1
    ;;
esac
