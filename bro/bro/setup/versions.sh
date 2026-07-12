#!/usr/bin/env bash
# pinned versions of the system tools managed by setup_env.sh — the single source
# of truth shared by its version checks and the ubuntu/ installers' build targets.
# Bumping a pin here makes the next setup_env.sh run upgrade existing machines.

TMUX_VERSION="3.7b"

# full ubuntu deb pin; the part before '-' is the stow release used for version checks
STOW_DEB_VERSION="2.4.1-2"
STOW_VERSION="${STOW_DEB_VERSION%%-*}"
