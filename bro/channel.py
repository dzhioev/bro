"""bro-side lifecycle emission over the broker channel.

`BroChannel` is the bro framework's consumer of the broker substrate: a thin
adapter over `bro.broker.client.Client` that emits the two lifecycle events the
host dispatcher routes to the run's parent — `started{trail_id}` once the
trail is open and `completed{result, end_reason}` when the run ends.
`from_env()` mirrors `Client.from_env()`: `None` when `BROKER_CHANNEL` is
unset, so the hook is inert where there is no channel. The broker imports are
deferred to runtime and `from_env()` also returns `None` when the `broker`
package is not importable (an environment provisioned before broker existed),
so importing `bro` never depends on broker being installed.

Wired into `Runner.run` (LLM-process children), and into `Runner.send`'s first
turn for a *summoned* interactive conversation — a manual summon child on the
bro harness announces `started` there, and its terminal is the `answer` service
tool's. An un-summoned interactive conversation emits nothing: its channel is
the enclosing session's, not its own. A claude-code `--raw` session never calls
`Runner.run` — it has no in-process return value — so it auto-emits no
lifecycle, by design. The claude-session emissions are the `raise` and `answer`
service tools' `completed`, sent mid-session; the dispatcher deliberately
leaves a root un-finalized on one, so the channel keeps serving.
"""

from typing import TYPE_CHECKING, Optional

from bro.llm.tracker import EndReason

if TYPE_CHECKING:
  from bro.broker.client import Client


class BroChannel:
  def __init__(self, client: 'Client'):
    self._client = client

  @classmethod
  def from_env(cls) -> Optional['BroChannel']:
    try:
      from bro.broker.client import Client
    except ImportError:
      return None
    client = Client.from_env()
    if client is None:
      return None
    return cls(client)

  def started(self, trail_id: str, *, workspace: Optional[str] = None) -> None:
    """announce the run's trail. `workspace` names the session's workspace where
    the host cannot derive it (a manual summon child's is user-chosen; a spawned
    child's is its channel-named one)."""
    from bro.broker.brotocol import Tag

    payload: dict[str, Optional[str]] = {'trail_id': trail_id}
    if workspace is not None:
      payload['workspace'] = workspace
    self._client.send(Tag.STARTED, payload)

  def completed(self, result: Optional[str], end_reason: EndReason) -> None:
    from bro.broker.brotocol import Tag

    # `result` is None only when `run()` unwinds without any of its three
    # end-reason paths assigning one (a BaseException such as cancellation).
    self._client.send(Tag.COMPLETED, {'result': result, 'end_reason': end_reason})

  def close(self) -> None:
    # the lifecycle terminal is typically this process's last act before exit;
    # confirm it was consumed rather than racing the exit (ClientTransport.close)
    self._client.close(confirm=True)
