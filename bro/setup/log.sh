#!/usr/bin/env bash
# sourceable logging for shell scripts, matching base/log.py's line shape:
#
#   2026-07-17T17:45:10 INFO[scope] message
#
# on stderr, with the executed script's file stem as the scope. Messages below
# the BRO_LOG_LEVEL threshold (base/log.py owns the variable; INFO when unset)
# are dropped; log_enabled exposes the same gate as a predicate, for quieting a
# subprocess's own output at higher thresholds.

# the outermost frame: the executed script, however deeply this file is sourced
_LOG_SCOPE="$(basename "${BASH_SOURCE[${#BASH_SOURCE[@]}-1]}")"
_LOG_SCOPE="${_LOG_SCOPE%.sh}"

_log_level_number() {
  case "$1" in
    DEBUG) echo 10 ;;
    VERBOSE) echo 15 ;;
    INFO) echo 20 ;;
    WARNING) echo 30 ;;
    ERROR) echo 40 ;;
    *)
      echo "unknown log level: $1" >&2
      return 1
      ;;
  esac
}

# resolved once at source time, so an invalid threshold aborts the sourcing
# script instead of silently dropping every message
_LOG_THRESHOLD="$(_log_level_number "${BRO_LOG_LEVEL:-INFO}")"

log_enabled() {
  [ "$(_log_level_number "$1")" -ge "$_LOG_THRESHOLD" ]
}

log() {
  local level="$1"
  shift
  if log_enabled "$level"; then
    echo "$(date '+%Y-%m-%dT%H:%M:%S') $level[$_LOG_SCOPE] $*" >&2
  fi
}
