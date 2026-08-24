"""the git identities a managed session carries: the bro it commits as, and the
human it works for.

Every managed session — claude code sessions and native bro runs alike — commits
as the bro identity, so agent commits are attributed to the bro that made them,
not the user. The human rides beside it, resolved here on the host from the
repository the session attaches to.
"""

from typing import Optional

from bro.base import log
from bro.workspace.human import configured_human, human_env
from ride.repository import Repository


def bro_git_identity_env(bro_name: str) -> dict[str, str]:
  """the GIT_AUTHOR/COMMITTER_* environment carrying the bro git identity."""
  email = f'{bro_name}@bro'
  return {
    'GIT_AUTHOR_NAME': bro_name,
    'GIT_AUTHOR_EMAIL': email,
    'GIT_COMMITTER_NAME': bro_name,
    'GIT_COMMITTER_EMAIL': email,
  }


def human_git_identity_env(repository: Optional[Repository]) -> dict[str, str]:
  """the environment carrying the human a session works for: the identity
  `repository` is configured with. Empty when the launch attaches to no
  repository — one with no commits to credit — and when the repository declares
  no identity of its own."""
  if repository is None:
    return {}
  human = configured_human(repository.git_dir)
  if human is None:
    log.warning(
      'no user.name/user.email configured in %s; this session credits no human for its commits',
      repository.identity,
    )
    return {}
  return human_env(human)
