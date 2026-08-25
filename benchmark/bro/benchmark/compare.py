#!/usr/bin/env python
"""Compare per-task benchmark rewards with a public leaderboard submission."""

import asyncio
import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from harbor.auth.errors import AuthenticationError
from harbor.constants import HARBOR_VIEWER_JOBS_URL
from harbor.hub.client import HubClient
from harbor.models.job.result import JobResult
from harbor.models.trial.result import TrialResult

import bro.base.args as base_args
from bro.base import log
from bro.benchmark.retention import MANIFEST_FILENAME, PREFIX_ROOT

__cli_name__ = 'benchmark-compare'

REFERENCE_FORMAT = 1
REFERENCE_DIRECTORY = 'references'
REFERENCE_DATASET = 'terminal-bench/terminal-bench-2-1'
DEFAULT_REFERENCE = 'claude-code'
REFERENCE_SOURCES: Mapping[str, str] = {
  DEFAULT_REFERENCE: (
    'https://raw.githubusercontent.com/harbor-framework/terminal-bench-2-1/'
    '7131e4375048a0e408a8fb404b5f499d726b695b/'
    'leaderboard/submissions/'
    '2026-07-09-anthropic-claude-opus-4-8-high-claude-code.json'
  )
}
_DIGEST_PATTERN = re.compile(r'sha256:[0-9a-f]{64}')


class ComparisonError(RuntimeError):
  pass


@dataclass(frozen=True)
class TrialReward:
  task_name: str
  agent_name: str
  model_name: Optional[str]
  reward: float


@dataclass(frozen=True)
class RunInput:
  description: str
  trials: tuple[TrialReward, ...]
  cache: 'ReferenceCache'
  run_count: int
  cohort: Optional[tuple[str, str]]


@dataclass(frozen=True)
class ReferenceInput:
  description: str
  source: str
  source_jobs: tuple[str, ...]
  trials: tuple[TrialReward, ...]


@dataclass(frozen=True)
class TaskMean:
  mean: float
  count: int


@dataclass(frozen=True)
class ComparisonRow:
  task_name: str
  ours: Optional[TaskMean]
  reference: Optional[TaskMean]
  delta: Optional[float]
  diverges: bool


class ReferenceCache:
  def read(self, key: str) -> Optional[bytes]:
    raise NotImplementedError

  def write(self, key: str, content: bytes) -> None:
    raise NotImplementedError


@dataclass(frozen=True)
class LocalReferenceCache(ReferenceCache):
  directory: Path

  def read(self, key: str) -> Optional[bytes]:
    path = self.directory / f'{key}.json'
    return path.read_bytes() if path.exists() else None

  def write(self, key: str, content: bytes) -> None:
    self.directory.mkdir(parents=True, exist_ok=True)
    (self.directory / f'{key}.json').write_bytes(content)


@dataclass(frozen=True)
class S3ReferenceCache(ReferenceCache):
  client: Any
  bucket: str

  def _key(self, key: str) -> str:
    return f'{REFERENCE_DIRECTORY}/{key}.json'

  def read(self, key: str) -> Optional[bytes]:
    try:
      return self.client.get_object(Bucket=self.bucket, Key=self._key(key))['Body'].read()
    except ClientError as error:
      if error.response.get('Error', {}).get('Code') in {'NoSuchKey', '404'}:
        return None
      raise

  def write(self, key: str, content: bytes) -> None:
    self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=content)


def _read_json_object(content: bytes, description: str) -> dict[str, Any]:
  try:
    value = json.loads(content)
  except (UnicodeDecodeError, json.JSONDecodeError) as error:
    raise ComparisonError(f'invalid JSON in {description}: {error}') from error
  if not isinstance(value, dict):
    raise ComparisonError(f'invalid JSON in {description}: expected an object')
  return value


def _primary_reward(result: TrialResult, description: str) -> float:
  if result.verifier_result is not None and result.verifier_result.rewards is not None:
    rewards = result.verifier_result.rewards
    if 'reward' not in rewards:
      raise ComparisonError(f'{description} has no primary reward')
    reward = float(rewards['reward'])
  elif result.exception_info is not None:
    reward = 0.0
  else:
    raise ComparisonError(f'{description} has neither a reward nor an exception')
  if not math.isfinite(reward):
    raise ComparisonError(f'{description} has a non-finite reward')
  return reward


