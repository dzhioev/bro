from bro.launch.identity import bro_git_identity_env


def test_identity_derives_from_the_bro_name():
  assert bro_git_identity_env('dev') == {
    'GIT_AUTHOR_NAME': 'dev',
    'GIT_AUTHOR_EMAIL': 'dev@bro',
    'GIT_COMMITTER_NAME': 'dev',
    'GIT_COMMITTER_EMAIL': 'dev@bro',
  }
