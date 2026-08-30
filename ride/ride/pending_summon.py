"""pending manual summons: the host-side records a launch token resolves to.

A manual summon leaves the host with an expectation (a provisioned broker
channel awaiting an external child) and the user with a token (the request id).
This module is the bridge between them: `SummonControl` writes one record per
registered manual summon under `<runtime-root>/summon/pending/<token>.json`,
and the user's `ride along --summoned <token>` launch reads it back — the
channel to attach to, the broker protocol revision, the authorized child shape (target, allow-list, scope overrides), the prompt, and the base-ref inheritance source.

`claim` is one-shot: exactly one launch may attach to the channel (a second
connection would supersede the first on it), so the unlink decides a
race — read as much as you like (`peek`) while preflighting, claim last, right
before the session starts. The claim leaves a second record behind, under
`claimed/<token>.json`: the workspace name the claiming launch runs the child
in, which is how the broker attributes the manual peer (`ride/ride/peers.py`).
The host discards both records when the summon ends (a denial, root teardown),
so a stale token fails the launch loudly.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from bro.workspace.paths import is_workspace_name, summon_dir


class UnknownToken(Exception):
  """no pending manual summon behind the token: never registered, already
  claimed by another launch, or its summon ended. The message is user-facing."""


@dataclass(frozen=True)
class PendingSummon:
  """one registered manual summon, keyed by its token (the request id)."""

  token: str
  protocol_revision: int
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


def _claimed_path(token: str) -> Path:
  return summon_dir() / 'claimed' / f'{token}.json'


def write(pending: PendingSummon) -> None:
  path = _path(pending.token)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(asdict(pending), ensure_ascii=False, indent=2))


def peek(token: str) -> PendingSummon:
  """read and validate the token's record without claiming it."""
  try:
    data = json.loads(_path(token).read_text())
  except FileNotFoundError:
    raise UnknownToken(
      f'no pending manual summon for token {token!r}: never registered, '
      'already claimed, or its summon ended'
    ) from None
  if not isinstance(data, dict):
    raise ValueError(f'pending manual summon {token!r} is not a JSON object')
  from bro.broker.brotocol import PROTOCOL_REVISION

  if 'protocol_revision' not in data:
    raise ValueError(
      f'pending manual summon {token!r} has no broker protocol revision; '
      're-mint the token from a session on this installation'
    )
  record_revision = data['protocol_revision']
  if (
    isinstance(record_revision, bool)
    or not isinstance(record_revision, int)
    or record_revision != PROTOCOL_REVISION
  ):
    raise ValueError(
      f'pending manual summon {token!r} uses broker protocol revision {record_revision!r}, '
      f'but this installation uses {PROTOCOL_REVISION}; re-mint the token from a matching release'
    )
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


def claim(token: str, *, workspace: str) -> PendingSummon:
  """read and consume the token's record — the unlink decides a race, so exactly
  one caller gets it — and record `workspace`, the name the claiming launch
  runs the child in, as the token's claimed record. Raises `UnknownToken` when
  there is nothing to claim."""
  pending = peek(token)
  try:
    _path(token).unlink()
  except FileNotFoundError:
    raise UnknownToken(
      f'pending manual summon {token!r} was just claimed by another launch'
    ) from None
  claimed = _claimed_path(token)
  claimed.parent.mkdir(parents=True, exist_ok=True)
  claimed.write_text(json.dumps({'token': token, 'workspace': workspace}, ensure_ascii=False))
  return pending


def claimed_workspace(token: str) -> Optional[str]:
  """the workspace name the token's claiming launch recorded, or None while the
  token is unclaimed."""
  try:
    data = json.loads(_claimed_path(token).read_text())
  except FileNotFoundError:
    return None
  workspace = data.get('workspace')
  if not isinstance(workspace, str) or not is_workspace_name(workspace):
    raise ValueError(f'claimed summon record {token!r} carries no usable workspace name')
  return workspace


def discard(token: str) -> None:
  """drop the token's records if any still exist — the host's cleanup when a
  manual summon ends."""
  _path(token).unlink(missing_ok=True)
  _claimed_path(token).unlink(missing_ok=True)
