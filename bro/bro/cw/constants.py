# the claude model every `cw ss` session runs (injected via --model by the argv
# builder). its own module so consumers share it without a heavier import.
_CW_MODEL = 'claude-fable-5'

# the git identity of autonomous (--auto) sessions; the container pre-push hook
# fences this identity from pushing to master/main.
_BRO_GIT_NAME = 'Bro'
_BRO_GIT_EMAIL = 'dzhioev+bro@gmail.com'