def _model_name(result: TrialResult) -> Optional[str]:
  model = result.agent_info.model_info
  if model is None:
    return None
  return f'{model.provider}/{model.name}' if model.provider else model.name


def _trial_reward(content: bytes, description: str) -> TrialReward:
  try:
    result = TrialResult.model_validate_json(content)
  except ValueError as error:
    raise ComparisonError(f'invalid trial result in {description}: {error}') from error
  return TrialReward(
    task_name=result.task_name,
    agent_name=result.agent_info.name,
    model_name=_model_name(result),
    reward=_primary_reward(result, description),
  )


def _local_run(path: Path) -> RunInput:
  job_path = path.resolve()
  try:
    result = JobResult.model_validate_json((job_path / 'result.json').read_text())
  except (OSError, ValueError) as error:
    raise ComparisonError(f'invalid Harbor job at {job_path}: {error}') from error
  if result.finished_at is None:
    raise ComparisonError(f'Harbor job at {job_path} is not finished')
  result_paths = sorted(job_path.glob('*/result.json'))
  if len(result_paths) != result.n_total_trials:
    raise ComparisonError(
      f'Harbor job at {job_path} records {result.n_total_trials} total trials but contains '
      f'{len(result_paths)} trial results'
    )
  trials = tuple(_trial_reward(path.read_bytes(), str(path)) for path in result_paths)
  return RunInput(
    description=str(job_path),
    trials=trials,
    cache=LocalReferenceCache(job_path.parent / REFERENCE_DIRECTORY),
    run_count=1,
    cohort=None,
  )


def _s3_uri(value: str) -> tuple[str, str]:
  parsed = urlparse(value)
  if parsed.scheme != 's3' or parsed.netloc == '':
    raise ComparisonError(f'invalid S3 run location {value!r}')
  return parsed.netloc, parsed.path.strip('/')


def _list_s3_keys(client: Any, bucket: str, prefix: str) -> list[str]:
  keys: list[str] = []
  continuation_token: Optional[str] = None
  while True:
    arguments: dict[str, object] = {'Bucket': bucket, 'Prefix': prefix}
    if continuation_token is not None:
      arguments['ContinuationToken'] = continuation_token
    response = client.list_objects_v2(**arguments)
    records = response.get('Contents', [])
    if not isinstance(records, list):
      raise ComparisonError('S3 listing returned malformed contents')
    for record in records:
      key = record.get('Key') if isinstance(record, dict) else None
      if not isinstance(key, str):
        raise ComparisonError('S3 listing returned an object without a key')
      keys.append(key)
    if not response.get('IsTruncated'):
      return keys
    continuation_token = response.get('NextContinuationToken')
    if not isinstance(continuation_token, str) or continuation_token == '':
      raise ComparisonError('S3 listing was truncated without a continuation token')


def _s3_bytes(client: Any, bucket: str, key: str) -> bytes:
  return client.get_object(Bucket=bucket, Key=key)['Body'].read()


def _manifest_cohort(manifest: dict[str, Any], description: str) -> tuple[str, str]:
  score_config_sha256 = manifest.get('score_config_sha256')
  roster_sha256 = manifest.get('roster_sha256')
  if not (
    isinstance(score_config_sha256, str)
    and _DIGEST_PATTERN.fullmatch(score_config_sha256)
    and isinstance(roster_sha256, str)
    and _DIGEST_PATTERN.fullmatch(roster_sha256)
  ):
    raise ComparisonError(f'{description} has malformed cohort digests')
  return score_config_sha256, roster_sha256


def _selected_manifests(
  manifests: list[tuple[str, dict[str, Any]]],
  score_config_sha256: Optional[str],
  roster_sha256: Optional[str],
) -> list[tuple[str, dict[str, Any]]]:
  selected = [
    item
    for item in manifests
    if (score_config_sha256 is None or item[1].get('score_config_sha256') == score_config_sha256)
    and (roster_sha256 is None or item[1].get('roster_sha256') == roster_sha256)
  ]
  if not selected:
    raise ComparisonError('no complete retained runs match the requested cohort')
  cohorts = {_manifest_cohort(manifest, key) for key, manifest in selected}
  if len(cohorts) != 1:
    choices = ', '.join(f'{score} / {roster}' for score, roster in sorted(cohorts))
    raise ComparisonError(
      'retention bucket contains multiple cohorts; select one with '
      f'--score-config-sha256 and --roster-sha256 ({choices})'
    )
  return selected


