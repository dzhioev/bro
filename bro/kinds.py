"""The context passed to contributed broker-kind factories."""

from dataclasses import dataclass
from pathlib import Path

from bro.worker_types import ArtifactDenied, ArtifactResolver, tree_path


@dataclass(frozen=True)
class KindContext:
  workspace_tree: Path
  artifacts: ArtifactResolver
  credential_scope: frozenset[str]


__all__ = ['ArtifactDenied', 'KindContext', 'tree_path']
