from bro.oops import assets


def test_asset_directory_contains_deployment_files():
  assert assets.asset_directory().is_dir()


def test_main_prints_asset_directory(capsys):
  assert assets.main(['bro-oops-dir']) is None
  assert capsys.readouterr().out == f'{assets.asset_directory()}\n'
