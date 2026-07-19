from bro.launch.identity import bro_git_identity_env


def test_identity_derives_from_the_bro_name():
  assert bro_git_identity_env('ppp-dev') == {
    'GIT_AUTHOR_NAME': 'bro',
    'GIT_AUTHOR_EMAIL': 'ppp-dev@bro',
    'GIT_COMMITTER_NAME': 'bro',
    'GIT_COMMITTER_EMAIL': 'ppp-dev@bro',
  }
