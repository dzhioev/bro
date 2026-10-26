import pytest

from bro.dev import references
from bros.eyebro import Eyebro


def test_reviewer_carries_review_spells_but_no_author_procedures():
  bro = Eyebro()
  assert 'review-diff' in bro.spell_paths
  assert 'review-pr' in bro.spell_paths
  # a reviewer must not carry the author-side procedures; Eyebro derives from
  # Bro rather than Dev precisely to keep them out
  assert 'fix' not in bro.spell_paths
  assert 'run-pr' not in bro.spell_paths
  assert 'land' not in bro.spell_paths


def test_claude_surface_selects_the_reference_tools():
  assert [server.namespace for server in Eyebro().assemble(harness='claude', hold='attended')] == [
    'dev-style-source',
    'man-source',
    'bro',
    'spell',
  ]


@pytest.mark.asyncio
async def test_manual_carries_every_development_guide():
  servers = Eyebro().assemble(harness='claude', hold='attended')
  manual = next(server for server in servers if server.namespace == 'man-source')
  [read] = await manual.list_tools()
  topics = read.parameters['properties']['topic']['enum']
  assert {guide.name for guide in references.guides} <= set(topics)


def test_github_is_the_reviewers_best_effort_credential():
  bro = Eyebro()
  assert 'github' in bro.optional_secrets(harness='claude')
  assert 'github' not in bro.needed_secrets(harness='claude')


def test_review_pr_drives_the_pull_request_only_under_a_github_identity(monkeypatch):
  bro = Eyebro()
  monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'github')
  with_identity = bro.get_spell_body('review-pr', harness='claude', hold='unattended')
  assert 'pr-state' in with_identity
  assert 'no GitHub identity' not in with_identity

  monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
  without_identity = bro.get_spell_body('review-pr', harness='claude', hold='unattended')
  assert 'no GitHub identity' in without_identity
  assert 'pr-state' not in without_identity


def test_review_pr_cannot_be_offered_without_the_spells_it_runs():
  import bros.eyebro as eyebro
  from bros.bro import Bro

  class Incomplete(Bro):
    name = 'incomplete'
    description = 'test bro'
    spells = (eyebro.spells.review_pr,)

  with pytest.raises(ValueError, match="requires spell 'review-diff'"):
    Incomplete()