def _retained_trial_keys(prefix: str, manifest: dict[str, Any]) -> list[str]:
  inventory = manifest.get('files')
  if not isinstance(inventory, dict):
    raise ComparisonError(f'{prefix}/{MANIFEST_FILENAME} has no file inventory')
  relative_paths = sorted(
    relative
    for relative in inventory
    if isinstance(relative, str) and relative != 'result.json' and relative.endswith('/result.json')
  )
  if not relative_paths:
    raise ComparisonError(f'{prefix}/{MANIFEST_FILENAME} records no trial results')
  return [f'{prefix}/{relative}' for relative in relative_paths]


def _s3_run(
  location: str,
  region: Optional[str],
  score_config_sha256: Optional[str],
  roster_sha256: Optional[str],
) -> RunInput:
  bucket, requested_prefix = _s3_uri(location)
  for name, value in (
    ('score config', score_config_sha256),
    ('roster', roster_sha256),
  ):
    if value is not None and not _DIGEST_PATTERN.fullmatch(value):
      raise ComparisonError(f'{name} digest must be sha256:<64 lowercase hex characters>')
  client = boto3.Session(region_name=region).client('s3')
  listing_prefix = f'{requested_prefix}/' if requested_prefix else f'{PREFIX_ROOT}/'
  manifest_keys = [
    key
    for key in _list_s3_keys(client, bucket, listing_prefix)
    if key.endswith(f'/{MANIFEST_FILENAME}')
  ]
  if not manifest_keys:
    raise ComparisonError(f'{location} contains no complete retained runs')
  manifests = [
    (key, _read_json_object(_s3_bytes(client, bucket, key), f's3://{bucket}/{key}'))
    for key in manifest_keys
  ]
  selected = _selected_manifests(manifests, score_config_sha256, roster_sha256)
  trials: list[TrialReward] = []
  for manifest_key, manifest in selected:
    prefix = manifest_key.removesuffix(f'/{MANIFEST_FILENAME}')
    for key in _retained_trial_keys(prefix, manifest):
      trials.append(_trial_reward(_s3_bytes(client, bucket, key), f's3://{bucket}/{key}'))
  cohort = _manifest_cohort(selected[0][1], selected[0][0])
  return RunInput(
    description=f's3://{bucket}/ ({len(selected)} retained runs)',
    trials=tuple(trials),
    cache=S3ReferenceCache(client, bucket),
    run_count=len(selected),
    cohort=cohort,
  )


def load_run(
  location: str,
  *,
  region: Optional[str] = None,
  score_config_sha256: Optional[str] = None,
  roster_sha256: Optional[str] = None,
) -> RunInput:
  if location.startswith('s3://'):
    return _s3_run(location, region, score_config_sha256, roster_sha256)
  if score_config_sha256 is not None or roster_sha256 is not None:
    raise ComparisonError('cohort digest selectors apply only to an S3 run location')
  return _local_run(Path(location))


def _reference_source(reference: str) -> str:
  return REFERENCE_SOURCES.get(reference, reference)


def _cache_key(source: str) -> str:
  return hashlib.sha256(source.encode()).hexdigest()


def _read_reference_submission(source: str) -> bytes:
  if source.startswith(('https://', 'http://')):
    try:
      with urllib.request.urlopen(source, timeout=30) as response:
        return response.read()
    except (OSError, urllib.error.URLError) as error:
      raise ComparisonError(f'failed to fetch reference submission {source}: {error}') from error
  try:
    return Path(source).read_bytes()
  except OSError as error:
    raise ComparisonError(f'failed to read reference submission {source}: {error}') from error


def _job_id(value: str) -> str:
  job_id = value.rstrip('/').rsplit('/', 1)[-1]
  try:
    return str(UUID(job_id))
  except ValueError as error:
    raise ComparisonError(f'invalid reference source job {value!r}') from error


def _source_job_ids(submission: dict[str, Any]) -> tuple[str, ...]:
  source_jobs = submission.get('source_jobs')
  if (
    not isinstance(source_jobs, list)
    or not source_jobs
    or not all(isinstance(value, str) for value in source_jobs)
  ):
    raise ComparisonError('reference submission has malformed source_jobs')
  return tuple(_job_id(value) for value in source_jobs)


def _source_job_urls(submission: dict[str, Any]) -> tuple[str, ...]:
  return tuple(f'{HARBOR_VIEWER_JOBS_URL}/{job_id}' for job_id in _source_job_ids(submission))


