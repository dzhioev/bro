"""Publish broker authority views to in-process surface tests."""

from bro import authority_view as client


def publish(monkeypatch, targets=(), *, permits=(), may_give=()):
  view = {
    'may_summon': tuple(sorted(targets)),
    'summon_entries': tuple('@' + target for target in sorted(targets)),
    'permits': tuple(permits),
    'may_give': tuple(may_give),
  }
  monkeypatch.setattr(client, 'read', lambda: view)
  monkeypatch.setattr(client, 'initial', lambda: view)
  return view
