from bro import shell


def test_shell_dir_contains_framework_shell_files():
  assert shell.shell_dir().is_dir()


def test_main_prints_shell_dir(capsys):
  assert shell.main(['bro-shell-dir']) is None
  assert capsys.readouterr().out == f'{shell.shell_dir()}\n'