async def _fetch_hub_trial_rows(job_ids: list[str]) -> list[dict[str, Any]]:
  client = HubClient()
  rows: list[dict[str, Any]] = []
  for job_id in job_ids:
    page_number = 1
    while True:
      page = await client.get_job_trials([job_id], page=page_number, page_size=500)
      raw_rows = page.raw.get('items')
      if not isinstance(raw_rows, list) or not all(isinstance(row, dict) for row in raw_rows):
        raise ComparisonError(f'Harbor Hub returned malformed trials for job {job_id}')
      rows.extend(raw_rows)
      if page_number >= page.total_pages:
        break
      page_number += 1
  return rows


def _full_row_model(row: dict[str, Any]) -> Optional[str]:
  model_name = row.get('model_name')
  provider = row.get('model_provider')
  if not isinstance(model_name, str):
    return None
  return (
    f'{provider}/{model_name}'
    if isinstance(provider, str) and provider not in {'', 'unknown'}
    else model_name
  )


def _reference_reward(row: dict[str, Any], disqualified: set[str]) -> float:
  trial_id = row.get('id')
  if not isinstance(trial_id, str):
    raise ComparisonError('reference trial has no id')
  if trial_id in disqualified:
    return 0.0
  value = row.get('reward')
  if value is None and row.get('error_type') is not None:
    return 0.0
  if not isinstance(value, (int, float)) or isinstance(value, bool):
    raise ComparisonError(f'reference trial {trial_id} has no numeric reward or error')
  reward = float(value)
  if not math.isfinite(reward):
    raise ComparisonError(f'reference trial {trial_id} has a non-finite reward')
  return reward


def _reference_trials(
  submission: dict[str, Any], rows: Iterable[dict[str, Any]]
) -> tuple[TrialReward, ...]:
  source_filter = submission.get('source_filter')
  if not isinstance(source_filter, dict):
    raise ComparisonError('reference submission has no source_filter')
  required_filter = ('agent', 'agent_version', 'model_name')
  if not all(isinstance(source_filter.get(field), str) for field in required_filter):
    raise ComparisonError('reference submission has a malformed source_filter')
  materialized = submission.get('trials')
  if materialized is not None and (
    not isinstance(materialized, list)
    or not all(isinstance(value, str) for value in materialized)
    or len(set(materialized)) != len(materialized)
  ):
    raise ComparisonError('reference submission has a malformed trials list')
  materialized_ids = set(materialized) if materialized is not None else None
  disqualified_records = submission.get('disqualified_trials') or []
  if not isinstance(disqualified_records, list) or not all(
    isinstance(record, dict) and isinstance(record.get('trial_id'), str)
    for record in disqualified_records
  ):
    raise ComparisonError('reference submission has malformed disqualified_trials')
  disqualified: set[str] = {record['trial_id'] for record in disqualified_records}
  selected: list[TrialReward] = []
  selected_ids: set[str] = set()
  for row in rows:
    trial_id = row.get('id')
    if (
      row.get('source') != REFERENCE_DATASET
      or row.get('agent_name') != source_filter['agent']
      or row.get('agent_version') != source_filter['agent_version']
      or _full_row_model(row) != source_filter['model_name']
      or (materialized_ids is not None and trial_id not in materialized_ids)
    ):
      continue
    task_name = row.get('task_name')
    if not isinstance(trial_id, str) or not isinstance(task_name, str) or task_name == '':
      raise ComparisonError('reference contains a trial with malformed identity')
    if trial_id in selected_ids:
      raise ComparisonError(f'reference Hub jobs return trial {trial_id} more than once')
    selected_ids.add(trial_id)
    selected.append(
      TrialReward(
        task_name=task_name,
        agent_name=source_filter['agent'],
        model_name=source_filter['model_name'],
        reward=_reference_reward(row, disqualified),
      )
    )
  if materialized_ids is not None and selected_ids != materialized_ids:
    missing = sorted(materialized_ids - selected_ids)
    raise ComparisonError(
      f'reference Hub jobs are missing {len(missing)} materialized submission trials'
    )
  if not disqualified <= selected_ids:
    raise ComparisonError('reference disqualifies trials outside its selected Hub records')
  if not selected:
    raise ComparisonError('reference submission selects no Hub trials')
  return tuple(selected)


