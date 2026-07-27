"""a bro run as a container launch, described once.

A bro run is the bro's LLM process in its own throwaway cw-style container:
`bro <verb> <bro> … --in-place` executing against a caller-resolved credential
scope, committing as the bro git identity, based on a caller-resolved git ref.
This module owns that description — inner command, container environment,
stdio and docker knobs — so every surface that spawns one composes it
identically; resolving the scope and executing the launch (attached TTY,
supervised non-TTY child) are the caller's.
"""

import json
from collections.abc import Sequence
from typing import Any, Literal, Optional

from base import credentials
from bro.launch.identity import bro_git_identity_env
from bro.summon import SUMMONER_ENV
from workspace.docker import Launch
from workspace.store import ScopedSecrets


def describe(
  bro_name: str,
  inner_args: Sequence[str],
  *,
  workspace_name: str,
  verb: Literal['run', 'chat'],
  scoped: ScopedSecrets,
  base_ref: Optional[str] = None,
  trails: bool = True,
  tty: bool = True,
  forward_env: bool = True,
  summoner: Optional[dict[str, Any]] = None,
) -> Launch:
  """describe the launch of `bro <verb> <bro_name> <inner_args…> --in-place`.

  `scoped` is the run's credential scope, applied as given — the caller resolves
  it, overrides included. `base_ref` is a caller-resolved commit sha the
  container's workspace clone bases on (`CW_BASE_REF`); None leaves the
  entrypoint's HEAD fallback — the host checkout's current commit.
  `trails=False` disables run recording: the trails secret leaves the scope and
  `TRAILS_DISABLED` rides in the env.
  """
  required = set(scoped.required)
  env = dict(bro_git_identity_env(bro_name))
  env['CW_BRO'] = bro_name
  if base_ref is not None:
    env['CW_BASE_REF'] = base_ref
  if not trails:
    # by kind, not literal name: the scope may carry a trails instance variant
    required = {name for name in required if credentials.parse_name(name)[0] != 'trails'}
    env['TRAILS_DISABLED'] = '1'
  if summoner is not None:
    env[SUMMONER_ENV] = json.dumps(summoner, ensure_ascii=False, separators=(',', ':'))
  return Launch(
    name=workspace_name,
    command=['bro', verb, bro_name, *inner_args, '--in-place'],
    env=env,
    secrets=required,
    optional_secrets=set(scoped.optional),
    docker_sock=scoped.docker_sock,
    tty=tty,
    forward_env=forward_env,
  )
