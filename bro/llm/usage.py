#!/usr/bin/env python
"""shared LLM-usage accounting: one usage-reading surface for every environment.

Usage is kept as per-model counts in the four token classes the Anthropic API
bills separately — they differ in price by up to ~50x, so a single summed number
would be dominated by `cache_read` and mean nothing as spend. Each class is kept
distinct:

- input        — fresh, uncached prompt tokens (full price)
- cache_write  — tokens written to the prompt cache (1.25x)
- cache_read   — tokens served from the prompt cache (0.1x); in a long agentic
                 session this dominates by volume but not by cost (re-reads of the
                 growing prefix)
- output       — generated tokens (5x)

Providers that bill differently map onto the same four classes: OpenAI reports
cached input as a subset of input, so cached tokens land in `cache_read`, the
uncached remainder in `input`, `cache_write` stays 0, and reasoning tokens stay
inside `output`.

Two cumulative-usage sources, unified by `current_usage()`:

- the usage file — the env-pointed (`BRO_USAGE_FILE`) JSON snapshot a native bro
  run's LLM loop publishes after every API call (`publish`). Written atomically
  (temp + rename) and self-describing (`{"agent": ..., "models": {slug: counts}}`):
  the reader cannot trust the environment for the agent — an in-process bro run
  inherits the launcher's `RIDE_BRO`, not its own. The first publish mints the
  path and exports the pointer, so tool subprocesses spawned afterwards inherit
  it.
- the Claude Code session transcript — the session's own segment plus the
  sidecar transcripts of the subagents it spawned (`session_transcripts`),
  summed per model across every billed assistant message (`transcript_usage`).

This module also owns the commit-footer line format (one `>`-quoted line; `'`
thousands separator so it never collides with the `, ` joining model entries):

  > created with <agents> | <model>: ↑(<input> <cache_write> <cache_read>) ↓<output>[, …]

`<agents>` is a `, `-joined list of surface identities — `Claude Code <version>`
for a Claude Code session, the usage file's agent unversioned (a bro run
publishes `bro//<name>`, e.g. `bro//dev`); a squash footer unions the
agents of the commits it aggregates.
`↑(…)` groups the upload classes (input, cache_write, cache_read, in that
order); `↓` marks output. `parse_footer` also accepts the historic shape that
compressed same-agent versions (`Claude Code 2.1.114, 2.1.120`), normalizing
bare version tokens back to full `Claude Code <version>` agents.

The `usage` CLI prints `current_usage()` — the agent line, then one per-model
entry per line. The per-commit delta/baseline machinery on top of these
cumulatives lives in `bro/workflow/commit_footer.py`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base.args import Parser
from bro.monitor import claude_config_dir, working_projects_dir

__cli_name__ = 'usage'

USAGE_FILE_VARIABLE = 'BRO_USAGE_FILE'
SESSION_ID_VARIABLE = 'CLAUDE_CODE_SESSION_ID'

_THOUSANDS = "'"
_UP = '↑'
_DOWN = '↓'

# claude code labels locally-generated assistant turns (interrupts, local errors,
# injected notices) with this sentinel model — no real API round-trip, so their
# usage is not billed spend and must not be credited to any commit.
_SYNTHETIC_MODEL = '<synthetic>'

# the four billed token classes, in footer display order, mapped to their
# Claude transcript `usage` field names.
CLASSES = ('input', 'cache_write', 'cache_read', 'output')
_FIELD_OF = {
  'input': 'input_tokens',
  'cache_write': 'cache_creation_input_tokens',
  'cache_read': 'cache_read_input_tokens',
  'output': 'output_tokens',
}

# a per-model usage record is a plain dict keyed by CLASSES (JSON-friendly for
# state files and the usage file; arithmetic via the helpers below).
Counts = dict[str, int]


def zero() -> Counts:
  return dict.fromkeys(CLASSES, 0)


def add(a: Counts, b: Counts) -> Counts:
  return {c: a.get(c, 0) + b.get(c, 0) for c in CLASSES}


def subtract(a: Counts, b: Counts) -> Counts:
  return {c: a.get(c, 0) - b.get(c, 0) for c in CLASSES}


def from_vendor_counts(raw: dict) -> Counts:
  """normalize one vendor-raw usage record into the four billed classes.

  Anthropic reports the four as disjoint fields. OpenAI reports a single
  `input_tokens` covering the whole prompt and breaks its cached and
  cache-written parts out under `input_tokens_details`, so both come off that
  total to leave the uncached remainder — the discriminator between the two
  shapes is that detail block, which Anthropic never sends.
  """
  details = raw.get('input_tokens_details')
  if details is None:
    return {c: int(raw.get(_FIELD_OF[c], 0)) for c in CLASSES}
  cache_read = int(details.get('cached_tokens', 0))
  cache_write = int(details.get('cache_write_tokens', 0))
  return {
    'input': int(raw['input_tokens']) - cache_read - cache_write,
    'cache_write': cache_write,
    'cache_read': cache_read,
    'output': int(raw.get('output_tokens', 0)),
  }


@dataclass
class Usage:
  """a surface's cumulative spend: who spent it and how much per model."""

  agent: str  # surface identity, e.g. 'Claude Code 2.1.201' or 'bro//dev'
  per_model: dict[str, Counts]  # keyed by model slug


