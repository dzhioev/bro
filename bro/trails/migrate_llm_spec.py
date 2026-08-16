#!/usr/bin/env python
"""bring recorded trail headers onto the current `LLMSpec` vocabulary.

A header's `native.llm` is the launch recipe its writer recorded, and the
vocabulary has moved under some of them: bro trails written before the provider
rename carry `type: chat_gpt`, and claude trails written before Claude Code
became a provider recipe carry no `type` at all — so `LLMSpec.from_dict` reads
neither.

This walks every header, decides a replacement per the rules below, and applies
it through the manifested repair (`TrailsClient.repair_llm_spec`), which is
conditional on the value this script read — so a re-run after a partial pass
skips what already landed instead of clobbering it.

A recipe carrying no model is left alone: there is nothing to migrate to, and
inventing one would record a claim no writer made. For the same reason a claude
recipe gains only its `type` — `fast_mode` was never written, and `from_dict`
already defaults it.
"""

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

import bro.base.args as base_args
from bro.base import log
from bro.llm.llms.claude_code import LLMSpec as ClaudeCodeSpec
from bro.llm.llms.openai import LLMSpec as OpenAISpec
from bro.trails.network import HTTPStatusError
from bro.trails.store import default_store

__cli_name__ = 'migrate-trail-llm-spec'

# the discriminator each retired spelling maps to. `chat_gpt` was the OpenAI
# provider's `TYPE` before it took the vendor's own one-word name.
_RETIRED_TYPES = {'chat_gpt': OpenAISpec.TYPE}


def replacement_for(harness: str, recipe: object) -> Optional[dict]:
  """the `native.llm` this recipe should become, or None when it is already
  current or carries too little to migrate."""
  if not isinstance(recipe, dict) or 'model' not in recipe:
    return None
  recorded_type = recipe.get('type')
  if recorded_type is None:
    # a claude session's recipe predates Claude Code being a provider: the
    # harness is what it ran under, so the discriminator is not a guess
    if harness == 'claude':
      return {**recipe, 'type': ClaudeCodeSpec.TYPE}
    return None
  current = _RETIRED_TYPES.get(recorded_type)
  return None if current is None else {**recipe, 'type': current}


@runtime_checkable
class Store(Protocol):
  """the two store calls a pass makes. The repair is an administration
  surface, which only a backend that hosts one offers."""

  def iter_trails(self) -> Iterator[dict]: ...

  def repair_llm_spec(self, trail_id: str, expected, replacement: dict) -> dict: ...


@dataclass
class Tally:
  scanned: int = 0
  migrated: int = 0
  current: int = 0
  skipped: int = 0
  conflicts: int = 0
  failures: list[str] = field(default_factory=list)

  def report(self) -> str:
    lines = [
      f'scanned    {self.scanned}',
      f'migrated   {self.migrated}',
      f'current    {self.current}',
      f'skipped    {self.skipped}  (no model to migrate)',
      f'conflicts  {self.conflicts}  (changed underneath the read; re-run to pick up)',
    ]
    if len(self.failures) > 0:
      lines.append(f'failed     {len(self.failures)}')
      lines.extend(f'  {failure}' for failure in self.failures)
    return '\n'.join(lines)


def _headers(client: Store, limit: Optional[int]) -> Iterator[dict]:
  for index, header in enumerate(client.iter_trails()):
    if limit is not None and index >= limit:
      return
    yield header


def migrate(client: Store, *, apply: bool, limit: Optional[int]) -> Tally:
  tally = Tally()
  for header in _headers(client, limit):
    tally.scanned += 1
    trail_id = header['id']
    harness = header.get('harness', '')
    recipe = (header.get('native') or {}).get('llm')
    replacement = replacement_for(harness, recipe)
    if replacement is None:
      if isinstance(recipe, dict) and 'model' in recipe:
        tally.current += 1
      else:
        tally.skipped += 1
      continue
    if not apply:
      log.info('%s (%s): %s -> %s', trail_id, harness, json.dumps(recipe), json.dumps(replacement))
      tally.migrated += 1
      continue
    try:
      client.repair_llm_spec(trail_id, recipe, replacement)
    except HTTPStatusError as error:
      if error.status == 409:
        # the header moved between the read and the write; the repair refused
        # rather than overwrite, and a later pass reads the new value
        log.warning('%s: %s', trail_id, error)
        tally.conflicts += 1
        continue
      tally.failures.append(f'{trail_id}: {error}')
      continue
    tally.migrated += 1
  return tally


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    description='rewrite recorded trail headers onto the current LLMSpec vocabulary'
  )
  parser.add_argument(
    '--apply',
    action='store_true',
    help='perform the rewrites; without it the run only reports what it would change',
  )
  parser.add_argument(
    '--limit', type=int, default=None, metavar='N', help='stop after N headers (for a trial run)'
  )
  args = parser.parse(argv)
  with default_store() as client:
    if not isinstance(client, Store):
      log.error('the configured trails backend has no administration surface')
      return 1
    tally = migrate(client, apply=args['apply'], limit=args['limit'])
  print(tally.report())
  if not args['apply']:
    print('\ndry run — pass --apply to perform the rewrites', file=sys.stderr)
  return 1 if len(tally.failures) > 0 else 0
