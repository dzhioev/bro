#!/usr/bin/env -S bash -e

TMUX_VERSION="3.5a"
TMUX_TARBALL="tmux-${TMUX_VERSION}.tar.gz"
TMUX_URL="https://github.com/tmux/tmux/releases/download/${TMUX_VERSION}/${TMUX_TARBALL}"

SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
BUILD_DIR="${SCRIPT_DIR}/.tmux_build"
PREFIX="${HOME}/.local"

echo "Installing tmux ${TMUX_VERSION} from source into ${PREFIX}"

sudo apt-get update
sudo apt-get install -y build-essential pkg-config libevent-dev libncurses-dev bison

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

wget "$TMUX_URL"
tar xf "$TMUX_TARBALL"
cd "tmux-${TMUX_VERSION}"
./configure --prefix="$PREFIX"
make -j"$(nproc)"
make install

cd "$SCRIPT_DIR"
rm -rf "$BUILD_DIR"

echo "tmux ${TMUX_VERSION} installed to ${PREFIX}/bin/tmux"
if [[ ":$PATH:" != *":${PREFIX}/bin:"* ]]; then
  echo "warning: ${PREFIX}/bin is not on PATH; tmux will not be found" >&2
fi
if tmux list-sessions &> /dev/null; then
  echo "note: a tmux server is still running on the old binary; run 'tmux kill-server' (ends all sessions) to pick up the new version" >&2
fi
