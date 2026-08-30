"""bro-side run-lifecycle emission over the broker channel.

`BroChannel` is the bro framework's consumer of the broker substrate: the
answering half of a worker peer, emitting `progress{trail_id}` once
the trail is open and the run's closing `result` when it ends — both correlated
to the quest id the launch delivered beside the channel (`QUEST_ENV`).
`from_env()` mirrors `Client.from_env()`: `None` when `BROKER_CHANNEL` is unset,
so the hook is inert where there is no channel; a channel without a quest id
is a mis-provisioned launch and raises. The broker imports are deferred to
runtime and `from_env()` also returns `None` when the `broker` package is not
importable (an environment provisioned before broker existed), so importing
`bro` never depends on broker being installed.

Wired into `Runner.run` (LLM-process children), and into `Runner.send`'s first
turn for a *summoned* interactive conversation — a manual summon child on the
bro harness announces `started` there, and its result is the `answer` service
tool's; the host attributes every peer itself, so the announcement carries
nothing about where the run lives. An un-summoned interactive conversation emits nothing: its channel is
the enclosing session's, not its own. A claude-code `--raw` session never calls
`Runner.run` — it has no in-process return value — so it auto-emits no
lifecycle, by design. The claude-session emissions are the `raise` and `answer`
service tools' results, sent mid-session; the result closes the session's own
quest while its channel keeps serving.
"""

import os
from typing import TYPE_CHECKING, Any, Optional

from bro.llm.tracker import EndReason

if TYPE_CHECKING:
  from bro.broker.client import Client


class BroChannel:
  def __init__(self, client: 'Client', quest: str):
    self._client = client
    self._quest = quest

  @classmethod
  def from_env(cls) -> Optional['BroChannel']:
    try:
      from bro.broker.client import QUEST_ENV, Client
    except ImportError:
      return None
    client = Client.from_env()
    if client is None:
      return None
    quest = os.environ.get(QUEST_ENV)
    if quest is None:
      client.close()
      raise ValueError(f'broker channel present but {QUEST_ENV} unset; the launch did not name the quest this run answers')  # fmt: skip
    return cls(client, quest)

  def started(self, trail_id: str) -> None:
    """announce the run's trail."""
    self._client.progress(self._quest, {'trail_id': trail_id})

  def completed(self, result: Optional[str], end_reason: EndReason) -> None:
    # `result` is None only when `run()` unwinds without any of its three
    # end-reason paths assigning one (a BaseException such as cancellation).
    payload: dict[str, Any]
    if end_reason == 'ok':
      payload = {'outcome': 'ok'}
      if result is not None:
        payload['value'] = result
    else:
      payload = {'outcome': 'failed', 'detail': {'reason': end_reason}}
      if result is not None:
        payload['error'] = result
    self._client.result(self._quest, payload)

  def close(self) -> None:
    # the run's result is typically this process's last act before exit;
    # confirm it was consumed rather than racing the exit (ClientTransport.close)
    self._client.close(confirm=True)
