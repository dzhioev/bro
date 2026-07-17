#!/usr/bin/env -S bash -e

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source "$DIR/log.sh"

"$DIR/provision_repo.sh"

if [ -d "$HOME/.ppp" ]; then
  log INFO "secret store ~/.ppp OK"
else
  log WARNING "~/.ppp not found; credentials will not be available (stow dot-ppp)"
fi

log INFO "repo setup complete"
