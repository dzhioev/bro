import json
from pathlib import Path
from types import SimpleNamespace

import duckdb

from bro.benchmark import query


def _manifest(format: int, *, job_id: str, source_commit: str, trial: str) -> dict[str, object]:
  return {
    'format': format,
    'job': {
      'id': job_id,
      'started_at': '2026-09-11T01:00:00+00:00',
      'finished_at': '2026-09-11T02:00:00+00:00',
      'n_retries': 1,
    },
    'config': {'datasets': [{'name': 'terminal-bench'}]},
    'score_config_sha256': 'sha256:' + 'a' * 64,
    'roster_sha256': 'sha256:' + 'b' * 64,
    'dataset': {'name': 'terminal-bench', 'ref': 'sha256:dataset'},
    'bundle': {
      'source_commit': source_commit,
      'framework_revision': 'sha256:' + 'c' * 64,
    },
    'total_cost_usd': 1.25,
    'trials': [
      {
        'trial': trial,
        'task': 'package-task',
        'harness': 'bro',
        'bro': 'terminal',
        'model': 'openai/gpt',
        'llm': {'type': 'openai', 'model': 'gpt'},
        'rewards': {'reward': 1, 'secondary': 0.5},
        'reward': 1,
        'error': None,
        'started_at': '2026-09-11T01:10:00+00:00',
        'finished_at': '2026-09-11T01:20:00+00:00',
        'root_trail_id': 'trail-id',
        'llm_calls': 2,
        'tokens': {'input': 10, 'cache_write': 0, 'cache_read': 5, 'output': 3},
        'cost_usd': 1.25,
      }
    ],
  }


def _write_manifest(root: Path, date: str, job: str, manifest: dict[str, object]) -> Path:
  path = root / date / job / 'retention.json'
  path.parent.mkdir(parents=True)
  path.write_text(json.dumps(manifest))
  return path


def test_views_project_format_three_runs_and_trial_stores(tmp_path):
  current_path = _write_manifest(
    tmp_path,
    '2026-09-11',
    'current',
    _manifest(3, job_id='current-job', source_commit='candidate', trial='trial-current'),
  )
  _write_manifest(
    tmp_path,
    '2026-08-24',
    'legacy',
    _manifest(2, job_id='legacy-job', source_commit='baseline', trial='trial-legacy'),
  )

  with duckdb.connect() as connection:
    query.define_views(connection, str(tmp_path / '*' / '*' / 'retention.json'))

    assert connection.sql(
      'SELECT job_id, source_commit, dataset_name, total_cost_usd FROM runs'
    ).fetchall() == [('current-job', 'candidate', 'terminal-bench', 1.25)]
    assert connection.sql(
      'SELECT task, rewards.reward, tokens.cache_read, store_prefix FROM trials'
    ).fetchall() == [
      (
        'package-task',
        '1',
        5,
        str(current_path.parent / 'trial-current' / 'agent' / 'ride' / 'trails') + '/',
      )
    ]


def test_query_output_includes_every_result_row(capsys):
  with duckdb.connect() as connection:
    query._execute(connection, 'SELECT range AS value FROM range(30)')

  output = capsys.readouterr().out
  assert '│    10 │' in output
  assert '│    19 │' in output
  assert '30 rows (20 shown)' not in output


def test_database_loads_s3_extensions_and_credential_chain(monkeypatch):
  class Connection:
    def __init__(self):
      self.statements = []
      self.closed = False

    def execute(self, statement):
      self.statements.append(statement)

    def close(self):
      self.closed = True

  connection = Connection()
  defined = {}
  monkeypatch.setattr(query.duckdb, 'connect', lambda: connection)
  monkeypatch.setattr(
    query.retention,
    'configured_retention',
    lambda: SimpleNamespace(bucket='benchmark-runs', region="region'one"),
  )
  monkeypatch.setattr(
    query,
    'define_views',
    lambda active_connection, source: defined.update(
      connection=active_connection,
      source=source,
    ),
  )

  with query.database() as opened:
    assert opened is connection

  assert connection.statements == [
    'INSTALL httpfs',
    'LOAD httpfs',
    'INSTALL aws',
    'LOAD aws',
    "CREATE SECRET (TYPE s3, PROVIDER credential_chain, REGION 'region''one')",
  ]
  assert defined == {
    'connection': connection,
    'source': 's3://benchmark-runs/runs/*/*/retention.json',
  }
  assert connection.closed
