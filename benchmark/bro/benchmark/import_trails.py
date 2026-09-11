"""Import a retained run's trial stores into the trails registry."""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from bro.base import credentials
from bro.benchmark import retention
from bro.trails import network, store, transfer

# `HTTPStatusError` is a trails server answering a route it does not serve yet
FAILURES = (
  credentials.SecretNotFound,
  OSError,
  ValueError,
  retention.RetentionError,
  store.PermissionDenied,
  store.TrailCollision,
  store.AppendConflict,
  store.TrailNotFound,
  store.TransientUnavailable,
  network.HTTPStatusError,
)


@dataclass(frozen=True)
class ImportedTrial:
  trial: str
  trail_ids: list[str]


def _trial_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
  rows = manifest.get('trials')
  if not isinstance(rows, list):
    raise ValueError('retention manifest trials must be an array')
  for row in rows:
    if not isinstance(row, dict):
      raise ValueError('retention trial must be an object')
    trial = row.get('trial')
    if not isinstance(trial, str) or trial == '':
      raise ValueError('retention trial name must be a non-empty string')
    root_trail_id = row.get('root_trail_id')
    if root_trail_id is not None and (not isinstance(root_trail_id, str) or root_trail_id == ''):
      raise ValueError(f'trial {trial} root_trail_id must be a non-empty string or null')
  return rows


def import_run(run_prefix: str, registry: store.TrailsStore) -> list[ImportedTrial]:
  """Import every trail the retained run's trials recorded into `registry`,
  each trial's store whole and parents first."""
  prefix = retention.run_prefix(run_prefix)
  config = retention.configured_retention()
  try:
    client = boto3.Session(region_name=config.region).client('s3')
  except (BotoCoreError, ClientError) as error:
    raise retention.RetentionError(f'failed to connect to retention storage: {error}') from error
  manifest = retention.read_manifest(client, config, prefix)
  files = retention.manifest_files(manifest)
  imported: list[ImportedTrial] = []
  with tempfile.TemporaryDirectory(prefix='benchmark-import-trails-') as temporary:
    scratch = Path(temporary)
    for row in _trial_rows(manifest):
      trial = row['trial']
      root_trail_id = row.get('root_trail_id')
      if root_trail_id is None:
        continue
      store_directory = Path(trial) / retention.TRAILS_DIRECTORY
      retained = [file for file in files if file.path.is_relative_to(store_directory)]
      if len(retained) == 0:
        raise ValueError(f'trial {trial} reports trail {root_trail_id} but retains no trail store')
      retention.download_files(client, config, prefix, retained, scratch)
      trail_ids = [
        result['trail_id'] for result in transfer.import_layout(scratch / store_directory, registry)
      ]
      if root_trail_id not in trail_ids:
        raise ValueError(
          f'trial {trial} reports trail {root_trail_id}, which its store does not hold'
        )
      imported.append(ImportedTrial(trial, trail_ids))
  return imported
