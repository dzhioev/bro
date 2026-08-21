#!/usr/bin/env python
"""the repository's markdown rules: semantic line breaks, and reflows that only move them.

Prose is written one sentence or major clause per source line, so that rewording a
clause shows up in review as that clause. Placement is a judgement no checker can
make, so the two halves here are the mechanical ones: a line cap that catches a
paragraph left whole, and the reflow check that holds a bulk rewrite to whitespace.
"""

import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Optional

from markdown_it import MarkdownIt
from markdown_it.token import Token

from bro.base.args import Parser

__cli_name__ = 'check-markdown'

# a sentence or a major clause fits well under this, so the cap catches only a
# paragraph left as one source line -- not a long sentence, which is a judgement call
MAX_LINE_LENGTH = 250

# text markdown reproduces verbatim, where the cap does not apply: a line is the
# author's or the language's, not a reflow decision
VERBATIM_BLOCKS = frozenset({'fence', 'code_block', 'html_block', 'table_open'})

_DIRECTIVE = re.compile(r'\{\{.*?\}\}', re.DOTALL)
_WHITESPACE = re.compile(r'\s+')
_LINE_BREAK = re.compile(r'\n[ \t]*')
_TRAILING_WHITESPACE = re.compile(r'\s*\Z')
_LEADING_WHITESPACE = re.compile(r'\A\s*')
# a quote marker is repeated on every line it covers; a list marker is not, so a
# continuation stands in the column the marker opened
_QUOTE_MARKER = re.compile(r'\A[ \t]*(?:>[ \t]?)+')
_LIST_MARKER = re.compile(r'\A[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+')
_INDENT = re.compile(r'\A[ \t]*')

# tables are the one construct the cap and the structure check both depend on, and
# commonmark alone does not read them
_MARKDOWN = MarkdownIt('gfm-like')


def tracked_markdown(repo_root: Path) -> list[str]:
  listing = subprocess.run(
    ['git', 'ls-files', '--cached', '--exclude-standard', '*.md'],
    capture_output=True,
    text=True,
    check=True,
    cwd=repo_root,
  ).stdout
  return listing.splitlines()


def frontmatter(text: str) -> str:
  """the leading `---` block, whose fields are parsed rather than rendered."""
  if not text.startswith('---\n'):
    return ''
  end = text.find('\n---\n', 4)
  return '' if end < 0 else text[: end + 5]


def _frontmatter_lines(text: str) -> frozenset[int]:
  return frozenset(range(1, frontmatter(text).count('\n') + 1))


def _verbatim_lines(text: str) -> frozenset[int]:
  numbers: set[int] = set()
  for token in _MARKDOWN.parse(text):
    if token.type not in VERBATIM_BLOCKS or token.map is None:
      continue
    numbers.update(range(token.map[0] + 1, token.map[1] + 1))
  return frozenset(numbers)


def long_lines(text: str) -> list[tuple[int, str]]:
  verbatim = _verbatim_lines(text) | _frontmatter_lines(text)
  return [
    (number, line)
    for number, line in enumerate(text.split('\n'), start=1)
    if len(line) > MAX_LINE_LENGTH and number not in verbatim
  ]


def continuation_prefix(first_line: str) -> str:
  """what every later line of a block starting with this one has to open with."""
  quote = _QUOTE_MARKER.match(first_line)
  quoted = quote.group() if quote is not None else ''
  rest = first_line[len(quoted) :]
  marker = _LIST_MARKER.match(rest)
  indent = marker if marker is not None else _INDENT.match(rest)
  assert indent is not None
  return quoted + ' ' * len(indent.group())


def continuation_problems(text: str) -> list[tuple[int, str]]:
  """lines that carry a paragraph on but do not stand in its content column."""
  skip = _frontmatter_lines(text)
  lines = text.split('\n')
  problems = []
  for token in _MARKDOWN.parse(text):
    if token.type != 'paragraph_open' or token.map is None:
      continue
    start, end = token.map
    prefix = continuation_prefix(lines[start])
    for number in range(start + 1, min(end, len(lines))):
      line = lines[number]
      if len(line.strip()) == 0:
        continue
      if number + 1 in skip:
        continue
      if not line.startswith(prefix) or line[len(prefix) : len(prefix) + 1] in (' ', '\t'):
        problems.append((number + 1, f'expected it to open with {prefix!r}, reads {line[:40]!r}'))
  return problems


_CODE_SPAN = re.compile(r'`[^`]*`')
# a break is only a break between clauses when nothing is still open across it
_OPENERS = {'(': ')', '[': ']', '“': '”'}
_CLOSERS = {close: open for open, close in _OPENERS.items()}


def _open_after(line: str, stack: list[str], quoted: bool) -> tuple[list[str], bool]:
  for character in _CODE_SPAN.sub('', line):
    if character == '"':
      quoted = not quoted
    elif character in _OPENERS:
      stack.append(character)
    elif character in _CLOSERS and len(stack) > 0 and stack[-1] == _CLOSERS[character]:
      stack.pop()
  return stack, quoted


