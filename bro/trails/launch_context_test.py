from typing import Any

import pytest

from bro.trails import backends
from bro.trails.backends import attached_header
from bro.trails.importing import imported_header
from bro.trails.launch_context import fold_launch_context, rebuild_launch_context
from bro.trails.model import BlazeRequest


def _git_record(**fields: Any) -> dict:
  return {
    'kind': 'git',
    'subtype': 'state',
    'title': 'git state at launch',
    'fields': fields,
  }


def _header(**fields: Any) -> dict:
  return {'id': 'trail-1', **fields}


def _blaze_request(**fields: Any) -> BlazeRequest:
  return BlazeRequest(
    harness='bro',
    version='test',
    interactive=False,
    surface='ask',
    body={'records': []},
    native={'llm': {'type': 'test'}},
    bro='dev',
    **fields,
  )


def _recorded_header(**fields: Any) -> dict:
  return {
    'id': 'trail-1',
    'format': 1,
    'harness': 'bro',
    'bro': 'dev',
    'version': 'test',
    'started_at': '2026-01-01T00:00:00Z',
    'last_alive_at': '2026-01-01T00:00:00Z',
    'end': None,
    'interactive': False,
    'surface': 'ask',
    'turn_count': 0,
    'extent': 0,
    'native': {'llm': {'type': 'test'}},
    **fields,
  }


def test_attached_header_restamps_git():
  request = _blaze_request(git={'branch': 'latest', 'base_sha': 'def456'})

  restamped = attached_header(
    {'git': {'branch': 'original', 'base_sha': 'abc123'}, 'native': {}}, request
  )

  assert restamped.values['git'] == request.git
  assert restamped.removed == frozenset()


def test_attached_header_removes_git_when_the_latest_launch_has_none():
  restamped = attached_header(
    {'git': {'branch': 'original', 'base_sha': 'abc123'}, 'native': {}}, _blaze_request()
  )

  assert 'git' not in restamped.values
  assert restamped.removed == {'git'}


def test_imported_header_validates_and_keeps_git():
  git = {'branch': 'workspace-test', 'base_sha': 'abc123'}

  imported = imported_header(_recorded_header(git=git), backends.BRO_ADAPTER)

  assert imported['git'] == git


def test_imported_header_refuses_null_git():
  with pytest.raises(ValueError, match='git must be a non-empty object'):
    imported_header(_recorded_header(git=None), backends.BRO_ADAPTER)


def test_plain_git_context_folds_into_git_alone():
  context = [_git_record(branch='workspace-test', base_sha='abc123')]

  folded = fold_launch_context(_header(), context)

  assert folded['git'] == {'branch': 'workspace-test', 'base_sha': 'abc123'}
  assert 'legacy_launch_context' not in folded


def test_git_context_with_base_ref_is_kept_whole():
  context = [
    _git_record(branch='workspace-test', base_sha='abc123', base_ref='integration/758-expand')
  ]

  folded = fold_launch_context(_header(), context)

  assert folded['git'] == {'branch': 'workspace-test', 'base_sha': 'abc123'}
  assert folded['legacy_launch_context'] == context


def test_context_without_git_is_kept_whole():
  context = [{'kind': 'system_prompt', 'subtype': 'bro', 'text': 'system prompt'}]

  folded = fold_launch_context(_header(), context)

  assert 'git' not in folded
  assert folded['legacy_launch_context'] == context


def test_fold_keeps_header_git_from_a_later_launch():
  latest = {'repo': '/source/bro', 'branch': 'latest', 'base_sha': 'def456'}
  context = [_git_record(branch='original', base_sha='abc123')]

  folded = fold_launch_context(_header(git=latest), context)

  assert folded == _header(git=latest)


@pytest.mark.parametrize(
  'context, message',
  [
    (
      [
        _git_record(branch='one', base_sha='abc123'),
        _git_record(branch='two', base_sha='def456'),
      ],
      'more than one git/state record',
    ),
    ({'kind': 'git'}, 'launch context must be a list'),
    ([{'kind': 'system_prompt'}, 'not an object'], 'record 1 must be an object'),
  ],
)
def test_fold_refuses_invalid_context_with_the_trail_named(context, message):
  with pytest.raises(ValueError, match=f'trail trail-1.*{message}'):
    fold_launch_context(_header(), context)


def test_fold_is_idempotent():
  context = [
    {'kind': 'system_prompt', 'subtype': 'bro', 'text': 'system prompt'},
    _git_record(branch='workspace-test', base_sha='abc123'),
  ]

  folded = fold_launch_context(_header(), context)

  assert fold_launch_context(folded, context) == folded


def test_fold_refuses_a_different_context_after_legacy_context_is_set():
  folded = fold_launch_context(_header(), [{'kind': 'system_prompt', 'text': 'original'}])

  with pytest.raises(ValueError, match='trail trail-1.*differs'):
    fold_launch_context(folded, [{'kind': 'system_prompt', 'text': 'different'}])


@pytest.mark.parametrize(
  'context',
  [
    [_git_record(branch='workspace-test', base_sha='abc123')],
    [
      {'kind': 'system_prompt', 'subtype': 'bro', 'text': 'system prompt'},
      _git_record(branch='workspace-test', base_sha='abc123'),
    ],
  ],
)
def test_folding_rebuilt_context_leaves_header_unchanged(context):
  folded = fold_launch_context(_header(), context)

  rebuilt = rebuild_launch_context(folded)

  assert fold_launch_context(folded, rebuilt) == folded


def test_rebuilt_context_refuses_null_git_with_the_trail_named():
  with pytest.raises(ValueError, match='trail trail-1 carries invalid git state'):
    rebuild_launch_context(_header(git=None))


def test_rebuilt_context_keeps_stored_order():
  context = [
    {'kind': 'system_prompt', 'subtype': 'bro', 'text': 'system prompt'},
    _git_record(branch='workspace-test', base_sha='abc123'),
    {'kind': 'mcp', 'subtype': 'servers', 'servers': ['bro']},
  ]

  assert rebuild_launch_context(_header(legacy_launch_context=context)) == context
