#!/usr/bin/env python
from bro.extra.github import pulls


class TestReviewDecision:
  def _graphql(self, monkeypatch, decision):
    calls: list[dict] = []

    def fake(query, token, description, **variables):
      calls.append(variables)
      return {'repository': {'pullRequest': {'reviewDecision': decision}}}

    monkeypatch.setattr(pulls.api, 'graphql', fake)
    return calls

  def test_reads_the_decision_off_the_pull_request(self, monkeypatch):
    calls = self._graphql(monkeypatch, 'REVIEW_REQUIRED')
    assert pulls.review_decision('owner', 'repo', 7, 't') == 'REVIEW_REQUIRED'
    assert calls == [{'owner': 'owner', 'repo': 'repo', 'pr': 7}]

  def test_a_base_asking_for_no_review_answers_none(self, monkeypatch):
    self._graphql(monkeypatch, None)
    assert pulls.review_decision('owner', 'repo', 7, 't') is None
