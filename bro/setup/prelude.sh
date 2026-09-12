#!/usr/bin/env bash
# the shell-script prelude, sourced first by every executable script: leveled
# logging (log.sh) + fail-fast guards (strict.sh)

_PRELUDE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "$_PRELUDE_DIR/log.sh"
source "$_PRELUDE_DIR/strict.sh"
unset _PRELUDE_DIR
