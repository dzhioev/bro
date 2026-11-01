from pathlib import Path

from bro.base.suite_environment import rebuild_environment
from bro.dev.packaging_policy import assert_packaging_policy
from bro.local.run_tests import PROJECTS

_REBUILD_MODULE_PATH = rebuild_environment.__module__.replace('.', '/') + '.py'
_WEBVIEW_MODULE_PATHS = (
  'bro/webview/cli.py',
  'bro/webview/serve.py',
  'bro/webview/worker.py',
  'bro/webview/container/Dockerfile',
)
_PERMISSION_REFERENCE_PATHS = (
  'bro/reference/permission_model.md',
  'bro/reference/permissions.md',
  'bro/reference/permissions_cheatsheet.md',
)
_CLAUDE_PLUGIN_PATHS = (
  'bro/claude/plugin/.claude-plugin/plugin.json',
  'bro/claude/plugin/hooks/hooks.json',
  'bro/claude/plugin/hooks/register.ts',
  'bro/claude/plugin/types/index.d.ts',
)


def test_repository_packaging_policy():
  assert_packaging_policy(
    Path(__file__).resolve().parents[3],
    siblings=[project.directory for project in PROJECTS],
    required_modules=(
      _REBUILD_MODULE_PATH,
      'bro/local/bro-settings.toml',
      'bro/pdf/manual.md',
      'bro/reference/llm_providers.md',
      'bro/reference/mu.md',
      *_WEBVIEW_MODULE_PATHS,
      *_CLAUDE_PLUGIN_PATHS,
      *_PERMISSION_REFERENCE_PATHS,
    ),
  )
