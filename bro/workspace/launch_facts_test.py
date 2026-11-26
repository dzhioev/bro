import json
from dataclasses import replace

import pytest

from bro import summon
from bro.authority_view_test_helper import publish
from bro.base import credentials
from bro.broker.environment import BROKER_TALK
from bro.llm.providers import bind_recipe
from bro.workspace.launch_facts import (
  FILENAME,
  Launches,
  LaunchFacts,
  LaunchFactsError,
  resume_notice,
)
from bro.workspace.launch_facts_test_helper import FACTS

_TEXTS_DIFFER = (
  '- spell and reference texts you loaded earlier may differ: '
  'load one again before you rely on its wording'
)


def _lines(notice: str) -> list[str]:
  return notice.splitlines()


def test_a_resume_with_nothing_changed_names_everything_unchanged():
  # §3.5's first example, word for word: the model reads this text as written
  assert resume_notice(FACTS, FACTS) == (
    '[notice: this run continues an earlier one, in a new process.\n'
    'Jobs, watches and summoned children of the earlier run have ended.\n'
    'The working directory may have changed since.\n'
    'Unchanged since then: the runtime; the recipe; your credentials; what you may summon; '
    'spell and reference texts.\n'
    'The mission and the trail are new: call bro::banner for them.]'
  )


def test_a_resume_on_an_updated_runtime_with_a_grant_lists_both_changes():
  # §3.5's second example, word for word
  current = replace(FACTS, runtime='5d0c41aa90e2', may_summon=('@eyebro for bro{}',))
  assert resume_notice(current, FACTS) == (
    '[notice: this run continues an earlier one, in a new process.\n'
    'Jobs, watches and summoned children of the earlier run have ended.\n'
    'The working directory may have changed since.\n'
    'Changed since then:\n'
    '- the runtime was updated\n'
    '- may_summon: none → @eyebro for bro{}\n'
    f'{_TEXTS_DIFFER}\n'
    'Unchanged: the recipe; your credentials.\n'
    'The mission and the trail are new: call bro::banner for them.]'
  )


def test_without_a_baseline_one_sentence_says_what_may_have_changed():
  lines = _lines(resume_notice(FACTS, None))
  assert lines[-2] == (
    'The runtime, the recipe, your credentials and what you may summon may have changed too: '
    'spell and reference texts you loaded earlier may differ; '
    'load one again before you rely on its wording.'
  )
  assert not any(line.startswith(('Changed', 'Unchanged')) for line in lines)


@pytest.mark.parametrize(
  ('changes', 'line', 'unchanged'),
  [
    (
      {'llm': 'anthropic:claude-opus-5-5'},
      '- llm: openai:gpt-6.1-sol,effort=high → anthropic:claude-opus-5-5',
      'the recipe',
    ),
    ({'hold': 'unattended'}, '- hold: attended → unattended', None),
    ({'summoned': True}, '- summoned: no → yes', None),
    ({'talk': ('worker.say',)}, '- talk: unset → worker.say', None),
    (
      {'credentials': ('github+eyebro',)},
      '- credentials: github+, mu+, trails+ → github+eyebro',
      'your credentials',
    ),
    (
      {'may_summon': ('@eyebro for bro{}',)},
      '- may_summon: none → @eyebro for bro{}',
      'what you may summon',
    ),
    ({'permits': ()}, '- permits: bro for * → none', 'what you may summon'),
    ({'may_give': None}, '- may_give: none → unset', 'what you may summon'),
  ],
)
def test_each_differing_fact_gets_a_line_and_leaves_the_unchanged_sentence(
  changes, line, unchanged
):
  lines = _lines(resume_notice(replace(FACTS, **changes), FACTS))
  assert lines[3] == 'Changed since then:'
  assert line in lines
  [sentence] = [entry for entry in lines if entry.startswith('Unchanged: ')]
  if unchanged is not None:
    assert unchanged not in sentence
  assert 'the runtime' in sentence


@pytest.mark.parametrize(
  'changes',
  [
    {'runtime': '5d0c41aa90e2'},
    {'hold': 'unattended'},
    {'credentials': ()},
    {'may_summon': ('@eyebro for bro{}',)},
  ],
)
def test_the_texts_line_follows_runtime_hold_credentials_and_may_summon(changes):
  lines = _lines(resume_notice(replace(FACTS, **changes), FACTS))
  assert _TEXTS_DIFFER in lines
  assert not any('spell and reference texts.' in line for line in lines)


@pytest.mark.parametrize(
  'changes', [{'llm': 'anthropic:claude-opus-5-5'}, {'summoned': True}, {'permits': ()}]
)
def test_the_texts_stay_unchanged_under_the_other_facts(changes):
  lines = _lines(resume_notice(replace(FACTS, **changes), FACTS))
  assert _TEXTS_DIFFER not in lines
  [sentence] = [entry for entry in lines if entry.startswith('Unchanged: ')]
  assert sentence.endswith('spell and reference texts.')


@pytest.mark.parametrize('side', ['earlier', 'current'])
def test_a_null_runtime_on_either_side_counts_as_updated(side):
  materialized = replace(FACTS, runtime=None)
  current, earlier = (materialized, FACTS) if side == 'current' else (FACTS, materialized)
  lines = _lines(resume_notice(current, earlier))
  assert '- the runtime was updated' in lines
  assert _TEXTS_DIFFER in lines
  assert '- the runtime was updated' in _lines(resume_notice(materialized, materialized))


