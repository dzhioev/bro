# the claude model every `cw ss` session runs (injected via --model by the argv
# builder). its own module so consumers share it without a heavier import.
_CW_MODEL = 'claude-opus-5'

CW_RESUMED_SESSION_ENV = 'CW_RESUMED_SESSION'
