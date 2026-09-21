from pathlib import Path

from bro.base.suite_environment import rebuild_environment
from bro.dev.packaging_policy import assert_packaging_policy
from bro.local.run_tests import BENCHMARK

_REBUILD_MODULE_PATH = rebuild_environment.__module__.replace('.', '/') + '.py'
_WEBVIEW_MODULE_PATHS = (
  'bro/webview/cli.py',
  'bro/webview/serve.py',
  'bro/webview/worker.py',
  'bro/webview/container/Dockerfile',
)


def test_repository_packaging_policy():
  assert_packaging_policy(
    Path(__file__).resolve().parents[3],
    siblings=(BENCHMARK,),
    required_modules=(_REBUILD_MODULE_PATH, *_WEBVIEW_MODULE_PATHS),
  )
