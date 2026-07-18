#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../prelude.sh"

SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source "$SCRIPT_DIR/../versions.sh"

STOW_DEB="stow_${STOW_DEB_VERSION}_all.deb"
MIRROR="http://mirrors.kernel.org/ubuntu/pool/universe/s/stow"

DOWNLOAD_DIR="${SCRIPT_DIR}/.stow_download"

echo "Installing stow ${STOW_DEB_VERSION} from Ubuntu repository..."

rm -rf "$DOWNLOAD_DIR"
mkdir -p "$DOWNLOAD_DIR"
cd "$DOWNLOAD_DIR"

wget "${MIRROR}/${STOW_DEB}"
sudo dpkg -i "$STOW_DEB"

cd "$SCRIPT_DIR"
rm -rf "$DOWNLOAD_DIR"

echo "stow ${STOW_DEB_VERSION} installed successfully"