def test_every_fact_differing_leaves_no_unchanged_sentence():
  current = LaunchFacts(
    runtime=None,
    llm='anthropic:claude-opus-5-5',
    hold='unattended',
    summoned=True,
    talk=('worker.say',),
    may_summon=None,
    permits=None,
    may_give=None,
    credentials=(),
  )
  lines = _lines(resume_notice(current, FACTS))
  assert not any(line.startswith('Unchanged') for line in lines)
  assert lines[-1] == 'The mission and the trail are new: call bro::banner for them.]'


def test_facts_round_trip_through_the_wire():
  assert LaunchFacts.from_wire(json.loads(json.dumps(FACTS.to_wire()))) == FACTS


def test_registering_appends_entries_in_order(tmp_path):
  first = Launches.load(tmp_path).register('01m7a', 0, FACTS)
  second = Launches.load(tmp_path).register('01m7b', 3, replace(FACTS, hold='guided'))
  assert Launches.load(tmp_path) == second
  assert [(entry.record, entry.start) for entry in second.entries] == [('01m7a', 0), ('01m7b', 3)]
  assert first.entries == second.entries[:1]
  # the persisted layout §3.5 fixes, which older and newer runtimes read
  value = json.loads((tmp_path / FILENAME).read_text())
  assert value['format'] == 1
  assert value['launches'][1] == {
    'record': '01m7b',
    'from': 3,
    'facts': second.entries[1].facts.to_wire(),
  }
  assert list(tmp_path.iterdir()) == [tmp_path / FILENAME]


def test_no_file_holds_no_entries(tmp_path):
  assert Launches.load(tmp_path).entries == ()


def test_a_file_of_another_format_holds_no_entries_and_is_replaced_on_registering(tmp_path):
  (tmp_path / FILENAME).write_text(json.dumps({'format': 2, 'whatever': True}))
  assert Launches.load(tmp_path).entries == ()
  Launches.load(tmp_path).register('01m7a', 0, FACTS)
  assert [entry.record for entry in Launches.load(tmp_path).entries] == ['01m7a']


@pytest.mark.parametrize(
  'content',
  [
    'not json',
    json.dumps([]),
    json.dumps({'launches': []}),
    json.dumps({'format': 1}),
    json.dumps({'format': 1, 'launches': [], 'extra': 1}),
    json.dumps({'format': 1, 'launches': [{'record': '', 'from': 0, 'facts': FACTS.to_wire()}]}),
    json.dumps({'format': 1, 'launches': [{'record': 'a', 'from': -1, 'facts': FACTS.to_wire()}]}),
    json.dumps({'format': 1, 'launches': [{'record': 'a', 'from': 0, 'facts': {'runtime': None}}]}),
    json.dumps(
      {
        'format': 1,
        'launches': [{'record': 'a', 'from': 0, 'facts': {**FACTS.to_wire(), 'summoned': 'no'}}],
      }
    ),
  ],
)
def test_a_malformed_file_is_refused_naming_it(tmp_path, content):
  (tmp_path / FILENAME).write_text(content)
  with pytest.raises(LaunchFactsError, match=str(tmp_path / FILENAME)):
    Launches.load(tmp_path)


def test_the_baseline_is_the_newest_entry_below_the_extent(tmp_path):
  launches = Launches.load(tmp_path)
  for record, start in (('t', 0), ('other', 0), ('t', 5), ('t', 5), ('t', 9)):
    launches = launches.register(record, start, FACTS)
  assert launches.baseline('t', 9) == 3
  assert launches.baseline('t', 10) == 4
  assert launches.baseline('t', 5) == 0
  assert launches.baseline('t', 0) is None
  assert launches.baseline('missing', 100) is None


def test_collect_reads_the_session_it_runs_in(monkeypatch, tmp_path):
  publish(monkeypatch, ['eyebro'], permits=['bro for *'])
  monkeypatch.setenv(summon.SUMMONED_ENV, '1')
  monkeypatch.setenv(BROKER_TALK, 'worker.say,owner.say')
  registry = {
    'github': credentials.CredentialKind('github', 'github'),
    'mu': credentials.CredentialKind('mu', 'mu'),
    'brave': credentials.CredentialKind('brave', 'brave'),
  }
  store_directory = tmp_path / 'store'
  (store_directory / 'creds').mkdir(parents=True)
  (store_directory / 'creds' / 'github+eyebro.cred').write_text('token')
  (store_directory / 'creds' / 'mu.cred').write_text('token')
  (store_directory / 'creds.json').write_text(json.dumps({'defaults': ['github+eyebro']}))
  store = credentials.Store(registry, store_directory, {})
  with credentials.as_default_store(store):
    facts = LaunchFacts.collect(
      runtime='b3886e68a777', recipe=bind_recipe('echo:echo'), hold='unattended'
    )
  assert facts == LaunchFacts(
    runtime='b3886e68a777',
    llm='echo:echo',
    hold='unattended',
    summoned=True,
    talk=('owner.say', 'worker.say'),
    may_summon=('@eyebro',),
    permits=('bro for *',),
    may_give=(),
    credentials=('github+eyebro', 'mu+'),
  )
