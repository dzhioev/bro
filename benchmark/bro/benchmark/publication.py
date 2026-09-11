"""Publish immutable retained benchmark runs to the Harbor Hub."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from harbor.models.job.result import JobResult
from harbor.models.trajectories import Trajectory
from harbor.models.trial.result import TrialResult

from bro.base import credentials
from bro.benchmark import retention
from bro.benchmark.trajectory import convert_trial_trajectory
from bro.llm import providers

HARBOR_CREDENTIAL = 'harbor'
PUBLICATIONS_DIRECTORY = 'publications'
_HARBOR_API_KEY_ENV = 'HARBOR_API_KEY'
_HARBOR_URL = re.compile(r'^View at (https://\S+)$', re.MULTILINE)
_DIGEST_PREFIX = 'sha256:'

Visibility = Literal['private', 'public']


class PublicationError(RuntimeError):
  pass


@dataclass(frozen=True)
class Publication:
  visibility: Visibility
  url: str
  published_at: str
  record_url: str


def _required_string(value: Any, field: str) -> str:
  if not isinstance(value, str) or value == '' or value.strip() != value:
    raise ValueError(f'{field} must be a non-empty trimmed string')
  return value


def _object(value: Any, field: str) -> dict[str, Any]:
  if not isinstance(value, dict):
    raise ValueError(f'{field} must be an object')
  return value


def _array(value: Any, field: str) -> list[Any]:
  if not isinstance(value, list):
    raise ValueError(f'{field} must be an array')
  return value


def _optional_cost(value: Any, field: str) -> float | None:
  if value is None:
    return None
  if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
    raise ValueError(f'{field} must be a non-negative number or null')
  return float(value)


def _run_prefix(value: str) -> str:
  prefix = value.removesuffix('/')
  path = PurePosixPath(prefix)
  if path.is_absolute() or path.parts[:1] != (retention.PREFIX_ROOT,) or len(path.parts) != 3:
    raise ValueError('run prefix must have the form runs/<YYYY-MM-DD>/<job-id>')
  if any(part in {'', '.', '..'} for part in path.parts):
    raise ValueError('run prefix must not contain empty or relative path components')
  return path.as_posix()


def _manifest(client: Any, config: retention.RetentionConfig, prefix: str) -> dict[str, Any]:
  key = f'{prefix}/{retention.MANIFEST_FILENAME}'
  try:
    response = client.get_object(Bucket=config.bucket, Key=key)
    with response['Body'] as body:
      content = body.read()
  except (KeyError, OSError, BotoCoreError, ClientError) as error:
    raise PublicationError(f'failed to read s3://{config.bucket}/{key}: {error}') from error
  try:
    value = json.loads(content)
  except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
    raise ValueError(
      f'invalid retention manifest at s3://{config.bucket}/{key}: {error}'
    ) from error
  manifest = _object(value, 'retention manifest')
  if manifest.get('format') != retention.MANIFEST_FORMAT:
    raise ValueError(
      f'retention manifest must have format {retention.MANIFEST_FORMAT}, '
      f'got {manifest.get("format")!r}'
    )
  return manifest


def _safe_relative_path(value: Any) -> Path:
  raw = _required_string(value, 'retention file path')
  path = PurePosixPath(raw)
  if path.is_absolute() or any(part in {'', '.', '..'} for part in path.parts):
    raise ValueError(f'retention file path is not relative: {raw!r}')
  return Path(*path.parts)


def _file_sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as file:
    while chunk := file.read(1024 * 1024):
      digest.update(chunk)
  return _DIGEST_PREFIX + digest.hexdigest()


def _download_run(
  client: Any,
  config: retention.RetentionConfig,
  prefix: str,
  manifest: dict[str, Any],
  destination: Path,
) -> None:
  seen: set[Path] = set()
  for raw_file in _array(manifest.get('files'), 'retention manifest files'):
    file = _object(raw_file, 'retention file')
    if set(file) != {'path', 'sha256', 'size'}:
      raise ValueError('retention file must contain exactly path, sha256, and size')
    relative_path = _safe_relative_path(file['path'])
    if relative_path in seen:
      raise ValueError(f'retention manifest repeats file {relative_path.as_posix()}')
    seen.add(relative_path)
    expected_digest = _required_string(file['sha256'], f'{relative_path} sha256')
    expected_size = file['size']
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
      raise ValueError(f'{relative_path} size must be a non-negative integer')
    path = destination / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    key = f'{prefix}/{relative_path.as_posix()}'
    try:
      client.download_file(config.bucket, key, str(path))
    except (OSError, BotoCoreError, ClientError) as error:
      raise PublicationError(f'failed to download s3://{config.bucket}/{key}: {error}') from error
    if path.stat().st_size != expected_size:
      raise PublicationError(f'downloaded file size differs from retention manifest: {key}')
    if _file_sha256(path) != expected_digest:
      raise PublicationError(f'downloaded file digest differs from retention manifest: {key}')


def _price_tables(manifest: dict[str, Any]) -> dict[str, Any]:
  pricing = _object(manifest.get('pricing'), 'retention manifest pricing')
  tables: dict[str, Any] = {}
  for provider, raw_record in pricing.items():
    provider_name = _required_string(provider, 'pricing provider')
    record = _object(raw_record, f'pricing record for {provider_name}')
    if set(record) != {'as_of', 'source', 'sha256', 'rates'}:
      raise ValueError(
        f'pricing record for {provider_name} must contain exactly as_of, source, sha256, and rates'
      )
    digest = _required_string(record['sha256'], f'pricing record for {provider_name} sha256')
    if len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest):
      raise ValueError(f'pricing record for {provider_name} sha256 must be 64 lowercase hex digits')
    rates = _object(record['rates'], f'pricing rates for {provider_name}')
    tables[provider_name] = providers.price_table_from_content(
      provider_name,
      _required_string(record['source'], f'pricing record for {provider_name} source'),
      _required_string(record['as_of'], f'pricing record for {provider_name} as_of'),
      rates,
    )
  return tables


def _write_json(path: Path, value: dict[str, Any]) -> None:
  path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def _derive_published_job(
  job_directory: Path, manifest: dict[str, Any], price_tables: Mapping[str, Any]
) -> None:
  bundle = _object(manifest.get('bundle'), 'retention manifest bundle')
  agent_version = _required_string(bundle.get('framework_revision'), 'bundle framework_revision')
  raw_trials = _array(manifest.get('trials'), 'retention manifest trials')
  rows: dict[str, dict[str, Any]] = {}
  for raw_row in raw_trials:
    row = _object(raw_row, 'retention trial')
    trial_name = _required_string(row.get('trial'), 'retention trial name')
    if trial_name in rows:
      raise ValueError(f'retention manifest repeats trial {trial_name}')
    rows[trial_name] = row

  trial_directories = {
    path.name: path
    for path in job_directory.iterdir()
    if path.is_dir() and (path / 'result.json').is_file()
  }
  if set(trial_directories) != set(rows):
    raise ValueError('retention trial rows do not match the downloaded Harbor trial directories')
  for trial_name, row in rows.items():
    trial_directory = trial_directories[trial_name]
    result_path = trial_directory / 'result.json'
    result = _object(json.loads(result_path.read_text()), f'trial {trial_name} result')
    agent_result = result.get('agent_result')
    if agent_result is None:
      agent_result = {}
      result['agent_result'] = agent_result
    agent_result_object = _object(agent_result, f'trial {trial_name} agent_result')
    expected_cost = _optional_cost(row.get('cost_usd'), f'trial {trial_name} cost_usd')
    agent_result_object['cost_usd'] = expected_cost
    TrialResult.model_validate(result)
    _write_json(result_path, result)

    root_trail_id = row.get('root_trail_id')
    if root_trail_id is None:
      continue
    _required_string(root_trail_id, f'trial {trial_name} root_trail_id')
    destination = convert_trial_trajectory(
      trial_directory / 'agent', agent_version=agent_version, price_tables=price_tables
    )
    trajectory = Trajectory.model_validate_json(destination.read_text())
    actual_cost = (
      None if trajectory.final_metrics is None else trajectory.final_metrics.total_cost_usd
    )
    if actual_cost != expected_cost:
      raise ValueError(
        f'trial {trial_name} trajectory cost {actual_cost!r} differs from '
        f'retention manifest cost {expected_cost!r}'
      )

  result_path = job_directory / 'result.json'
  result = _object(json.loads(result_path.read_text()), 'job result')
  stats = _object(result.get('stats'), 'job result stats')
  stats['cost_usd'] = _optional_cost(manifest.get('total_cost_usd'), 'total_cost_usd')
  JobResult.model_validate(result)
  _write_json(result_path, result)


def _upload(job_directory: Path, visibility: Visibility, api_key: str) -> str:
  harbor = shutil.which('harbor')
  if harbor is None:
    raise PublicationError('harbor executable is not installed')
  environment = os.environ.copy()
  environment[_HARBOR_API_KEY_ENV] = api_key
  completed = subprocess.run(
    [harbor, 'upload', str(job_directory), f'--{visibility}'],
    capture_output=True,
    text=True,
    env=environment,
  )
  output = '\n'.join(part for part in (completed.stdout, completed.stderr) if part)
  if completed.returncode != 0:
    raise PublicationError(
      f'harbor upload failed with status {completed.returncode}: {output.strip()}'
    )
  matches = _HARBOR_URL.findall(output)
  if len(matches) != 1:
    raise PublicationError('harbor upload succeeded without reporting exactly one Hub URL')
  return matches[0]


def _publication_time() -> str:
  return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def _put_publication(
  client: Any,
  config: retention.RetentionConfig,
  prefix: str,
  visibility: Visibility,
  url: str,
) -> Publication:
  published_at = _publication_time()
  key = f'{prefix}/{PUBLICATIONS_DIRECTORY}/{published_at}.json'
  record = {'visibility': visibility, 'url': url, 'published_at': published_at}
  content = (
    json.dumps(record, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode() + b'\n'
  )
  checksum = base64.b64encode(hashlib.sha256(content).digest()).decode()
  try:
    client.put_object(
      Bucket=config.bucket,
      Key=key,
      Body=content,
      ChecksumAlgorithm='SHA256',
      ChecksumSHA256=checksum,
      IfNoneMatch='*',
    )
  except (BotoCoreError, ClientError) as error:
    raise PublicationError(
      f'Harbor upload succeeded at {url}, but publication record failed at '
      f's3://{config.bucket}/{key}: {error}'
    ) from error
  return Publication(visibility, url, published_at, f's3://{config.bucket}/{key}')


def publish_run(run_prefix: str, visibility: Visibility) -> Publication:
  prefix = _run_prefix(run_prefix)
  config = retention.configured_retention()
  api_key = credentials.get(HARBOR_CREDENTIAL)
  if api_key == '':
    raise ValueError('harbor credential must not be empty')
  try:
    client = boto3.Session(region_name=config.region).client('s3')
  except (BotoCoreError, ClientError) as error:
    raise PublicationError(f'failed to connect to retention storage: {error}') from error
  manifest = _manifest(client, config, prefix)
  job = _object(manifest.get('job'), 'retention manifest job')
  if _required_string(job.get('id'), 'retention manifest job id') != PurePosixPath(prefix).name:
    raise ValueError('run prefix job id differs from the retention manifest')
  tables = _price_tables(manifest)
  with tempfile.TemporaryDirectory(prefix='benchmark-publish-') as temporary:
    job_directory = Path(temporary) / PurePosixPath(prefix).name
    job_directory.mkdir()
    _download_run(client, config, prefix, manifest, job_directory)
    _derive_published_job(job_directory, manifest, tables)
    url = _upload(job_directory, visibility, api_key)
  return _put_publication(client, config, prefix, visibility, url)
