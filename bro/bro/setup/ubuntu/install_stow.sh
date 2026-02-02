#!/usr/bin/env -S bash -e

STOW_VERSION="2.4.1-2"
STOW_DEB="stow_${STOW_VERSION}_all.deb"
MIRROR="http://mirrors.kernel.org/ubuntu/pool/universe/s/stow"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOWNLOAD_DIR="${SCRIPT_DIR}/.stow_download"

echo "Installing stow ${STOW_VERSION} from Ubuntu repository..."

rm -rf "$DOWNLOAD_DIR"
mkdir -p "$DOWNLOAD_DIR"
cd "$DOWNLOAD_DIR"

wget "${MIRROR}/${STOW_DEB}"
sudo dpkg -i "$STOW_DEB"

cd "$SCRIPT_DIR"
rm -rf "$DOWNLOAD_DIR"

echo "stow ${STOW_VERSION} installed successfully"
