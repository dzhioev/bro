"""Transitional fold between stored launch contexts and trail headers."""

from dataclasses import replace
from typing import Any, Optional

from bro.trails.model import BlazeRequest, validate_git

_GIT_RECORD_KEYS = frozenset({'kind', 'subtype', 'title', 'fields'})
_GIT_FIELDS = frozenset({'branch', 'base_sha'})
_GIT_RECORD_TITLE = 'git state at launch'


def fold_launch_context(header: dict, launch_context: Any) -> dict:
  trail = _trail_description(header)
  if not isinstance(launch_context, list):
    raise ValueError(f'{trail} launch context must be a list')
  for index, record in enumerate(launch_context):
    if not isinstance(record, dict):
      raise ValueError(f'{trail} launch context record {index} must be an object')

  git_records = [record for record in launch_context if _is_git_record(record)]
  if len(git_records) > 1:
    raise ValueError(f'{trail} launch context carries more than one git/state record')

  folded = dict(header)
  if len(git_records) == 1 and 'git' not in folded:
    folded['git'] = _git_from_record(git_records[0], trail)

  existing = folded.get('legacy_launch_context')
  if 'legacy_launch_context' in folded:
    if existing != launch_context:
      raise ValueError(f'{trail} legacy launch context differs from its stored context')
  elif not _is_plain_git_context(launch_context):
    folded['legacy_launch_context'] = launch_context
  return folded


def fold_request_launch_context(request: BlazeRequest, trail_id: str) -> BlazeRequest:
  if 'launch_context' not in request.body:
    return request
  header: dict[str, Any] = {'id': trail_id}
  if request.git is not None:
    header['git'] = request.git
  folded = fold_launch_context(header, request.body['launch_context'])
  return replace(request, git=folded.get('git'))


def rebuild_launch_context(header: dict) -> Optional[Any]:
  if 'legacy_launch_context' in header:
    return header['legacy_launch_context']
  if 'git' not in header:
    return None

  git = header['git']
  try:
    validate_git(git)
  except ValueError as error:
    raise ValueError(f'{_trail_description(header)} carries invalid git state: {error}') from error
  fields = {field: git[field] for field in ('branch', 'base_sha') if field in git}
  return [
    {
      'kind': 'git',
      'subtype': 'state',
      'title': _GIT_RECORD_TITLE,
      'fields': fields,
    }
  ]


def _git_from_record(record: dict, trail: str) -> dict[str, str]:
  fields = record.get('fields')
  if not isinstance(fields, dict):
    raise ValueError(f'{trail} git/state record fields must be an object')
  git = {field: fields[field] for field in ('branch', 'base_sha') if field in fields}
  try:
    validate_git(git)
  except ValueError as error:
    raise ValueError(f'{trail} carries an invalid git/state record: {error}') from error
  return git


def _is_git_record(record: dict) -> bool:
  return record.get('kind') == 'git' and record.get('subtype') == 'state'


def _is_plain_git_context(launch_context: list[dict]) -> bool:
  if len(launch_context) != 1:
    return False
  record = launch_context[0]
  fields = record.get('fields')
  return (
    _is_git_record(record)
    and set(record) == _GIT_RECORD_KEYS
    and record.get('title') == _GIT_RECORD_TITLE
    and isinstance(fields, dict)
    and set(fields).issubset(_GIT_FIELDS)
  )


def _trail_description(header: dict) -> str:
  trail_id = header.get('id')
  return f'trail {trail_id}' if isinstance(trail_id, str) else f'trail {trail_id!r}'
