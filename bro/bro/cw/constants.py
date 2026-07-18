# the claude model every `cw ss` session runs (injected via --model by the argv
# builder). its own module so consumers share it without a heavier import.
_CW_MODEL = 'claude-fable-5'
