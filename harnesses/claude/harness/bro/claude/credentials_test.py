from bro.base import credentials


def test_installed_claude_credential_hook_exports_the_selected_token(tmp_path):
  registry = credentials.default_registry()
  store_dir = tmp_path / 'store'
  material = store_dir / credentials.MATERIAL_DIR
  material.mkdir(parents=True)
  (material / f'claude_code+work{credentials.MATERIAL_SUFFIX}').write_text('session-token')
  store = credentials.Store(registry, store_dir, {'claude_code': 'work'})

  exported = credentials.install_hooks(
    registry, {'claude_code'}, store, tmp_path / 'environment', {}
  )

  assert exported['CLAUDE_CODE_OAUTH_TOKEN'] == 'session-token'
