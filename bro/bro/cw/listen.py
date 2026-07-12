"""claude Stop-hook listener: run turn-audit handlers over each finished turn.

`cw.listen` runs at the end of every assistant turn of a `cw ss` session (wired
through the merged `--settings` by `cw/claude_argv.py`). It reads the hook JSON
from stdin, reconstructs the finished turn from the session transcript, and runs
every registered handler over it; the first handler that returns a finding
blocks the stop with the finding's corrective feedback. The blocked stop lands
in the transcript, which `sync-session-log` ships — that record is the
offline-review flag. The mechanism abstains (allows the stop) when its evidence
is unreliable — the transcript never catches up with the turn — and unexpected
errors propagate: a non-2 exit is non-blocking for claude and lands in its
debug log.

One handler exists today: the tool_use guard, catching a turn whose text claims
tool activity its real `tool_use` blocks cannot account for — the fully narrated
fake transcript of incident session ef942eaa is the defining case; contamination
must be confronted in the very turn it happens, since later self-correction
demonstrably fails. The guard is layered: a cheap structural gate (zero
`tool_use` blocks with non-empty text, or transcript-mimicry formatting even
alongside real calls) decides whether to audit at all; an LLM extraction (`mu`
with `prompts/tool_use_guard.prompt`) parses the tool activity the text depicts
as executed — the reading a regex cannot do, excluding quoted syntax, plans, and
earlier-turn summaries; and the verdict is computed in code by comparing the
narrated calls against the transcript's real ones. The guard abstains when its
LLM key is missing.
"""

import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base import credentials, log
from base.args import Parser
from usage import SYNTHETIC_MODEL

# the transcript is written asynchronously and can lag the finished turn (per the
# claude hooks doc); judging a lagging transcript could scold a healthy turn whose
# real tool_use blocks are still unflushed, so reads are synchronized against
# `last_assistant_message` under this deadline.
_SYNC_DEADLINE_SECONDS = 5.0
_SYNC_POLL_SECONDS = 0.2


@dataclass(frozen=True)
class Turn:
  """the finished assistant turn: its assistant-authored text and the names of
  the real tool_use blocks the transcript records for it."""

  text: str
  tool_calls: list[str]


@dataclass(frozen=True)
class Finding:
  """a handler's verdict on a contaminated turn: the corrective feedback fed
  back to claude (the blocked stop's reason) and the one-line notice shown to
  the user."""

  reason: str
  notice: str


Handler = Callable[[Turn], Optional[Finding]]


def _read_entries(path: Path) -> list[dict]:
  entries = []
  for line in path.read_text().splitlines():
    if len(line.strip()) == 0:
      continue
    try:
      entries.append(json.loads(line))
    except json.JSONDecodeError:
      # a torn tail line is expected mid-write; the sync loop retries
      continue
  return entries


def _entry_content(entry: dict):
  message = entry.get('message')
  return message.get('content') if isinstance(message, dict) else None


def _is_prompt_boundary(entry: dict) -> bool:
  """a real user prompt — not a tool_result carrier, not harness-injected."""
  if entry.get('type') != 'user':
    return False
  if entry.get('isSidechain') is True or entry.get('isMeta') is True:
    return False
  content = _entry_content(entry)
  if isinstance(content, str):
    return True
  if isinstance(content, list):
    return not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content)
  return False


def _final_turn(entries: list[dict]) -> Turn:
  """the turn since the last real user prompt: main-thread assistant text
  (thinking blocks and synthetic harness messages excluded) plus real tool_use
  names, in transcript order."""
  texts: list[str] = []
  tool_calls: list[str] = []
  for entry in reversed(entries):
    if _is_prompt_boundary(entry):
      break
    if entry.get('type') != 'assistant' or entry.get('isSidechain') is True:
      continue
    message = entry.get('message')
    if not isinstance(message, dict) or message.get('model') == SYNTHETIC_MODEL:
      continue
    content = message.get('content')
    if not isinstance(content, list):
      continue
    for block in content:
      if not isinstance(block, dict):
        continue
      if block.get('type') == 'text':
        texts.append(block.get('text', ''))
      elif block.get('type') == 'tool_use':
        tool_calls.append(block.get('name', 'unknown'))
  texts.reverse()
  tool_calls.reverse()
  return Turn(text='\n\n'.join(texts).strip(), tool_calls=tool_calls)


def _normalize(text: str) -> str:
  return ' '.join(text.split())


def _synchronized_turn(path: Path, last_assistant_message: str) -> Optional[Turn]:
  """the finished turn once the transcript has flushed it, None when it never
  does within the deadline. flushed = the turn's text contains the final
  response text the hook input carries (whitespace-normalized); transcript
  entries are appended in order, so once the final message is present the whole
  turn is."""
  marker = _normalize(last_assistant_message)
  deadline = time.monotonic() + _SYNC_DEADLINE_SECONDS
  while True:
    turn = _final_turn(_read_entries(path))
    if len(marker) == 0 or marker in _normalize(turn.text):
      return turn
    if time.monotonic() >= deadline:
      return None
    time.sleep(_SYNC_POLL_SECONDS)


# --- the tool_use guard ----------------------------------------------------------

_TOOL_TOKEN = re.compile(r'\bmcp__[\w-]+__\w+')
_TRANSCRIPT_FIELD = re.compile(r'^\s*(?:name|input|result|output)\s*:', re.MULTILINE)