def _reference_description(submission: dict[str, Any]) -> str:
  source_filter = submission['source_filter']
  metadata = submission.get('metadata')
  display = metadata.get('agent_display') if isinstance(metadata, dict) else None
  label = display.get('label') if isinstance(display, dict) else None
  return f'{label or source_filter["agent"]} / {source_filter["model_name"]}'


def _reference_input(
  submission: dict[str, Any], rows: Iterable[dict[str, Any]], source: str
) -> ReferenceInput:
  trials = _reference_trials(submission, rows)
  return ReferenceInput(
    description=_reference_description(submission),
    source=source,
    source_jobs=_source_job_urls(submission),
    trials=trials,
  )


def _decode_cached_reference(content: bytes, source: str) -> ReferenceInput:
  record = _read_json_object(content, f'cached reference for {source}')
  if record.get('format') != REFERENCE_FORMAT or record.get('source') != source:
    raise ComparisonError(f'cached reference for {source} has an incompatible format')
  submission = record.get('submission')
  rows = record.get('hub_trials')
  if (
    not isinstance(submission, dict)
    or not isinstance(rows, list)
    or not all(isinstance(row, dict) for row in rows)
  ):
    raise ComparisonError(f'cached reference for {source} is malformed')
  return _reference_input(submission, rows, source)


def load_reference(
  reference: str, cache: ReferenceCache, *, refresh: bool = False
) -> ReferenceInput:
  source = _reference_source(reference)
  key = _cache_key(source)
  if not refresh:
    cached = cache.read(key)
    if cached is not None:
      return _decode_cached_reference(cached, source)
  submission = _read_json_object(_read_reference_submission(source), source)
  rows = asyncio.run(_fetch_hub_trial_rows(list(_source_job_ids(submission))))
  reference_input = _reference_input(submission, rows, source)
  record = {
    'format': REFERENCE_FORMAT,
    'source': source,
    'submission': submission,
    'hub_trials': rows,
  }
  cache.write(key, json.dumps(record, ensure_ascii=False, sort_keys=True).encode() + b'\n')
  return reference_input


def _select_ours(
  trials: tuple[TrialReward, ...], agent_name: Optional[str], model_name: Optional[str]
) -> tuple[str, Optional[str], tuple[TrialReward, ...]]:
  available = sorted({(trial.agent_name, trial.model_name or '') for trial in trials})
  selected = tuple(
    trial
    for trial in trials
    if (agent_name is None or trial.agent_name == agent_name)
    and (model_name is None or trial.model_name == model_name)
  )
  selected_groups = {(trial.agent_name, trial.model_name) for trial in selected}
  if len(selected_groups) != 1:
    choices = ', '.join(f'{agent} / {model or "<unknown model>"}' for agent, model in available)
    if not selected:
      raise ComparisonError(f'our run selector matched no trials; available groups: {choices}')
    raise ComparisonError(
      'our run contains multiple agent/model groups; select one with --agent and --model '
      f'({choices})'
    )
  [(selected_agent, selected_model)] = selected_groups
  return selected_agent, selected_model, selected


def _means(trials: Iterable[TrialReward]) -> dict[str, TaskMean]:
  rewards: dict[str, list[float]] = defaultdict(list)
  for trial in trials:
    rewards[trial.task_name].append(trial.reward)
  return {
    task_name: TaskMean(sum(values) / len(values), len(values))
    for task_name, values in rewards.items()
  }


def compare_rewards(
  ours: Iterable[TrialReward], reference: Iterable[TrialReward], divergence: float
) -> list[ComparisonRow]:
  if not math.isfinite(divergence) or divergence < 0:
    raise ComparisonError(
      f'divergence threshold must be a finite non-negative number, got {divergence}'
    )
  ours_by_task = _means(ours)
  reference_by_task = _means(reference)
  rows: list[ComparisonRow] = []
  for task_name in sorted(ours_by_task):
    ours_mean = ours_by_task[task_name]
    reference_mean = reference_by_task.get(task_name)
    delta = None if reference_mean is None else ours_mean.mean - reference_mean.mean
    diverges = delta is None or abs(delta) > divergence
    rows.append(ComparisonRow(task_name, ours_mean, reference_mean, delta, diverges))
  return rows


def _format_mean(value: Optional[TaskMean]) -> str:
  return '—' if value is None else f'{value.mean:.3f}'


def _format_count(value: Optional[TaskMean]) -> str:
  return '—' if value is None else str(value.count)


def _task_count(count: int) -> str:
  return f'{count} {"task" if count == 1 else "tasks"}'


