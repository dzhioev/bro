"""Retain finished benchmark job directories in S3."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, Optional

import boto3
from boto3.exceptions import Boto3Error
from harbor.models.job.result import JobResult
from harbor.models.trial.result import TrialResult

from bro.base import credentials, log
from bro.benchmark.harbor_agent import benchmark_bundle

RETENTION_CREDENTIAL = 'benchmark_retention'
MANIFEST_FILENAME = 'retention.json'
MANIFEST_FORMAT = 1
_PREFIX_ROOT = 'runs'
_DIGEST_PREFIX = 'sha256:'


class RetentionError(RuntimeError):
  pass


@dataclass(frozen=True)
class RetentionConfig:
  bucket: str
  region: str


@dataclass(frozen=True)
class RetainedRun:
  bucket: str
  prefix: str

  @property
  def url(self) -> str:
    return f's3://{self.bucket}/{self.prefix}/'


def _canonical_bytes(value: object) -> bytes:
  return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()


def _sha256(value: object) -> str:
  return _DIGEST_PREFIX + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
  try:
    value = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError) as error:
    raise ValueError(f'invalid benchmark record at {path}: {error}') from error
  if not isinstance(value, dict):
    raise ValueError(f'invalid benchmark record at {path}: expected a JSON object')
  return value


def _config_from_text(text: str) -> RetentionConfig:
  try:
    value = json.loads(text)
  except json.JSONDecodeError as error:
    raise ValueError(f'{RETENTION_CREDENTIAL} credential is not valid JSON') from error
  if not isinstance(value, dict) or set(value) != {'bucket', 'region'}:
    raise ValueError(
      f'{RETENTION_CREDENTIAL} credential must be an object with exactly bucket and region'
    )
  for field in ('bucket', 'region'):
    field_value = value[field]
    if not isinstance(field_value, str) or field_value == '' or field_value.strip() != field_value:
      raise ValueError(f'{RETENTION_CREDENTIAL} {field} must be a non-empty trimmed string')
  return RetentionConfig(bucket=value['bucket'], region=value['region'])


def configured_retention() -> Optional[RetentionConfig]:
  value = credentials.try_get(RETENTION_CREDENTIAL)
  return None if value is None else _config_from_text(value)


def _file_digest(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as file:
    while chunk := file.read(1024 * 1024):
      digest.update(chunk)
  return _DIGEST_PREFIX + digest.hexdigest()


def _inventory(job_directory: Path) -> dict[str, dict[str, object]]:
  files: dict[str, dict[str, object]] = {}
  for path in sorted(job_directory.rglob('*')):
    relative = path.relative_to(job_directory).as_posix()
    if relative == MANIFEST_FILENAME:
      continue
    if path.is_symlink() or not (path.is_dir() or path.is_file()):
      raise ValueError(f'benchmark job contains an unsupported file at {path}')
    if path.is_file():
      files[relative] = {'sha256': _file_digest(path), 'size': path.stat().st_size}
  return files


def _reported_revision(job_directory: Path) -> str:
  result_paths = sorted(job_directory.glob('*/result.json'))
  revisions = {
    TrialResult.model_validate_json(path.read_text()).agent_info.version for path in result_paths
  }
  if len(revisions) != 1:
    raise ValueError(
      f'{len(result_paths)} benchmark trials report {len(revisions)} framework revisions, '
      'expected exactly one'
    )
  [revision] = revisions
  if not re.fullmatch(r'sha256:[0-9a-f]{64}', revision):
    raise ValueError(f'benchmark result reports malformed framework revision {revision!r}')
  return revision


def _hub_url(job_directory: Path) -> Optional[str]:
  path = job_directory / 'upload.json'
  if not path.exists():
    return None
  record = _read_json_object(path)
  if set(record) != {'visibility', 'url'}:
    raise ValueError(f'invalid benchmark upload record at {path}: unexpected fields')
  if record['visibility'] not in {'private', 'public'}:
    raise ValueError(f'invalid benchmark upload record at {path}: malformed visibility')
  if not isinstance(record['url'], str) or record['url'] == '':
    raise ValueError(f'invalid benchmark upload record at {path}: malformed URL')
  return record['url']


def _score_config(config: dict[str, Any]) -> dict[str, Any]:
  score_config = dict(config)
  score_config.pop('job_name', None)
  score_config.pop('jobs_dir', None)
  return score_config


def _run_prefix(
  job: JobResult,
  config_digest: str,
  roster_digest: str,
  source_commit: str,
  framework_revision: str,
) -> str:
  started_at = job.started_at
  if started_at.tzinfo is not None:
    started_at = started_at.astimezone(UTC)
    timestamp = started_at.strftime('%Y%m%dT%H%M%S.%fZ')
  else:
    timestamp = started_at.strftime('%Y%m%dT%H%M%S.%f')
  return '/'.join(
    (
      _PREFIX_ROOT,
      started_at.strftime('%Y-%m-%d'),
      timestamp,
      f'config-{config_digest.removeprefix(_DIGEST_PREFIX)}',
      f'roster-{roster_digest.removeprefix(_DIGEST_PREFIX)}',
      f'commit-{source_commit}',
      f'revision-{framework_revision.removeprefix(_DIGEST_PREFIX)}',
      f'job-{job.id}',
    )
  )


def _manifest(job_directory: Path) -> tuple[dict[str, object], str]:
  job = JobResult.model_validate_json((job_directory / 'result.json').read_text())
  if job.finished_at is None:
    raise ValueError(f'benchmark result at {job_directory} is not finished')
  config = _read_json_object(job_directory / 'config.json')
  roster = config.get('agents')
  if not isinstance(roster, list) or len(roster) == 0:
    raise ValueError(f'benchmark config at {job_directory} has no agent roster')
  bundle = benchmark_bundle()
  framework_revision = _reported_revision(job_directory)
  if framework_revision != bundle.identity:
    raise ValueError(
      f'benchmark result reports {framework_revision}, but the host bundle is {bundle.identity}'
    )
  score_config_digest = _sha256(_score_config(config))
  roster_digest = _sha256(roster)
  prefix = _run_prefix(
    job,
    score_config_digest,
    roster_digest,
    bundle.source_commit,
    framework_revision,
  )
  manifest: dict[str, object] = {
    'format': MANIFEST_FORMAT,
    'job': {
      'id': str(job.id),
      'started_at': job.started_at.isoformat(),
      'finished_at': job.finished_at.isoformat(),
    },
    'job_config': config,
    'score_config_sha256': score_config_digest,
    'roster_sha256': roster_digest,
    'bundle': {
      'source_commit': bundle.source_commit,
      'framework_revision': framework_revision,
    },
    'files': _inventory(job_directory),
  }
  hub_url = _hub_url(job_directory)
  if hub_url is not None:
    manifest['hub_url'] = hub_url
  return manifest, prefix


def retain_job(job_directory: Path) -> Optional[RetainedRun]:
  config = configured_retention()
  if config is None:
    log.info('benchmark retention skipped: no %s credential', RETENTION_CREDENTIAL)
    return None
  manifest, prefix = _manifest(job_directory)
  manifest_path = job_directory / MANIFEST_FILENAME
  manifest_path.write_bytes(_canonical_bytes(manifest) + b'\n')
  client = boto3.Session(region_name=config.region).client('s3')
  files = [
    path for path in sorted(job_directory.rglob('*')) if path.is_file() and path != manifest_path
  ]
  try:
    for path in files:
      key = f'{prefix}/{path.relative_to(job_directory).as_posix()}'
      client.upload_file(str(path), config.bucket, key)
    client.upload_file(str(manifest_path), config.bucket, f'{prefix}/{MANIFEST_FILENAME}')
  except Boto3Error as error:
    raise RetentionError(
      f'failed to retain benchmark run in s3://{config.bucket}/{prefix}'
    ) from error
  return RetainedRun(config.bucket, prefix)
