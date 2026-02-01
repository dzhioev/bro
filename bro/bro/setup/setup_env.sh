#!/bin/bash -e

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

echo "Setting up dev environment on ${PLATFORM}"
