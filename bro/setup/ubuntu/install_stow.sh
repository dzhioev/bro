#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../prelude.sh"

source "$HERE/../versions.sh"

STOW_DEB="stow_${STOW_DEB_VERSION}_all.deb"
MIRROR="http://mirrors.kernel.org/ubuntu/pool/universe/s/stow"

DOWNLOAD_DIR="${HERE}/.stow_download"

echo "Installing stow ${STOW_DEB_VERSION} from Ubuntu repository..."

rm -rf "$DOWNLOAD_DIR"
mkdir -p "$DOWNLOAD_DIR"
cd "$DOWNLOAD_DIR"

wget "${MIRROR}/${STOW_DEB}"
sudo dpkg -i "$STOW_DEB"

cd "$HERE"
rm -rf "$DOWNLOAD_DIR"

echo "stow ${STOW_DEB_VERSION} installed successfully"
