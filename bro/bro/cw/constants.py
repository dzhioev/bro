# the claude model every `cw ss` session runs (native sessions inject it via
# --model; --bro builds it into the bare argv). its own module so bro.py and
# session.py both import it without pulling in a heavier dependency.
_CW_MODEL = 'claude-fable-5'
