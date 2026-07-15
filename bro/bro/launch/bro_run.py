"""a bro run as a container launch, described once.

A bro run is the bro's LLM process in its own throwaway cw-style container:
`<cli> <bro> … --in-place` executing against the bro's own credential scope,
committing as the bro git identity, based on a caller-resolved git ref. This
module owns that description — inner command, container environment, credential
scope, docker-socket decision, workspace naming — so every surface that spawns
one computes it identically; executing the launch (attached TTY, supervised
non-TTY child) is the caller's.
"""

import secrets
from collections.abc import Sequence
from typing import Optional

from cw.constants import bro_git_identity_env
from cw.docker import Launch
from cw.paths import _containers_dir, _project_root, _worktrees_dir
from cw.secrets import Surface, scoped_secrets


def describe(
  bro_name: str,
  inner_args: Sequence[str],
  *,
  workspace_name: str,
  cli_name: str = 'ask',
  base_ref: Optional[str] = None,
  trails: bool = True,
  tty: bool = True,
  forward_env: bool = True,
) -> Launch:
  """describe the launch of `<cli_name> <bro_name> <inner_args…> --in-place`.

  `base_ref` is a caller-resolved commit sha the container's workspace clone
  bases on (`CW_BASE_REF`); None leaves the entrypoint's HEAD fallback — the
  host checkout's current commit. `trails=False` disables run recording: the
  `trails` secret leaves the scope and `TRAILS_DISABLED` rides in the env.
  """
  scoped = scoped_secrets(bro_name, Surface.BRO_RUN)
  required = set(scoped.required)
  env = dict(bro_git_identity_env())
  env['CW_BRO'] = bro_name
  if base_ref is not None:
    env['CW_BASE_REF'] = base_ref
  if not trails:
    required.remove('trails')
    env['TRAILS_DISABLED'] = '1'
  return Launch(
    name=workspace_name,
    command=[cli_name, bro_name, *inner_args, '--in-place'],
    env=env,
    secrets=required,
    optional_secrets=set(scoped.optional),
    docker_sock=scoped.docker_sock,
    tty=tty,
    forward_env=forward_env,
  )


def fresh_workspace_name(base: str) -> str:
  """mint a workspace name absent from both local workspace namespaces."""
  project = _project_root()
  worktrees = _worktrees_dir(project)
  containers = _containers_dir(project)
  while True:
    name = f'{base}-{secrets.token_hex(4)}'
    if not (worktrees / name).exists() and not (containers / name).exists():
      return name