# --- the env-pointed usage file ----------------------------------------------


def publish(agent: str, per_model: dict[str, Counts]) -> None:
  """write the full cumulative snapshot to the env-pointed usage file, atomically.

  When the pointer is absent, mints a per-process temp path and exports it, so
  tool subprocesses spawned after the first publish inherit the pointer. Each
  publish replaces the whole file — the file is a snapshot of one writer's
  totals, and a process runs one publishing LLM loop in practice.
  """
  pointer = os.environ.get(USAGE_FILE_VARIABLE)
  if pointer is None:
    pointer = str(Path(tempfile.gettempdir()) / f'bro-usage-{os.getpid()}.json')
    os.environ[USAGE_FILE_VARIABLE] = pointer
  path = Path(pointer)
  payload = json.dumps({'agent': agent, 'models': per_model}, indent=2)
  tmp = path.with_name(path.name + '.tmp')
  tmp.write_text(payload + '\n')
  tmp.replace(path)


def read_usage_file(path: Path) -> Usage:
  data = json.loads(path.read_text())
  per_model = {
    model: {c: int(counts.get(c, 0)) for c in CLASSES} for model, counts in data['models'].items()
  }
  return Usage(agent=data['agent'], per_model=per_model)


# --- Claude transcript reading ------------------------------------------------


def _session_segment() -> Optional[Path]:
  """the transcript segment of the Claude Code session owning this process.

  Claude names the segment file after the session id it exports, and a subagent
  inherits its parent's — so the id resolves the session that paid even from a
  working directory claude keeps no project dir for, such as an agent's own
  worktree.
  """
  session_id = os.environ.get(SESSION_ID_VARIABLE)
  if session_id is not None:
    segments = sorted((claude_config_dir() / 'projects').glob(f'*/{session_id}.jsonl'))
    if len(segments) > 0:
      return segments[0]
  jsonls = sorted(working_projects_dir().glob('*.jsonl'), key=lambda p: p.stat().st_mtime)
  if len(jsonls) == 0:
    return None
  return jsonls[-1]


def session_transcripts() -> list[Path]:
  """every transcript file the current Claude Code session bills through.

  The segment records only the main thread; the turns of each subagent it
  spawns land in a sidecar transcript under the segment's companion dir.
  """
  segment = _session_segment()
  if segment is None:
    return []
  return [segment, *sorted(segment.with_suffix('').rglob('*.jsonl'))]


def transcript_usage(path: Path) -> dict[str, Counts]:
  """returns {model_slug: per-class totals} summed across every billed assistant message.

  synthetic turns (model `<synthetic>`) are skipped — they carry no real API spend.
  """
  totals: dict[str, Counts] = {}
  with path.open() as f:
    for line in f:
      try:
        entry = json.loads(line)
      except json.JSONDecodeError:
        continue
      msg = entry.get('message')
      if not isinstance(msg, dict):
        continue
      u = msg.get('usage')
      if not isinstance(u, dict):
        continue
      model = msg.get('model')
      if not isinstance(model, str):
        model = 'unknown'
      if model == _SYNTHETIC_MODEL:
        continue
      totals[model] = add(totals.get(model, zero()), from_vendor_counts(u))
  return totals


def claude_version() -> str:
  # claude code exports AI_AGENT like "claude-code_2-1-181_agent"
  agent = os.environ.get('AI_AGENT')
  if agent is not None:
    m = re.match(r'claude-code_(\d+(?:-\d+)*)_', agent)
    if m is not None:
      return m.group(1).replace('-', '.')
  # fall back to a version-looking component of a versioned install path
  # (e.g. .../versions/2.1.181/claude)
  execpath = os.environ.get('CLAUDE_CODE_EXECPATH')
  if execpath is not None:
    for part in Path(execpath).parts:
      if re.match(r'^\d+\.\d+', part) is not None:
        return part
  return 'unknown'


def claude_agent() -> str:
  return f'Claude Code {claude_version()}'


# --- the unified reader --------------------------------------------------------


def current_usage() -> Optional[Usage]:
  """the session's cumulative usage: the env-pointed usage file when the pointer
  is set (bro runs), else the Claude session's transcripts, else None."""
  pointer = os.environ.get(USAGE_FILE_VARIABLE)
  if pointer is not None:
    return read_usage_file(Path(pointer))
  per_model: dict[str, Counts] = {}
  for transcript in session_transcripts():
    for model, counts in transcript_usage(transcript).items():
      per_model[model] = add(per_model.get(model, zero()), counts)
  if len(per_model) == 0:
    return None
  return Usage(agent=claude_agent(), per_model=per_model)


# --- footer formatting + parsing -----------------------------------------------


def format_int(n: int) -> str:
  return f'{n:,}'.replace(',', _THOUSANDS)