def _closes_within_the_cap(lines: list[str], number: int, end: int) -> bool:
  """whether the span open at this break could be closed without passing the cap."""
  width = len(lines[number])
  stack, quoted = _open_after(lines[number], [], False)
  for following in range(number + 1, end):
    width += 1 + len(lines[following].strip())
    if width > MAX_LINE_LENGTH:
      return False
    stack, quoted = _open_after(lines[following], stack, quoted)
    if len(stack) == 0 and not quoted:
      return True
  return False


def open_span_problems(text: str) -> list[tuple[int, str]]:
  """breaks landing inside a bracket or a quotation short enough to hold one line.

  A parenthesis too long to close within the cap is prose in its own right and
  breaks at its own clause boundaries."""
  skip = _frontmatter_lines(text)
  lines = text.split('\n')
  problems = []
  for token in _MARKDOWN.parse(text):
    if token.type != 'paragraph_open' or token.map is None:
      continue
    start, end = token.map[0], min(token.map[1], len(lines))
    stack: list[str] = []
    quoted = False
    opened_at = start
    for number in range(start, end - 1):
      was_closed = len(stack) == 0 and not quoted
      stack, quoted = _open_after(lines[number], stack, quoted)
      if len(stack) == 0 and not quoted or number + 1 in skip:
        continue
      if was_closed:
        opened_at = number
      # measured from where the span opened: what cannot fit on one line anywhere
      # is prose, and its inner breaks are the ones the policy asks for
      if _closes_within_the_cap(lines, opened_at, end):
        still_open = '"' if quoted else stack[-1]
        problems.append(
          (number + 1, f'the line breaks while {still_open!r} is still open, mid-phrase')
        )
        break
  return problems


def assert_markdown_policy(repo_root: Path, *, exemptions: Iterable[str] = ()) -> None:
  exempt = frozenset(exemptions)
  paths = [path for path in tracked_markdown(repo_root) if path not in exempt]
  if len(paths) == 0:
    raise AssertionError(f'no tracked markdown found under {repo_root}')

  problems = []
  for relative_path in paths:
    text = (repo_root / relative_path).read_text()
    for number, line in long_lines(text):
      problems.append(
        f'{relative_path}:{number}: {len(line)} characters, over the {MAX_LINE_LENGTH} cap '
        f'-- break it at a sentence or clause boundary'
      )
    for number, reason in continuation_problems(text) + open_span_problems(text):
      problems.append(f'{relative_path}:{number}: {reason}')
  if len(problems) > 0:
    raise AssertionError('\n'.join(problems))


def _unquoted(text: str) -> str:
  # carrying a quoted paragraph onto a second line repeats the quote marker, so the
  # markers come off before words are compared; the token stream still holds the
  # quote structure itself to account
  return '\n'.join(_QUOTE_MARKER.sub('', line) for line in text.split('\n'))


def _words_and_gaps(text: str) -> tuple[list[str], list[str]]:
  stripped = _unquoted(text)
  return _WHITESPACE.split(stripped), _WHITESPACE.findall(stripped)


def _context(words: list[str], index: int) -> str:
  return ' '.join(words[max(0, index - 4) : index + 5])


def assert_content_preserved(before: str, after: str, label: str) -> None:
  """every word survives, and every whitespace run either survives or becomes a break."""
  before_words, before_gaps = _words_and_gaps(before)
  after_words, after_gaps = _words_and_gaps(after)
  if before_words != after_words:
    for index, (was, now) in enumerate(zip(before_words, after_words, strict=False)):
      if was != now:
        raise AssertionError(
          f'{label}: text changed at word {index}: {was!r} became {now!r}\n'
          f'  before: ...{_context(before_words, index)}...\n'
          f'  after:  ...{_context(after_words, index)}...'
        )
    longer, shorter = (
      (before_words, after_words)
      if len(before_words) > len(after_words)
      else (after_words, before_words)
    )
    raise AssertionError(
      f'{label}: {len(longer) - len(shorter)} word(s) added or dropped at the end: '
      f'...{_context(longer, len(shorter))}...'
    )

  for index, (was, now) in enumerate(zip(before_gaps, after_gaps, strict=True)):
    if was == now:
      continue
    if set(was) == {' '} and _LINE_BREAK.fullmatch(now) is not None:
      continue
    raise AssertionError(
      f'{label}: whitespace after word {index} went from {was!r} to {now!r}, '
      f'which is not a line break\n  around: ...{_context(after_words, index)}...'
    )


def _inline_shape(children: list[Token]) -> list[tuple]:
  """the inline stream with soft breaks read as the spaces they render to."""
  shape: list[tuple] = []
  run: list[str] = []

  def flush() -> None:
    if len(run) > 0:
      shape.append(('text', ' '.join(''.join(run).split())))
      run.clear()

  for child in children:
    if child.type == 'softbreak':
      run.append(' ')
    elif child.type == 'text':
      run.append(child.content)
    else:
      flush()
      # every other inline token keeps its content raw: a break inside a code span
      # renders as a space, so only the raw content still shows it moved
      shape.append(
        (
          child.type,
          child.tag,
          child.nesting,
          child.markup,
          child.info,
          tuple(sorted(child.attrs.items())),
          child.content,
        )
      )
  flush()
  return shape


