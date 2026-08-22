"""pending manual summons: the host-side record a launch token resolves to.

A manual summon leaves the host with an expectation (a provisioned broker
channel awaiting an external child) and the user with a token (the request id).
This module is the bridge between them: `SummonControl` writes one record per
registered manual summon under `<runtime-root>/summon/pending/<token>.json`,
and the user's `ride along --summoned <token>` launch reads it back — the
channel to attach to, the authorized child shape (target, allow-list, scope
overrides), the prompt, and the base-ref inheritance source.

`claim` is one-shot: exactly one launch may attach to the channel (a second
connection would supersede the first on it), so the unlink decides a
race — read as much as you like (`peek`) while preflighting, claim last, right
before the session starts. The host discards the record when the summon ends
unclaimed (a denial, root teardown), so a stale token fails the launch loudly.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from bro.workspace.paths import summon_dir


class UnknownToken(Exception):
  """no pending manual summon behind the token: never registered, already
  claimed by another launch, or its summon ended. The message is user-facing."""


@dataclass(frozen=True)
class PendingSummon:
  """one registered manual summon, keyed by its token (the request id)."""

  token: str
  port: int  # the provisioned broker channel: the host's listening port
  channel_token: str  # and the token that attaches to this summon's channel on it
  target: str
  prompt: str
  parent_workspace: str  # the summoner's tree — the default base-ref source
  may_summon: tuple[str, ...]  # the child's own resolved allow-list
  grant: tuple[str, ...]  # the request's scope overrides, applied at launch
  revoke: tuple[str, ...]
  summoner: Optional[dict[str, Any]]  # the child's summoned_by provenance
  repo: Optional[str] = None  # attachment identity inherited from the root session
  into: Optional[str] = None  # unresolved ref overriding the parent-HEAD base

  def address(self, host: Optional[str] = None) -> str:
    """the channel address for a child that reaches the summoner's host at
    `host`, or beside it on loopback when none is named."""
    # function-local like the rest of the launch path: a record is read while
    # preflighting, before the broker gate (ride/ride/workspace/AGENTS.md)
    from bro.broker.transports.tcp import LOCAL_HOST, Endpoint

    return Endpoint(port=self.port, token=self.channel_token).address(host or LOCAL_HOST)


def _path(token: str) -> Path:
  return summon_dir() / 'pending' / f'{token}.json'


def write(pending: PendingSummon) -> None:
  path = _path(pending.token)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(asdict(pending), ensure_ascii=False, indent=2))


def peek(token: str) -> PendingSummon:
  """read the token's record without claiming it. Raises `UnknownToken`."""
  try:
    data = json.loads(_path(token).read_text())
  except FileNotFoundError:
    raise UnknownToken(
      f'no pending manual summon for token {token!r}: never registered, '
      'already claimed, or its summon ended'
    ) from None
  loaded = PendingSummon(
    **{
      **data,
      'may_summon': tuple(data['may_summon']),
      'grant': tuple(data['grant']),
      'revoke': tuple(data['revoke']),
    }
  )
  if loaded.token != token:
    raise ValueError(f'pending summon record {token!r} names token {loaded.token!r}')
  return loaded


def claim(token: str) -> PendingSummon:
  """read and consume the token's record — the unlink decides a race, so exactly
  one caller gets it. Raises `UnknownToken` when there is nothing to claim."""
  pending = peek(token)
  try:
    _path(token).unlink()
  except FileNotFoundError:
    raise UnknownToken(
      f'pending manual summon {token!r} was just claimed by another launch'
    ) from None
  return pending


def discard(token: str) -> None:
  """drop the token's record if it still exists — the host's cleanup when a
  manual summon ends unclaimed."""
  _path(token).unlink(missing_ok=True)
