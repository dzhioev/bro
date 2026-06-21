#!/usr/bin/env -S bash -e

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

"$DIR/provision_repo.sh"

if [ -d "$HOME/.ppp" ]; then
  echo "secret store ~/.ppp OK"
else
  echo "warning: ~/.ppp not found; credentials will not be available (stow dot-ppp)" >&2
fi

echo "repo setup complete"
