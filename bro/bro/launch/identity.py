"""the git identity agent sessions commit as.

Every managed session — claude code sessions and native bro runs alike — commits
as the bro identity, so agent commits are attributed to bro, not the user.
"""

from typing import Optional

from base import credentials

_BRO_GIT_NAME = 'bro'
_LEGACY_BRO_GIT_EMAIL = 'dzhioev+bro@gmail.com'


def _minted_github_git_email(store: credentials.Store) -> Optional[str]:
  for source in store.sources('github'):
    if not isinstance(source, credentials.MintingSource):
      continue
    config = source.config()
    if config is None:
      continue
    email = config.get('git_email')
    if not isinstance(email, str):
      raise ValueError(
        f'{source.TYPE} config {source.file!r} carries no git_email — add the app '
        "bot's address, <bot-user-id>+<slug>[bot]@users.noreply.github.com"
      )
    return email
  return None


def bro_git_identity_env(store: Optional[credentials.Store] = None) -> dict[str, str]:
  """the GIT_AUTHOR/COMMITTER_* environment carrying the bro git identity: the
  app bot's address (`git_email` of the minting config backing the `github`
  credential) when the store carries one, the legacy address otherwise."""
  if store is None:
    store = credentials.default_store()
  email = _minted_github_git_email(store)
  return {
    'GIT_AUTHOR_NAME': _BRO_GIT_NAME,
    'GIT_AUTHOR_EMAIL': email if email is not None else _LEGACY_BRO_GIT_EMAIL,
    'GIT_COMMITTER_NAME': _BRO_GIT_NAME,
    'GIT_COMMITTER_EMAIL': email if email is not None else _LEGACY_BRO_GIT_EMAIL,
  }
