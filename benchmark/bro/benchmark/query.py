"""Query retained benchmark manifests with DuckDB."""

import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Optional

import duckdb

from bro.benchmark import retention

_MANIFEST_KEY = "regexp_replace(manifest.filename, '^s3://[^/]+/', '')"
_MANIFEST_COLUMNS = """{
  format: 'UINTEGER',
  job: 'STRUCT(id VARCHAR, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, n_retries UINTEGER)',
  config: 'JSON',
  score_config_sha256: 'VARCHAR',
  roster_sha256: 'VARCHAR',
  dataset: 'STRUCT(name VARCHAR, ref VARCHAR)',
  bundle: 'STRUCT(source_commit VARCHAR, framework_revision VARCHAR)',
  total_cost_usd: 'DOUBLE',
  trials: 'STRUCT(trial VARCHAR, task VARCHAR, harness VARCHAR, bro VARCHAR, model VARCHAR, llm JSON, rewards JSON, reward DOUBLE, error VARCHAR, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, root_trail_id VARCHAR, llm_calls UINTEGER, tokens STRUCT(input UBIGINT, cache_write UBIGINT, cache_read UBIGINT, output UBIGINT), cost_usd DOUBLE)[]'
}"""


def _sql_string(value: str) -> str:
  return "'" + value.replace("'", "''") + "'"


def _reader(source: str) -> str:
  return (
    f'read_json({_sql_string(source)}, '
    f'columns = {_MANIFEST_COLUMNS}, filename = true, union_by_name = false)'
  )


def define_views(connection: duckdb.DuckDBPyConnection, source: str) -> None:
  reader = _reader(source)
  connection.execute(
    f"""
    CREATE VIEW runs AS
    SELECT
      manifest.job.id AS job_id,
      manifest.job.started_at AS started_at,
      manifest.job.finished_at AS finished_at,
      manifest.job.n_retries AS n_retries,
      manifest.config AS config,
      manifest.bundle.source_commit AS source_commit,
      manifest.bundle.framework_revision AS framework_revision,
      manifest.dataset.name AS dataset_name,
      manifest.dataset.ref AS dataset_ref,
      manifest.score_config_sha256 AS score_config_sha256,
      manifest.roster_sha256 AS roster_sha256,
      manifest.total_cost_usd AS total_cost_usd,
      {_MANIFEST_KEY} AS manifest_key
    FROM {reader} AS manifest
    WHERE manifest.format = {retention.MANIFEST_FORMAT}
    """
  )
  connection.execute(
    f"""
    CREATE VIEW trials AS
    SELECT
      manifest.job.id AS job_id,
      manifest.job.started_at AS run_started_at,
      manifest.bundle.source_commit AS source_commit,
      manifest.bundle.framework_revision AS framework_revision,
      manifest.dataset.name AS dataset_name,
      manifest.dataset.ref AS dataset_ref,
      manifest.score_config_sha256 AS score_config_sha256,
      manifest.roster_sha256 AS roster_sha256,
      {_MANIFEST_KEY} AS manifest_key,
      trial.trial AS trial,
      trial.task AS task,
      trial.harness AS harness,
      trial.bro AS bro,
      trial.model AS model,
      trial.llm AS llm,
      trial.rewards AS rewards,
      trial.reward AS reward,
      trial.error AS error,
      trial.started_at AS started_at,
      trial.finished_at AS finished_at,
      trial.root_trail_id AS root_trail_id,
      trial.llm_calls AS llm_calls,
      trial.tokens AS tokens,
      trial.cost_usd AS cost_usd,
      regexp_replace({_MANIFEST_KEY}, '/retention[.]json$', '')
        || '/' || trial.trial || '/agent/ride/trails/' AS store_prefix
    FROM {reader} AS manifest
    CROSS JOIN UNNEST(manifest.trials) AS trial_rows(trial)
    WHERE manifest.format = {retention.MANIFEST_FORMAT}
    """
  )


@contextmanager
def database() -> Iterator[duckdb.DuckDBPyConnection]:
  config = retention.configured_retention()
  with closing(duckdb.connect()) as connection:
    connection.execute('INSTALL httpfs')
    connection.execute('LOAD httpfs')
    connection.execute('INSTALL aws')
    connection.execute('LOAD aws')
    connection.execute(
      f'CREATE SECRET (TYPE s3, PROVIDER credential_chain, REGION {_sql_string(config.region)})'
    )
    define_views(
      connection,
      f's3://{config.bucket}/{retention.PREFIX_ROOT}/*/*/{retention.MANIFEST_FILENAME}',
    )
    yield connection


def _execute(connection: duckdb.DuckDBPyConnection, sql: str) -> None:
  result = connection.sql(sql)
  if result is not None:
    result.show(max_width=sys.maxsize, max_rows=sys.maxsize, max_col_width=sys.maxsize)


def _shell(connection: duckdb.DuckDBPyConnection) -> None:
  print(f'DuckDB {duckdb.__version__}; query the runs and trials views; .quit exits')
  lines: list[str] = []
  while True:
    try:
      line = input('D ' if len(lines) == 0 else '  ')
    except EOFError:
      if lines:
        _execute(connection, '\n'.join(lines))
      else:
        print()
      return
    if not lines and line.strip() in {'.exit', '.quit'}:
      return
    lines.append(line)
    if not line.rstrip().endswith(';'):
      continue
    try:
      _execute(connection, '\n'.join(lines))
    except duckdb.Error as error:
      print(error, file=sys.stderr)
    lines.clear()


def command(sql: Optional[str], sql_file: Optional[Path]) -> None:
  if sql is not None and sql_file is not None:
    raise ValueError('inline SQL and --file are mutually exclusive')
  statement = sql_file.read_text() if sql_file is not None else sql
  with database() as connection:
    if statement is None:
      _shell(connection)
    else:
      _execute(connection, statement)
