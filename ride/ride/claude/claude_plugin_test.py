import json

from ride.claude import claude_plugin


def test_a_session_lays_a_fresh_copy_of_the_plugin(tmp_path):
  stale = tmp_path / 'plugin' / 'hooks' / 'removed.ts'
  stale.parent.mkdir(parents=True)
  stale.write_text('')

  plugin = claude_plugin.provision(tmp_path)

  assert plugin.parent == tmp_path
  assert not stale.exists()
  manifest = json.loads((plugin / '.claude-plugin' / 'plugin.json').read_text())
  assert (plugin / manifest['types']).is_file()
  hooks = json.loads((plugin / 'hooks' / 'hooks.json').read_text())
  assert len(hooks['modules']) > 0
  assert all((plugin / 'hooks' / module).is_file() for module in hooks['modules'])