def render_report(
  run: RunInput,
  ours_description: str,
  ours: tuple[TrialReward, ...],
  reference: ReferenceInput,
  rows: list[ComparisonRow],
  divergence: float,
) -> str:
  task_width = max(len('task'), *(len(row.task_name) for row in rows))
  lines = [
    f'ours: {ours_description} from {run.description}',
    f'reference: {reference.description}',
    f'submission: {reference.source}',
    f'reference jobs: {", ".join(reference.source_jobs)}',
  ]
  if run.cohort is not None:
    lines.append(f'cohort: {run.cohort[0]} / {run.cohort[1]}')
  lines += [
    '',
    f'  {"task":<{task_width}}  ours   n   reference   n    delta',
    f'  {"-" * task_width}  -----  ---  ---------  ---  -------',
  ]
  for row in rows:
    marker = '*' if row.diverges else ' '
    delta = '—' if row.delta is None else f'{row.delta:+.3f}'
    lines.append(
      f'{marker} {row.task_name:<{task_width}}  {_format_mean(row.ours):>5}  '
      f'{_format_count(row.ours):>3}  {_format_mean(row.reference):>9}  '
      f'{_format_count(row.reference):>3}  {delta:>7}'
    )
  ours_by_task = _means(ours)
  reference_by_task = _means(reference.trials)
  ours_overall = sum(value.mean for value in ours_by_task.values()) / len(ours_by_task)
  matching_reference = tuple(trial for trial in reference.trials if trial.task_name in ours_by_task)
  matching_reference_by_task = _means(matching_reference)
  reference_overall = (
    f'{sum(value.mean for value in matching_reference_by_task.values()) / len(matching_reference_by_task):.3f}'
    if matching_reference_by_task
    else '—'
  )
  divergent_count = sum(row.diverges for row in rows)
  ours_only = sum(row.reference is None for row in rows)
  omitted_reference_tasks = len(set(reference_by_task) - set(ours_by_task))
  lines += [
    '',
    f'{_task_count(len(rows))}; ours {len(ours)} trials / mean {ours_overall:.3f}; '
    f'reference {len(matching_reference)} matching trials / mean {reference_overall}',
    f'{_task_count(divergent_count)} {"diverges" if divergent_count == 1 else "diverge"} '
    f'(absolute delta > {divergence:.3f} or missing reference); '
    f'{_task_count(ours_only)} missing from reference; '
    f'{_task_count(omitted_reference_tasks)} from reference outside this run omitted; '
    '* marks divergence',
  ]
  return '\n'.join(lines)


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog='bro.benchmark.compare',
    description='compare one benchmark agent per task with a public leaderboard submission',
  )
  parser.add_argument('run', help='local Harbor job directory or s3:// retention bucket/prefix')
  parser.add_argument(
    '--agent', help='exact agent name from our trial results; required when the run has several'
  )
  parser.add_argument('--model', help='exact provider/model from our trial results')
  parser.add_argument(
    '--reference',
    default=DEFAULT_REFERENCE,
    help='leaderboard submission path/URL or claude-code (default: claude-code)',
  )
  parser.add_argument(
    '--divergence',
    default=0.0,
    type=float,
    help='absolute mean-reward delta that earns a divergence marker (default: 0)',
  )
  parser.add_argument(
    '--refresh-reference', action='store_true', help='ignore the cached reference'
  )
  parser.add_argument('--region', help='AWS region for an S3 run location')
  parser.add_argument('--score-config-sha256', help='select one retained score-config cohort')
  parser.add_argument('--roster-sha256', help='select one retained agent-roster cohort')
  args = parser.parse(argv)
  try:
    run = load_run(
      args['run'],
      region=args['region'],
      score_config_sha256=args['score_config_sha256'],
      roster_sha256=args['roster_sha256'],
    )
    selected_agent, selected_model, ours = _select_ours(run.trials, args['agent'], args['model'])
    reference = load_reference(args['reference'], run.cache, refresh=args['refresh_reference'])
    rows = compare_rewards(ours, reference.trials, args['divergence'])
    ours_description = f'{selected_agent} / {selected_model or "<unknown model>"}'
    print(render_report(run, ours_description, ours, reference, rows, args['divergence']))
  except (AuthenticationError, BotoCoreError, ClientError, ComparisonError) as error:
    log.error('benchmark comparison failed: %s', error)
    return 1
  return 0