def _mimics_transcript(text: str) -> bool:
  """transcript-shaped text: a concrete mcp tool token plus the field lines of a
  narrated call record — the shape a turn takes when it writes fake tool
  transcripts alongside (or instead of) real calls."""
  return _TOOL_TOKEN.search(text) is not None and len(_TRANSCRIPT_FIELD.findall(text)) >= 2


def _needs_audit(turn: Turn) -> bool:
  if len(turn.text) == 0:
    return False
  if len(turn.tool_calls) == 0:
    return True
  return _mimics_transcript(turn.text)


@dataclass(frozen=True)
class NarratedActivity:
  """the tool activity a turn's text presents as executed: the depicted calls in
  narration order (duplicates kept), plus whether the text asserts further
  actions performed with no identifiable tool named."""

  tool_calls: list[str]
  unattributed_claims: bool


def _narrated_activity(text: str) -> NarratedActivity:
  """LLM extraction of the activity the text depicts. imports are lazy to keep
  the openai stack off the no-trigger path, which runs on every turn end."""
  from pydantic import BaseModel

  from mu import Text, mu
  from prompts import get_prompt

  class Extraction(BaseModel):
    tool_calls: list[str]
    unattributed_claims: bool

  parsed = mu(get_prompt('tool_use_guard.prompt'), Extraction, Text(text), reasoning_effort='low')
  return NarratedActivity(
    tool_calls=parsed.tool_calls, unattributed_claims=parsed.unattributed_claims
  )


def _canonical(name: str) -> str:
  """one comparison form for the tool-name spellings a text may use: `Bash`,
  `mcp__flow__update_task`, and the `flow::update_task` wire form all reduce to
  a lowercase double-underscore name."""
  return name.strip().lower().replace('::', '__').removeprefix('mcp__')


def _fabricated_calls(real: list[str], narrated: list[str]) -> list[str]:
  """the narrated calls the real ones cannot account for, multiset-style: each
  real call covers one narrated depiction of it. narration order is not held
  against the turn — prose legitimately reorders what it describes."""
  remaining = Counter(_canonical(name) for name in real)
  fabricated = []
  for name in narrated:
    canonical = _canonical(name)
    if remaining[canonical] > 0:
      remaining[canonical] -= 1
    else:
      fabricated.append(name)
  return fabricated


def _evidence(turn: Turn, narrated: NarratedActivity) -> Optional[str]:
  """what the turn fabricated, or None for a grounded turn. fabrication is a
  narrated call the real ones cannot cover, or — only when the turn made no
  calls at all — an action claim with no tool named; alongside real calls such
  loose claims plausibly describe those calls."""
  fabricated = _fabricated_calls(turn.tool_calls, narrated.tool_calls)
  if len(fabricated) > 0:
    return 'its text depicts these calls the transcript does not record: ' + ', '.join(fabricated)
  if narrated.unattributed_claims and len(turn.tool_calls) == 0:
    return 'its text asserts actions were performed, but the turn called no tools'
  return None


def _reminder(turn: Turn, evidence: str) -> str:
  if len(turn.tool_calls) == 0:
    reality = 'the session transcript records zero real tool calls for that turn'
  else:
    reality = (
      'the only real tool calls the session transcript records for that turn are: '
      + ', '.join(turn.tool_calls)
    )
  return (
    f'tool_use guard: the turn that just ended described tool activity that did not happen — '
    f'{reality}. Evidence: {evidence}. '
    f'Tell the user plainly that those actions did not happen and any results described for '
    f'them were fabricated. Then redo the work with real tool calls, or — if a needed tool is '
    f'not in your tool list — report exactly that instead.'
  )


def tool_use_guard(turn: Turn) -> Optional[Finding]:
  """block a turn whose text depicts tool activity its real calls cannot cover."""
  if not _needs_audit(turn):
    return None
  if not credentials.available('openai'):
    log.warning('openai secret not resolvable; the tool_use guard cannot audit the turn')
    return None
  evidence = _evidence(turn, _narrated_activity(turn.text))
  if evidence is None:
    return None
  return Finding(
    reason=_reminder(turn, evidence),
    notice='tool_use guard: the finished turn described tool calls it never made; '
    'corrective feedback injected',
  )


# --- the mechanism ---------------------------------------------------------------

# every handler runs over each finished turn; the first finding blocks the stop
_HANDLERS: tuple[Handler, ...] = (tool_use_guard,)


def listen() -> Optional[int]:
  hook_input = json.load(sys.stdin)
  if hook_input.get('stop_hook_active') is True:
    return None
  turn = _synchronized_turn(
    Path(hook_input['transcript_path']),
    hook_input.get('last_assistant_message', ''),
  )
  if turn is None:
    log.warning('transcript never caught up with the finished turn; abstaining')
    return None
  for handler in _HANDLERS:
    finding = handler(turn)
    if finding is None:
      continue
    json.dump(
      {'decision': 'block', 'reason': finding.reason, 'systemMessage': finding.notice},
      sys.stdout,
    )
    return None
  return None


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(
    description='claude Stop hook: run turn-audit handlers over the finished turn '
    '(hook JSON on stdin)'
  )
  return listen(**parser.parse(argv))
