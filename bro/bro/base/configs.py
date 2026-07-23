import os

DEFAULT_CONFIGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.configs')

# the standalone host secret store. the credential resolver searches
# DEFAULT_CONFIGS_DIR (where the deployed services synthesize their configs) then
# this dir; host-side writable caches (gmail token, twitch token) write here
# directly.
DEFAULT_PPP_DIR = os.path.expanduser('~/.ppp')

# bumped whenever the bro framework changes in a way trails consumers care about
# (schema additions, semantics shifts, kind enum extensions). recorded on each
# trail header so an offline reader can tell which framework revision produced it.
VERSION = '2'
