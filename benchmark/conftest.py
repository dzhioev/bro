import pytest

from bro.base.suite_environment import rebuild_environment

rebuild_environment()

collect_ignore = ['bro/benchmark/harbor_e2e_test.py', 'bro/benchmark/benchmark_job_e2e_test.py']


@pytest.fixture(autouse=True)
def installation_environment(monkeypatch, tmp_path):
  from bro.workspace import installation

  environment = tmp_path.parent / f'{tmp_path.name}-installation'
  environment.mkdir()
  (environment / installation.MARKER).write_text('test-home\n')
  monkeypatch.setattr(installation, 'ENVIRONMENT', environment)
  return environment
