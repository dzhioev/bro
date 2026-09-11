"""Retain immutable benchmark job directories in S3."""

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from harbor.models.job.result import JobResult
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult

from bro import artifact
from bro.base import credentials
from bro.benchmark.bundle import Bundle
from bro.llm import providers, usage
from bro.trails.cost import trail_cost
from bro.trails.local import LocalStore

RETENTION_CREDENTIAL = 'benchmark_retention'
MANIFEST_FILENAME = 'retention.json'
MANIFEST_FORMAT = 3
PREFIX_ROOT = 'runs'
TRAILS_DIRECTORY = Path('agent') / 'ride' / 'trails'
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


@dataclass(frozen=True)
class InventoryFile:
  path: Path
  relative_path: str
  sha256: str
  checksum: str
  size: int

  def manifest_row(self) -> dict[str, object]:
    return {'path': self.relative_path, 'sha256': self.sha256, 'size': self.size}


def _canonical_bytes(value: object) -> bytes:
  return json.dumps(
    value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False
  ).encode()


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


def configured_retention() -> RetentionConfig:
  return _config_from_text(credentials.get(RETENTION_CREDENTIAL))


def _file_digest(path: Path) -> tuple[str, str]:
  digest = hashlib.sha256()
  with path.open('rb') as file:
    while chunk := file.read(1024 * 1024):
      digest.update(chunk)
  raw = digest.digest()
  return _DIGEST_PREFIX + digest.hexdigest(), base64.b64encode(raw).decode()


def _inventory(job_directory: Path) -> list[InventoryFile]:
  files: list[InventoryFile] = []
  for path in sorted(job_directory.rglob('*')):
    relative_path = path.relative_to(job_directory).as_posix()
    if relative_path == MANIFEST_FILENAME:
      raise ValueError(f'benchmark job contains reserved marker {path}')
    if path.is_symlink() or not (path.is_dir() or path.is_file()):
      raise ValueError(f'benchmark job contains an unsupported file at {path}')
    if path.is_file():
      sha256, checksum = _file_digest(path)
      files.append(
        InventoryFile(
          path=path,
          relative_path=relative_path,
          sha256=sha256,
          checksum=checksum,
          size=path.stat().st_size,
        )
      )
  return files


def _score_config(config: dict[str, Any]) -> dict[str, Any]:
  score_config = dict(config)
  score_config.pop('job_name', None)
  score_config.pop('jobs_dir', None)
  return score_config


def _required_string(value: Any, field: str) -> str:
  if not isinstance(value, str) or value == '':
    raise ValueError(f'{field} must be a non-empty string')
  return value


def _agent_dimensions(config_path: Path) -> tuple[str, str, str]:
  config = TrialConfig.model_validate(_read_json_object(config_path))
  if config.agent is None:
    raise ValueError(f'benchmark trial config at {config_path} has no agent')
  kwargs = config.agent.kwargs
  bro = _required_string(kwargs.get('bro'), f'benchmark trial config at {config_path} agent bro')
  harness = _required_string(
    kwargs.get('harness', 'bro'), f'benchmark trial config at {config_path} agent harness'
  )
  model = _required_string(
    config.agent.model_name, f'benchmark trial config at {config_path} agent model_name'
  )
  return harness, bro, model


def _root_header(store: LocalStore) -> dict[str, Any]:
  headers = list(store.iter_trails())
  roots = [header for header in headers if header.get('summoned_by') is None]
  if len(roots) != 1:
    raise ValueError(f'trails store at {store.root} must hold exactly one root, found {len(roots)}')
  return roots[0]


def _header_llm(header: dict[str, Any]) -> dict[str, Any]:
  native = header.get('native')
  if not isinstance(native, dict):
    raise ValueError('trail header native must be an object')
  llm = native.get('llm')
  if not isinstance(llm, dict):
    raise ValueError('trail header native.llm must be an object')
  return llm


