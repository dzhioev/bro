"""the contract a contributed broker kind is built against.

An installed distribution serves a request kind on every managed session's
host broker through the `bro.broker_kinds` entry-point group; each entry
targets a factory `(context: KindContext) -> RequestHandler` (loading:
`ride/ride/kinds.py`). The contract lives core-side so a contributing
distribution needs no ride import: the context carries the session's workspace
tree, artifact resolver, and bounded credential scope, and `tree_path` is the validation for any path
a peer names relative to that tree.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.runtime import Peer


class ArtifactDenied(Exception):
  """an artifact operation the store refuses. The message is the reason a
  handler folds into its `result{denied}`; a sharing denial is uniform, telling
  a requester nothing about whether the ref exists."""


class ArtifactResolver(Protocol):
  """host-side artifact resolution for kind handlers (the session store,
  `ride/ride/artifacts.py`, implements it)."""

  def resolve(self, ref: str, context: 'Dispatcher', requester: 'Peer') -> Path:
    """the host path holding `ref`'s content, checked against the requesting
    peer's reach; raises `ArtifactDenied` when the peer may not read it. The
    peer is attributed through the dispatcher's live tables, so a handler
    passes its own `context` through."""
    ...


def tree_path(tree: Path, relative: str) -> Path:
  """resolve the workspace-relative path a peer named against `tree`, raising
  `ValueError` when it is absolute or escapes the tree — resolved first, so a
  symlink escape falls out together with `../`."""
  if Path(relative).is_absolute():
    raise ValueError(f'{relative!r} must be a path relative to the workspace root')
  resolved = (tree / relative).resolve()
  if not resolved.is_relative_to(tree.resolve()):
    raise ValueError(f'{relative!r} escapes the workspace')
  return resolved


@dataclass(frozen=True)
class KindContext:
  """Host capabilities available to a contributed kind factory."""

  workspace_tree: Path
  artifacts: ArtifactResolver
  credential_scope: frozenset[str]
