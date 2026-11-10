import pytest

from bro.base.text_window import BYTE_LIMIT
from bro.bro import BaseBro
from bro.dev import references
from bros.eyebro import Eyebro, spells


@pytest.mark.asyncio
async def test_reviewer_serves_review_spells_but_no_author_procedures():
  servers = Eyebro().assemble(harness='claude', hold='attended')
  server = next(server for server in servers if server.namespace == 'spell')
  assert {tool.name for tool in await server.list_tools()} == {
    'ask',
    'watch',
    'review-diff',
    'review',
  }


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


_CHANNEL_MARKERS = {
  'github': '## 2. Arm replay, then reconcile',
  'talk': '### Pushed review branch',
  'local': '### Local delivery, without a remote',
}


@pytest.mark.parametrize('channel', _CHANNEL_MARKERS)
@pytest.mark.parametrize('harness', ['bro', 'claude'])
@pytest.mark.parametrize('github', [True, False])
def test_review_selects_only_its_channel_within_one_window(monkeypatch, channel, harness, github):
  monkeypatch.setattr('bro.base.credentials.available', lambda name: github and name == 'github')
  body = Eyebro().get_spell_body(
    'review', harness=harness, hold='unattended', choices={'channel': channel}
  )
  marker = (
    '## Missing GitHub identity'
    if channel == 'github' and not github
    else _CHANNEL_MARKERS[channel]
  )
  assert marker in body
  for other, other_marker in _CHANNEL_MARKERS.items():
    if other != channel:
      assert other_marker not in body
  assert len(body) <= BYTE_LIMIT


@pytest.mark.parametrize(
  ('missing', 'declaration'),
  [
    ('review-diff', (spells.review,)),
    ('watch', (spells.review, spells.review_diff)),
  ],
)
def test_review_cannot_be_offered_without_the_spells_it_runs(missing, declaration):
  class Incomplete(BaseBro):
    name = 'incomplete'
    description = 'test bro'
    spells = declaration

  with pytest.raises(ValueError, match=f"requires spell '{missing}'"):
    Incomplete()