def token_shape(text: str) -> list[tuple]:
  shape: list[tuple] = []
  for token in _MARKDOWN.parse(text):
    if token.type == 'inline':
      shape.append(('inline', token.level, tuple(_inline_shape(token.children or []))))
    else:
      shape.append(
        (
          token.type,
          token.tag,
          token.nesting,
          token.level,
          token.markup,
          token.info,
          tuple(sorted(token.attrs.items())),
          token.content,
        )
      )
  return shape


def assert_structure_preserved(before: str, after: str, label: str) -> None:
  """the document parses to the same thing: no break escaped its list item or block."""
  before_shape = token_shape(before)
  after_shape = token_shape(after)
  if before_shape == after_shape:
    return
  for index, (was, now) in enumerate(zip(before_shape, after_shape, strict=False)):
    if was != now:
      raise AssertionError(
        f'{label}: the document parses differently at token {index}:\n'
        f'  before: {was!r}\n  after:  {now!r}'
      )
  raise AssertionError(
    f'{label}: the document parses to {len(after_shape)} blocks, was {len(before_shape)}'
  )


def directive_contexts(text: str) -> list[tuple[str, str, str]]:
  """each `{{...}}` group with the whitespace runs touching it, which render literally."""
  contexts = []
  for match in _DIRECTIVE.finditer(text):
    leading = _TRAILING_WHITESPACE.search(text[: match.start()])
    trailing = _LEADING_WHITESPACE.match(text[match.end() :])
    assert leading is not None and trailing is not None
    contexts.append((leading.group(), match.group(), trailing.group()))
  return contexts


def assert_directives_preserved(before: str, after: str, label: str) -> None:
  before_contexts = directive_contexts(before)
  after_contexts = directive_contexts(after)
  if before_contexts == after_contexts:
    return
  for index, (was, now) in enumerate(zip(before_contexts, after_contexts, strict=False)):
    if was != now:
      raise AssertionError(
        f'{label}: template directive {index} or the whitespace it renders with changed:\n'
        f'  before: {was!r}\n  after:  {now!r}'
      )
  raise AssertionError(
    f'{label}: {len(after_contexts)} template directives, was {len(before_contexts)}'
  )


def assert_frontmatter_preserved(before: str, after: str, label: str) -> None:
  """the `---` block is parsed field by field, so a break in it is a parse change."""
  if frontmatter(before) != frontmatter(after):
    raise AssertionError(f'{label}: the frontmatter block changed; it is not reflowed')


def assert_reflow(before: str, after: str, label: str) -> None:
  assert_frontmatter_preserved(before, after, label)
  assert_content_preserved(before, after, label)
  assert_structure_preserved(before, after, label)
  assert_directives_preserved(before, after, label)


def _revision_text(repo_root: Path, revision: str, relative_path: str) -> Optional[str]:
  finished = subprocess.run(
    ['git', 'show', f'{revision}:{relative_path}'],
    capture_output=True,
    text=True,
    cwd=repo_root,
  )
  return finished.stdout if finished.returncode == 0 else None


def _changed_markdown(repo_root: Path, base: str) -> list[str]:
  listing = subprocess.run(
    ['git', 'diff', '--name-only', base, '--', '*.md'],
    capture_output=True,
    text=True,
    check=True,
    cwd=repo_root,
  ).stdout
  return listing.splitlines()


def _repo_root() -> Path:
  return Path(
    subprocess.run(
      ['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=True
    ).stdout.strip()
  )


def _check(repo_root: Path, base: str, paths: list[str]) -> Iterator[str]:
  for relative_path in paths:
    before = _revision_text(repo_root, base, relative_path)
    after_path = repo_root / relative_path
    if not after_path.is_file():
      yield f'{relative_path}: deleted, not reflowed'
      continue
    after = after_path.read_text()
    if before is not None:
      try:
        assert_reflow(before, after, relative_path)
      except AssertionError as problem:
        yield str(problem)
    for number, line in long_lines(after):
      yield f'{relative_path}:{number}: {len(line)} characters, over the {MAX_LINE_LENGTH} cap'
    for number, reason in continuation_problems(after) + open_span_problems(after):
      yield f'{relative_path}:{number}: {reason}'


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='check markdown reflows against the repository policy')
  parser.add_argument(
    '--base', default='origin/master', help='revision the working tree is compared against'
  )
  parser.add_argument(
    'paths', nargs='*', help='markdown files to check (default: every one changed since --base)'
  )
  args = parser.parse(argv)
  repo_root = _repo_root()
  paths = args['paths'] if len(args['paths']) > 0 else _changed_markdown(repo_root, args['base'])
  if len(paths) == 0:
    print(f'no markdown changed since {args["base"]}', file=sys.stderr)
    return 0

  problems = list(_check(repo_root, args['base'], paths))
  for problem in problems:
    print(problem, file=sys.stderr)
  print(
    f'{len(paths)} file(s) checked against {args["base"]}: {len(problems)} problem(s)',
    file=sys.stderr,
  )
  return 1 if len(problems) > 0 else 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
