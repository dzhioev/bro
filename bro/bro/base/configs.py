import os

DEFAULT_CONFIGS_DIR = os.path.join(os.path.dirname(__file__), '.configs')

# bumped whenever the bro framework changes in a way trails consumers care about
# (schema additions, semantics shifts, kind enum extensions). recorded on each
# trail header so an offline reader can tell which framework revision produced it.
VERSION = 1
