#!/usr/bin/env bash
# sourceable fail-fast guards for bash scripts; requires log.sh to be sourced
# first. refuses to continue when errexit is off, and aborts the script on
# command-not-found even in the positions where -e is suppressed (if/while
# conditions, && / || chains) and bash would otherwise read the typo as false.

case $- in
  *e*) ;;
  *)
    log ERROR "errexit is off; use the '#!/usr/bin/env -S bash -e' shebang"
    exit 1
    ;;
esac

command_not_found_handle() {
  log ERROR "command not found: $1"
  # the handler runs in a forked child, where exit only sets the failed
  # command's status; killing the script itself is what aborts it
  kill -s TERM $$
  exit 127
}