# the vendor a resolved model slug bills, by the shape of the slug. Distinct
# from the launch-time provider of `bro.llm.providers`, which answers which
# surface serves a model *name*: a claude slug bills `anthropic` whether a Claude
# Code session or the Anthropic API served it, where the launch roster would call
# the same string `claude-code`.
_VENDOR_PATTERNS = (
  (re.compile(r'^claude-'), 'anthropic'),
  (re.compile(r'^(gpt|o\d)'), 'openai'),
)


def vendor_of(slug: str) -> str:
  """the vendor that billed `slug`. Raises for a slug no known vendor claims,
  rather than folding unattributed spend into a bucket."""
  for pattern, vendor in _VENDOR_PATTERNS:
    if pattern.match(slug) is not None:
      return vendor
  raise ValueError(f'no vendor known for model {slug!r}')


def model_family(slug: str) -> str:
  """the model a resolved slug belongs to, with whatever pins one version of it
  stripped — the key that matches two snapshots of one model to each other.

  A vendor pins a version in its own way: OpenAI resolves a request to a dated
  snapshot (`gpt-5` → `gpt-5-2025-08-07`), Anthropic carries the date in the id
  (`claude-haiku-4-5-20251001`). Either way the version *of the model itself*
  survives, because Opus 4.8 and Opus 5 are different models, not two snapshots
  of one. A slug no scheme matches is its own family.
  """
  # minor version is optional: single-number families (claude-fable-5) label as
  # just the major ("Fable 5")
  m = re.match(r'^claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?', slug)
  if m is not None:
    family, major, minor = m.groups()
    version = major if minor is None else f'{major}.{minor}'
    return f'{family.title()} {version}'
  # OpenAI resolves a requested model to a dated snapshot (gpt-5 →
  # gpt-5-2025-08-07); label it by the family name
  return re.sub(r'-\d{4}-\d{2}-\d{2}$', '', slug)


def to_labels(slug_counts: dict[str, Counts]) -> dict[str, Counts]:
  """collapse model-slug-keyed counts to the families the footer labels."""
  labels: dict[str, Counts] = {}
  for slug, c in slug_counts.items():
    label = model_family(slug)
    labels[label] = add(labels.get(label, zero()), c)
  return labels


def _format_entry(label: str, c: Counts) -> str:
  return (
    f'{label}: {_UP}({format_int(c.get("input", 0))} {format_int(c.get("cache_write", 0))} '
    f'{format_int(c.get("cache_read", 0))}) {_DOWN}{format_int(c.get("output", 0))}'
  )


def format_footer(agents: list[str], label_counts: dict[str, Counts]) -> str:
  token_parts = ', '.join(_format_entry(m, label_counts[m]) for m in label_counts)
  return f'> created with {", ".join(agents)} | {token_parts}'


_FOOTER_RE = re.compile(
  r'^>\s*created with\s+(?P<agents>.+?)\s*\|\s*(?P<tokens>.+?)\s*$',
  re.MULTILINE,
)
_PART_RE = re.compile(
  r'^(?P<model>.*?):\s*'
  r'↑\s*\(\s*(?P<input>[\d\']+)\s+(?P<cache_write>[\d\']+)\s+(?P<cache_read>[\d\']+)\s*\)\s*'
  r'↓\s*(?P<output>[\d\']+)$'
)
_BARE_VERSION_RE = re.compile(r'^\d+(?:\.\d+)*$')


@dataclass
class Footer:
  agents: list[str]
  delta: dict[str, Counts]  # label-keyed (the footer renders labels, not slugs)


def _unformat_int(s: str) -> int:
  return int(s.replace(_THOUSANDS, ''))


def parse_footer(commit_msg: str) -> Optional[Footer]:
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  delta: dict[str, Counts] = {}
  for chunk in m.group('tokens').split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    label = pm.group('model').strip()
    counts = {
      'input': _unformat_int(pm.group('input')),
      'cache_write': _unformat_int(pm.group('cache_write')),
      'cache_read': _unformat_int(pm.group('cache_read')),
      'output': _unformat_int(pm.group('output')),
    }
    delta[label] = add(delta.get(label, zero()), counts)
  if len(delta) == 0:
    return None
  agents: list[str] = []
  for token in m.group('agents').split(', '):
    token = token.strip()
    # historic footers compressed same-agent versions ("Claude Code 2.1.114,
    # 2.1.120"); a bare version token is such a compressed Claude Code agent.
    if _BARE_VERSION_RE.match(token) is not None:
      token = f'Claude Code {token}'
    agents.append(token)
  return Footer(agents=agents, delta=delta)


def strip_footer(commit_msg: str) -> str:
  """the message without its footer line."""
  return _FOOTER_RE.sub('', commit_msg).rstrip()


# --- CLI -----------------------------------------------------------------------


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(
    description="print the session's cumulative LLM usage as per-model counts "
    'in the four billed token classes'
  )
  parser.parse(argv)
  current = current_usage()
  if current is None:
    print(
      f'error: no usage source found (no {USAGE_FILE_VARIABLE} pointer, '
      'no Claude Code session transcript)',
      file=sys.stderr,
    )
    return 1
  print(f'agent: {current.agent}')
  for label, counts in to_labels(current.per_model).items():
    print(_format_entry(label, counts))
  return 0
