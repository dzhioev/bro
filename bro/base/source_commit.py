import importlib.metadata
import json
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# the `.dist-info` file a wheel built from a git checkout records that checkout's commit in
RECORD_NAME = 'source_commit.json'


@dataclass(frozen=True)
class SourceCommit:
  commit: str
  # the tree the code came from differed from `commit`
  modified: bool

  def to_json(self) -> str:
    return json.dumps({'commit': self.commit, 'modified': self.modified}, sort_keys=True)

  @classmethod
  def from_json(cls, text: str) -> 'SourceCommit':
    record = json.loads(text)
    if (
      not isinstance(record, dict)
      or not isinstance(commit := record.get('commit'), str)
      or len(commit) == 0
      or not isinstance(modified := record.get('modified'), bool)
    ):
      raise ValueError(f'malformed {RECORD_NAME}: {text!r}')
    return cls(commit, modified)


def checkout_commit(directory: Path) -> Optional[SourceCommit]:
  """the commit checked out at `directory`, modified when the working tree under `directory`
  differs from it, untracked files included; None when `directory` is in no git checkout with a
  commit."""
  head = subprocess.run(
    ['git', 'rev-parse', '--verify', 'HEAD'], cwd=directory, capture_output=True, text=True
  )
  if head.returncode != 0:
    return None
  status = subprocess.run(
    ['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=normal', '--', '.'],
    cwd=directory,
    capture_output=True,
    text=True,
    check=True,
  )
  return SourceCommit(head.stdout.strip(), modified=len(status.stdout) > 0)


def _editable_source(direct_url: dict) -> Path:
  url = urllib.parse.urlparse(direct_url['url'])
  if url.scheme != 'file':
    raise ValueError(f'editable installation from a non-local URL: {direct_url["url"]!r}')
  source = Path(urllib.request.url2pathname(url.path))
  subdirectory = direct_url.get('subdirectory')
  return source if subdirectory is None else source / subdirectory


def installed_commit(distribution: importlib.metadata.Distribution) -> Optional[SourceCommit]:
  """the commit the installed `distribution` runs: the one its wheel recorded, the one its
  version-control installation resolved, or the live checkout its editable installation runs
  from; None when its installation records none."""
  recorded = distribution.read_text(RECORD_NAME)
  if recorded is not None:
    return SourceCommit.from_json(recorded)
  text = distribution.read_text('direct_url.json')
  if text is None:
    return None
  direct_url = json.loads(text)
  if (vcs_info := direct_url.get('vcs_info')) is not None:
    return SourceCommit(vcs_info['commit_id'], modified=False)
  if direct_url.get('dir_info', {}).get('editable', False):
    return checkout_commit(_editable_source(direct_url))
  return None
