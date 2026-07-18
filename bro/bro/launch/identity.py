"""the git identity agent sessions commit as.

Every managed session — claude code sessions and native bro runs alike — commits
as the bro identity, so agent commits are attributed to bro, not the user; the
container pre-push hook fences this identity from pushing to master/main.
"""

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