def _message_call(message: dict[str, Any]) -> tuple[str, dict[str, Any], Optional[str]]:
  model = _required_string(message.get('model'), 'llm_call model')
  raw_usage = message.get('usage')
  if not isinstance(raw_usage, dict):
    raise ValueError('llm_call usage must be an object')
  service_tier = message.get('service_tier')
  if service_tier is not None and not isinstance(service_tier, str):
    raise ValueError('llm_call service_tier must be a string or null')
  return model, raw_usage, service_tier


def _pricing_record(provider: str, models: set[str]) -> dict[str, object]:
  table = providers.price_table(provider)
  try:
    rates = {model: table.models[model].content() for model in sorted(models)}
    return {
      'as_of': table.as_of.isoformat(),
      'source': table.source,
      'sha256': table.sha256,
      'rates': rates,
    }
  except (AttributeError, KeyError) as error:
    raise ValueError(
      f'provider {provider!r} has no serializable rates for {sorted(models)}'
    ) from error


def _trail_summary(
  store_root: Path, expected_harness: str, expected_bro: str
) -> tuple[
  Optional[str],
  Optional[dict[str, Any]],
  int,
  Optional[dict[str, int]],
  Optional[float],
  dict[str, set[str]],
]:
  if not (store_root / 'trails').is_dir():
    return None, None, 0, None, None, {}
  store = LocalStore(store_root)
  with store:
    first = next(store.iter_trails(max_items=1), None)
    if first is None:
      return None, None, 0, None, None, {}
    root = _root_header(store)
    if root.get('harness') != expected_harness:
      raise ValueError(
        f'trail {root.get("id")} reports harness {root.get("harness")!r}, '
        f'expected {expected_harness!r}'
      )
    if root.get('bro') != expected_bro:
      raise ValueError(
        f'trail {root.get("id")} reports bro {root.get("bro")!r}, expected {expected_bro!r}'
      )

    llm_calls = 0
    tokens = usage.zero()
    cost: Optional[Decimal] = Decimal(0)
    priced_models: dict[str, set[str]] = {}
    for header in store.iter_trails():
      trail_id = _required_string(header.get('id'), 'trail header id')
      provider = _required_string(
        _header_llm(header).get('type'), f'trail {trail_id} native.llm.type'
      )
      current_cost = trail_cost(store, trail_id)
      if current_cost is None:
        cost = None
      elif cost is not None:
        cost += current_cost
      for message in store.iter_messages(trail_id, types={'llm_call'}):
        model, raw_usage, service_tier = _message_call(message)
        llm_calls += 1
        tokens = usage.add(tokens, usage.from_vendor_counts(raw_usage))
        if providers.price(provider, model, raw_usage, service_tier) is not None:
          priced_models.setdefault(provider, set()).add(model)

    root_id = _required_string(root.get('id'), 'root trail id')
    return (
      root_id,
      _header_llm(root),
      llm_calls,
      tokens,
      None if cost is None else float(cost),
      priced_models,
    )


def _trial_error(result: TrialResult) -> Optional[str]:
  error = result.exception_info
  if error is None:
    return None
  return f'{error.exception_type}: {error.exception_message}'


