"""The repository attachment type."""

import contextlib
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

from bro.base import log
from bro.base.git_url import git_url_path, is_git_url
from bro.registry import create_bro
from bro.workspace.git import resolve_head, resolve_ref, rev_parse_commit
from bro.workspace.human import HUMAN_EMAIL_ENV, HUMAN_NAME_ENV
from bro.workspace.repository import repository_config
from ride.attachments import Attachment, AttachmentRequest, AttachmentType, ResolvedAttachment
from ride.identity import human_git_identity_env
from ride.repository import (
  Repository,
  as_repository,
  clean_managed_mirrors,
  hold_repository,
  open_repository,
  recorded_origin_url,
)
from ride.workspace.clones import ensure_clone


def repository_attachment(repository: Repository | Path, workspace: str) -> Attachment:
  repository = as_repository(repository)
  name = (
    git_url_path(repository.identity).rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')
    if is_git_url(repository.identity)
    else Path(repository.identity).name
  )
  return Attachment('repo', name, {'repo': repository.identity, 'branch': f'workspace-{workspace}'})


class RepositoryAttachment(AttachmentType):
  name = 'repo'

  def validate(self, attachment: Attachment) -> None:
    if attachment.fields.keys() != {'repo', 'branch'} or any(
      not isinstance(value, str) or not value for value in attachment.fields.values()
    ):
      raise ValueError('repo attachment requires non-empty repo and branch fields')

  @contextlib.contextmanager
  def resolve(self, request: AttachmentRequest, workspace: str) -> Iterator[ResolvedAttachment]:
    if not {'repo'} <= request.fields.keys() or not request.fields.keys() <= {
      'repo',
      'into',
      'owner',
    }:
      raise ValueError('invalid repo attachment request fields')
    if not isinstance(request.fields['repo'], str) or not request.fields['repo']:
      raise ValueError('repo attachment request requires a repository identity')
    for key in ('into', 'owner'):
      value = request.fields.get(key)
      if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f'repo attachment request has invalid {key}')
    identity = request.fields['repo']
    into = request.fields.get('into')
    owner = request.fields.get('owner')
    with hold_repository(identity) as repository:
      if into is not None:
        base = resolve_ref(repository.git_dir, into)
        if base is None:
          raise ValueError(f'cannot resolve into ref {into!r}')
      elif owner is not None:
        base = resolve_head(repository.git_dir, Path(owner))
        if base is None:
          raise ValueError(f"cannot read the summoner's HEAD at {owner}")
      else:
        base = repository.default_base or rev_parse_commit(repository.git_dir, 'HEAD')
        if base is None:
          raise ValueError(f'cannot read HEAD of {repository.git_dir}')
      yield ResolvedAttachment(repository_attachment(repository, workspace), base)

  def launch_arguments(self, attachment: Attachment) -> tuple[str, ...]:
    return ('--repo', attachment.fields['repo'])

  def identity(self, attachment: Attachment) -> object:
    return attachment.fields['repo']

  def prepare(
    self, attachment: Attachment, directory: Path, workspace: Path, base: str | None
  ) -> None:
    ensure_clone(
      open_repository(attachment.fields['repo']),
      directory,
      attachment.fields['branch'],
      base,
      staging_parent=workspace,
    )

  def environment(self, attachment: Attachment, directory: Path) -> dict[str, str | None]:
    base = rev_parse_commit(directory, 'HEAD')
    if base is None:
      raise RuntimeError(f'cannot read HEAD of clone {directory}')
    return {
      HUMAN_EMAIL_ENV: None,
      HUMAN_NAME_ENV: None,
      'RIDE_REPO_URL': recorded_origin_url(directory),
      'RIDE_REPO': attachment.fields['repo'],
      'RIDE_BRANCH': attachment.fields['branch'],
      'RIDE_BASE_SHA': base,
      **human_git_identity_env(open_repository(attachment.fields['repo'])),
    }

  def start_session(self, attachment: Attachment, directory: Path, bro: str) -> None:
    command = repository_config(directory).provision_command
    if command is not None:
      result = subprocess.run(
        ['sh', '-c', command],
        cwd=directory,
        stdin=subprocess.DEVNULL,
        stdout=sys.stderr,
        stderr=sys.stderr,
      )
      if result.returncode != 0:
        raise RuntimeError(
          f'provisioning command failed with exit status {result.returncode}: {command}'
        )
    create_bro(bro).provision_workspace(directory)

  def inherit(self, attachment: Attachment, directory: Path, into: str | None) -> AttachmentRequest:
    return AttachmentRequest(
      self.name, {'repo': attachment.fields['repo'], 'owner': str(directory), 'into': into}
    )

  def _missing(self, attachment: Attachment) -> str | None:
    try:
      open_repository(attachment.fields['repo'])
    except (RuntimeError, ValueError):
      return f'attached repository no longer exists: {attachment.fields["repo"]}'
    return None

  def cleanliness(
    self, attachment: Attachment, directory: Path, session_end: tuple[bool, list[str]]
  ) -> tuple[bool, list[str]]:
    missing = self._missing(attachment)
    return (False, [missing]) if missing is not None else session_end

  def remove(self, attachment: Attachment, directory: Path, *, force: bool) -> None:
    missing = self._missing(attachment)
    if missing is not None and not force:
      raise RuntimeError(f'{missing}; use --force to remove')

  def summary(self, attachment: Attachment) -> str:
    return attachment.fields['repo']

  def clean_shared(self, attachments: Sequence[Attachment], *, dry_run: bool) -> None:
    removed, skipped = clean_managed_mirrors(
      {attachment.fields['repo'] for attachment in attachments}, dry_run=dry_run
    )
    log.info(
      '%s %d managed mirror(s), skipped %d referenced',
      'would clean' if dry_run else 'cleaned',
      removed,
      skipped,
    )


repo = RepositoryAttachment()
