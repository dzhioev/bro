# the claude model every `cw ss` session runs (injected via --model by the argv
# builder). its own module so consumers share it without a heavier import.
_CW_MODEL = 'claude-fable-5'

# the git identity every cw-launched session commits as — agent commits are
# attributed to bro, not the user; the container pre-push hook fences this
# identity from pushing to master/main.
_BRO_GIT_NAME = 'bro'
_BRO_GIT_EMAIL = 'dzhioev+bro@gmail.com'


def bro_git_identity_env() -> dict[str, str]:
  """the GIT_AUTHOR/COMMITTER_* environment carrying the bro git identity."""
  return {
    'GIT_AUTHOR_NAME': _BRO_GIT_NAME,
    'GIT_AUTHOR_EMAIL': _BRO_GIT_EMAIL,
    'GIT_COMMITTER_NAME': _BRO_GIT_NAME,
    'GIT_COMMITTER_EMAIL': _BRO_GIT_EMAIL,
  }