def _trial_row(
  trial_directory: Path, framework_revision: str
) -> tuple[dict[str, object], dict[str, set[str]]]:
  result_path = trial_directory / 'result.json'
  try:
    result = TrialResult.model_validate_json(result_path.read_text())
  except (OSError, ValueError) as error:
    raise ValueError(f'invalid benchmark trial result at {result_path}: {error}') from error
  harness, bro, model = _agent_dimensions(trial_directory / 'config.json')
  agent_directory = trial_directory / 'agent'
  reached_install = result.agent_setup is not None or (
    agent_directory.is_dir() and next(agent_directory.rglob('*'), None) is not None
  )
  if reached_install and result.agent_info.version != framework_revision:
    raise ValueError(
      f'benchmark trial {trial_directory.name} reports bundle {result.agent_info.version!r}, '
      f'expected {framework_revision!r}'
    )

  root_trail_id, llm, llm_calls, tokens, cost_usd, priced_models = _trail_summary(
    trial_directory / TRAILS_DIRECTORY, harness, bro
  )
  error = _trial_error(result)
  if error is None:
    if result.verifier_result is None or result.verifier_result.rewards is None:
      raise ValueError(f'benchmark trial {trial_directory.name} has neither rewards nor an error')
    rewards: Optional[dict[str, float | int]] = result.verifier_result.rewards
    if 'reward' not in rewards:
      raise ValueError(f'benchmark trial {trial_directory.name} has no primary reward')
    reward: Optional[float | int] = rewards['reward']
  else:
    rewards = None
    reward = None

  return (
    {
      'trial': trial_directory.name,
      'task': result.task_name,
      'harness': harness,
      'bro': bro,
      'model': model,
      'llm': llm,
      'rewards': rewards,
      'reward': reward,
      'error': error,
      'started_at': None if result.started_at is None else result.started_at.isoformat(),
      'finished_at': None if result.finished_at is None else result.finished_at.isoformat(),
      'root_trail_id': root_trail_id,
      'llm_calls': llm_calls,
      'tokens': tokens,
      'cost_usd': cost_usd,
    },
    priced_models,
  )


def _dataset(config: dict[str, Any], results: list[TrialResult]) -> dict[str, str]:
  if any(result.source is None for result in results):
    raise ValueError('benchmark trials must report their dataset source')
  sources = {result.source for result in results if result.source is not None}
  if len(sources) != 1:
    raise ValueError(f'benchmark job spans {len(sources)} dataset sources: {sorted(sources)}')
  [source] = sources
  datasets = config.get('datasets')
  if not isinstance(datasets, list):
    raise ValueError('benchmark config has no dataset roster')
  matches = [
    dataset for dataset in datasets if isinstance(dataset, dict) and dataset.get('name') == source
  ]
  if len(matches) != 1:
    raise ValueError(f'benchmark dataset source {source!r} matches {len(matches)} config entries')
  match = matches[0]
  return {
    'name': _required_string(match.get('name'), 'benchmark dataset name'),
    'ref': _required_string(match.get('ref'), 'benchmark dataset ref'),
  }


def _manifest(job_directory: Path, inventory: list[InventoryFile]) -> tuple[dict[str, object], str]:
  result_path = job_directory / 'result.json'
  try:
    job = JobResult.model_validate_json(result_path.read_text())
  except (OSError, ValueError) as error:
    raise ValueError(f'invalid benchmark job result at {result_path}: {error}') from error
  if job.finished_at is None:
    raise ValueError(f'benchmark result at {job_directory} is not finished')
  config = _read_json_object(job_directory / 'config.json')
  roster = config.get('agents')
  if not isinstance(roster, list) or len(roster) == 0:
    raise ValueError(f'benchmark config at {job_directory} has no agent roster')

  bundle = Bundle(job_directory)
  framework_revision = bundle.identity
  source_commit = bundle.source_commit
  trial_directories = [
    path
    for path in sorted(job_directory.iterdir())
    if path.is_dir() and (path / 'result.json').is_file()
  ]
  if len(trial_directories) == 0:
    raise ValueError(f'benchmark job at {job_directory} holds no trial results')
  trial_results = [
    TrialResult.model_validate_json((trial_directory / 'result.json').read_text())
    for trial_directory in trial_directories
  ]
  dataset = _dataset(config, trial_results)

  trials: list[dict[str, object]] = []
  priced_models: dict[str, set[str]] = {}
  for trial_directory in trial_directories:
    row, trial_priced_models = _trial_row(trial_directory, framework_revision)
    trials.append(row)
    for provider, models in trial_priced_models.items():
      priced_models.setdefault(provider, set()).update(models)
  total_cost_usd: Optional[float] = 0.0
  for trial in trials:
    cost_usd = trial['cost_usd']
    if cost_usd is None:
      total_cost_usd = None
      break
    if not isinstance(cost_usd, (int, float)) or isinstance(cost_usd, bool):
      raise TypeError(f'trial cost must be numeric or null, got {cost_usd!r}')
    total_cost_usd += float(cost_usd)
  if job.started_at.tzinfo is None:
    raise ValueError('benchmark job started_at must carry a timezone')
  started_at = job.started_at.astimezone(UTC)
  prefix = f'{PREFIX_ROOT}/{started_at.strftime("%Y-%m-%d")}/{job.id}'
  manifest: dict[str, object] = {
    'format': MANIFEST_FORMAT,
    'job': {
      'id': str(job.id),
      'started_at': job.started_at.isoformat(),
      'finished_at': job.finished_at.isoformat(),
      'n_retries': job.stats.n_retries,
    },
    'config': config,
    'score_config_sha256': _sha256(_score_config(config)),
    'roster_sha256': _sha256(roster),
    'dataset': dataset,
    'bundle': {
      'source_commit': source_commit,
      'framework_revision': framework_revision,
    },
    'pricing': {
      provider: _pricing_record(provider, models)
      for provider, models in sorted(priced_models.items())
    },
    'total_cost_usd': total_cost_usd,
    'trials': trials,
    'files': [file.manifest_row() for file in inventory],
  }
  return manifest, prefix


