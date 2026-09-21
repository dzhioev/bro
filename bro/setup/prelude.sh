#!/usr/bin/env bash
# the shell-script prelude, sourced first by every executable script: leveled
# logging (log.sh), fail-fast guards (strict.sh), and HERE, the physical path
# of the executed script's directory

_PRELUDE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "$_PRELUDE_DIR/log.sh"
source "$_PRELUDE_DIR/strict.sh"
unset _PRELUDE_DIR

# the outermost frame: the executed script, however deeply this file is sourced
HERE="$(cd "$(dirname "${BASH_SOURCE[${#BASH_SOURCE[@]}-1]}")" && pwd -P)"
