import email.message
import io
import json
import urllib.error
from typing import Any

import pytest

from bro.base import retry
from bro.extra.github import api
from bro.extra.github.trust import Trust


def _item(login='writer', association='NONE') -> dict[str, Any]:
  return {'user': {'login': login}, 'author_association': association}


class _Response(io.BytesIO):
  headers = email.message.Message()

  def __enter__(self):
    return self


class TestTrust:
  def test_named_accounts_and_owner_need_no_lookup(self, monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
      pytest.fail('an explicitly trusted account or OWNER caused a permission lookup')

    monkeypatch.setattr(api, 'get', unexpected)
    trust = Trust('x', 'y')
    path = tmp_path / 'trust'
    path.write_text('\nReviewer[bot]\n\n')
    trust.read(path)
    assert trust.trusted(_item('reviewer[bot]'), 'token')
    assert trust.trusted(_item('owner', 'OWNER'), 'token')

  @pytest.mark.parametrize(
    'permission, trusted',
    [
      ('write', True),
      ('admin', True),
      ('maintain', True),
      ('read', False),
      ('triage', False),
      ('none', False),
    ],
  )
  def test_permission_is_read_once_per_login(self, monkeypatch, permission, trusted):
    calls = []

    def get(url, token, **kwargs):
      calls.append((url, token))
      return {'permission': permission}

    monkeypatch.setattr(api, 'get', get)
    trust = Trust('x', 'y')
    assert trust.trusted(_item('writer'), 'first-token') is trusted
    assert trust.trusted(_item('WRITER'), 'fresh-token') is trusted
    assert calls == [
      ('https://api.github.com/repos/x/y/collaborators/writer/permission', 'first-token')
    ]
    trust.trusted(_item('another'), 'fresh-token')
    assert len(calls) == 2

  @pytest.mark.parametrize('status', [403, 404])
  def test_a_refusal_is_cached_without_retries_or_failure_reports(self, monkeypatch, status):
    calls = []
    observed = []

    def refused(request, **kwargs):
      calls.append(request)
      raise urllib.error.HTTPError(
        request.full_url, status, 'refused', email.message.Message(), None
      )

    def unexpected_sleep(seconds):
      pytest.fail('a refused permission lookup was retried')

    monkeypatch.setattr(api.urllib.request, 'urlopen', refused)
    monkeypatch.setattr(retry, 'sleep', unexpected_sleep)
    trust = Trust('x', 'y')
    with api.observing_failures(observed.append):
      assert not trust.trusted(_item(), 'token')
      assert not trust.trusted(_item(), 'token')
    assert len(calls) == 1
    assert observed == []
    assert trust.trusted(_item(association='OWNER'), 'token')

  def test_a_transient_lookup_failure_is_not_cached(self, monkeypatch):
    answers = iter([urllib.error.URLError('offline'), {'permission': 'write'}])

    def get(*args, **kwargs):
      answer = next(answers)
      if isinstance(answer, Exception):
        raise answer
      return answer

    monkeypatch.setattr(api, 'get', get)
    trust = Trust('x', 'y')
    with pytest.raises(urllib.error.URLError):
      trust.trusted(_item(), 'token')
    assert trust.trusted(_item(), 'token')

  def test_malformed_permissions_fail_fast(self, monkeypatch):
    monkeypatch.setattr(api, 'get', lambda *args, **kwargs: {})
    with pytest.raises(KeyError):
      Trust('x', 'y').trusted(_item(), 'token')

  def test_real_permission_response_reaches_the_predicate(self, monkeypatch):
    monkeypatch.setattr(
      api.urllib.request,
      'urlopen',
      lambda *args, **kwargs: _Response(json.dumps({'permission': 'write'}).encode()),
    )
    assert Trust('x', 'y').trusted(_item(), 'token')