def _existing_checksum(client: Any, bucket: str, key: str) -> Optional[str]:
  response = client.head_object(Bucket=bucket, Key=key, ChecksumMode='ENABLED')
  checksum = response.get('ChecksumSHA256')
  return checksum if isinstance(checksum, str) else None


def _put_file(client: Any, config: RetentionConfig, prefix: str, file: InventoryFile) -> None:
  key = f'{prefix}/{file.relative_path}'
  try:
    with file.path.open('rb') as body:
      client.put_object(
        Bucket=config.bucket,
        Key=key,
        Body=body,
        ChecksumAlgorithm='SHA256',
        ChecksumSHA256=file.checksum,
        IfNoneMatch='*',
      )
  except ClientError as error:
    code = str(error.response.get('Error', {}).get('Code'))
    if code not in {'PreconditionFailed', '412'}:
      raise
    if _existing_checksum(client, config.bucket, key) == file.checksum:
      return
    raise RetentionError(
      f'retention key already holds different content: s3://{config.bucket}/{key}'
    ) from error


def _put_manifest(
  client: Any, config: RetentionConfig, prefix: str, manifest: dict[str, object]
) -> None:
  key = f'{prefix}/{MANIFEST_FILENAME}'
  content = _canonical_bytes(manifest) + b'\n'
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
  except ClientError as error:
    code = str(error.response.get('Error', {}).get('Code'))
    if code in {'PreconditionFailed', '412'}:
      raise RetentionError(
        f'benchmark run is already retained at s3://{config.bucket}/{prefix}/'
      ) from error
    raise


def retain_job(job_directory: Path) -> RetainedRun:
  config = configured_retention()
  inventory = _inventory(job_directory)
  manifest, prefix = _manifest(job_directory, inventory)
  try:
    client = boto3.Session(region_name=config.region).client('s3')
    for file in inventory:
      _put_file(client, config, prefix, file)
    _put_manifest(client, config, prefix, manifest)
  except RetentionError:
    raise
  except (OSError, BotoCoreError, ClientError) as error:
    raise RetentionError(
      f'failed to retain benchmark run in s3://{config.bucket}/{prefix}: {error}'
    ) from error
  return RetainedRun(config.bucket, prefix)


def resolve_job(value: str) -> Path:
  path = Path(artifact.get_artifact(value)) if artifact.is_ref(value) else Path(value)
  if artifact.is_ref(value):
    output = path / 'output'
    jobs = (
      [entry for entry in sorted(output.iterdir()) if entry.is_dir()] if output.is_dir() else []
    )
    if len(jobs) != 1:
      raise ValueError(
        f'benchmark artifact {value} holds {len(jobs)} jobs under output, expected one'
      )
    path = jobs[0]
  if not path.is_dir() or not (path / 'result.json').is_file():
    raise ValueError(f'no Harbor job directory at {path}')
  return path
