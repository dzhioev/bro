"""the git identity agent sessions commit as.

Every managed session — claude code sessions and native bro runs alike — commits
as the bro identity, so agent commits are attributed to the bro that made them,
not the user.
"""


def bro_git_identity_env(bro_name: str) -> dict[str, str]:
  """the GIT_AUTHOR/COMMITTER_* environment carrying the bro git identity."""
  email = f'{bro_name}@bro'
  return {
    'GIT_AUTHOR_NAME': bro_name,
    'GIT_AUTHOR_EMAIL': email,
    'GIT_COMMITTER_NAME': bro_name,
    'GIT_COMMITTER_EMAIL': email,
  }
