"""the sleep policy: a test module's sleeps are yields, poll intervals, or declared.

A sleep standing in for the signal a test should block on holds only on an idle
machine, so a test module carries none. `RULE` says what a sleep may be instead,
read off the shape of its call site or off a `# sleep: <class>` marker on the
call's line; a marker on a sleep the shape already admits, or on no sleep at
all, is rejected too.
"""

import ast
import io
import re
import subprocess
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.dev.packaging_policy import is_test_module

SUBJECT = 'subject'
BOUND = 'bound'
MARKED_CLASSES = frozenset({SUBJECT, BOUND})
MARKER = re.compile(r'\bsleep:\s*(\w+)')

RULE = (
  'a sleep in a test module is a yield (`sleep(0)`), a poll interval (in a loop that exits on '
  'its signal and is bounded by a deadline that fails: under `asyncio.timeout`, by a comparison '
  'of a clock reading that asserts or raises inside it, or by exhausting a `for` over `range` '
  'or a `while` on such a comparison into a failure right after it), or carries '
  f'`# sleep: {SUBJECT}` (the sleep is what is under test) or `# sleep: {BOUND}` (the wait '
  'proves nothing happens within it) on its line; anything else stands in for a signal to '
  'block on:'
)

# context managers whose expiry raises out of the loop they enclose
_TIMEOUT_CONTEXTS = frozenset({'timeout', 'timeout_at'})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_CLOCKS = frozenset({'monotonic', 'time', 'perf_counter'})
_ORDERINGS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


@dataclass(frozen=True)
class Violation:
  line: int
  reason: str


def _callee(call: ast.Call) -> Optional[str]:
  function = call.func
  if isinstance(function, ast.Attribute):
    return function.attr
  if isinstance(function, ast.Name):
    return function.id
  return None


def _names_sleep(node: ast.AST, names: frozenset[str]) -> bool:
  if isinstance(node, ast.Attribute):
    return node.attr == 'sleep'
  if isinstance(node, ast.Name):
    return node.id in names
  return False


def _sleep_names(tree: ast.AST) -> frozenset[str]:
  """the bare names a module calls a sleep by: `sleep`, and what it imports or assigns one as."""
  names = frozenset({'sleep'})
  for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom):
      names |= {
        alias.asname for alias in node.names if alias.name == 'sleep' and alias.asname is not None
      }
    elif isinstance(node, ast.Assign) and _names_sleep(node.value, names):
      names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
  return names


def _is_yield(call: ast.Call) -> bool:
  if len(call.args) == 0:
    return False
  first = call.args[0]
  return isinstance(first, ast.Constant) and type(first.value) in (int, float) and first.value == 0


def _fails(node: ast.AST) -> bool:
  if isinstance(node, (ast.Assert, ast.Raise)):
    return True
  return (
    isinstance(node, ast.Expr)
    and isinstance(node.value, ast.Call)
    and _callee(node.value) == 'fail'
  )


def _on_path(node: ast.AST) -> Iterator[ast.AST]:
  """`node` and what a pass through it can run — nested loops and functions excluded."""
  yield node
  for child in ast.iter_child_nodes(node):
    if not isinstance(child, (*_FUNCTIONS, *_LOOPS)):
      yield from _on_path(child)


def _has_timeout(context: ast.With | ast.AsyncWith) -> bool:
  return any(
    isinstance(item.context_expr, ast.Call) and _callee(item.context_expr) in _TIMEOUT_CONTEXTS
    for item in context.items
  )


def _following(statement: ast.stmt, parent: ast.AST) -> Optional[ast.stmt]:
  """the statement control reaches when `statement` falls through."""
  for _, value in ast.iter_fields(parent):
    if isinstance(value, list) and statement in value:
      index = value.index(statement)
      return value[index + 1] if index + 1 < len(value) else None
  raise ValueError(f'{ast.dump(statement)} is not a statement of {ast.dump(parent)}')


def _reads_a_clock(node: ast.AST) -> bool:
  return isinstance(node, ast.Call) and _callee(node) in _CLOCKS


def _deadline_check(test: ast.expr) -> bool:
  """whether `test` orders a clock reading against something — a deadline."""
  if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
    return _deadline_check(test.operand)
  return (
    isinstance(test, ast.Compare)
    and all(isinstance(operator, _ORDERINGS) for operator in test.ops)
    and any(_reads_a_clock(operand) for operand in (test.left, *test.comparators))
  )


def _fails_on_a_deadline(node: ast.AST) -> bool:
  if isinstance(node, ast.Assert):
    return _deadline_check(node.test)
  if isinstance(node, ast.If):
    return _deadline_check(node.test) and any(
      _fails(inner) for statement in node.body for inner in _on_path(statement)
    )
  return False


def _exits_on_a_signal(loop: ast.For | ast.AsyncFor | ast.While) -> bool:
  """whether the loop can end on something it observes, not only on a count, a deadline, or never."""
  if (
    isinstance(loop, ast.While)
    and not isinstance(loop.test, ast.Constant)
    and not _deadline_check(loop.test)
  ):
    return True
  return any(
    isinstance(node, (ast.Break, ast.Return))
    for statement in loop.body
    for node in _on_path(statement)
  )


def _exhausts(loop: ast.For | ast.AsyncFor | ast.While) -> bool:
  """whether falling through the loop means its bound ran out."""
  if isinstance(loop, ast.While):
    return _deadline_check(loop.test)
  return isinstance(loop.iter, ast.Call) and _callee(loop.iter) == 'range'


def _loudly_bounded(
  loop: ast.For | ast.AsyncFor | ast.While,
  ancestors: list[ast.AST],
  parents: dict[ast.AST, ast.AST],
) -> bool:
  if any(isinstance(node, (ast.With, ast.AsyncWith)) and _has_timeout(node) for node in ancestors):
    return True
  if any(_fails_on_a_deadline(node) for statement in loop.body for node in _on_path(statement)):
    return True
  if not _exhausts(loop):
    return False
  exhausted = loop.orelse[0] if len(loop.orelse) > 0 else _following(loop, parents[loop])
  return exhausted is not None and _fails(exhausted)


def _classify(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> tuple[bool, str]:
  """whether the call site admits the sleep, and what the sleep is there."""
  if _is_yield(call):
    return True, 'a yield'
  ancestors: list[ast.AST] = []
  cursor = parents.get(call)
  while cursor is not None and not isinstance(cursor, _FUNCTIONS):
    ancestors.append(cursor)
    cursor = parents.get(cursor)
  loops = [node for node in ancestors if isinstance(node, _LOOPS)]
  if len(loops) == 0 or not _exits_on_a_signal(loops[0]):
    return False, 'a timer wait'
  loop = loops[0]
  if _loudly_bounded(loop, ancestors[ancestors.index(loop) :], parents):
    return True, 'a poll interval'
  return False, 'a poll interval in a loop without a loud bound'


def _markers(source: str) -> dict[int, list[str]]:
  """the sleep classes the marker comments name, by line."""
  found: dict[int, list[str]] = {}
  for token in tokenize.generate_tokens(io.StringIO(source).readline):
    if token.type != tokenize.COMMENT:
      continue
    classes = MARKER.findall(token.string)
    if len(classes) > 0:
      found[token.start[0]] = classes
  return found


def violations(source: str) -> list[Violation]:
  """the sleeps of one module the policy rejects, and the markers it rejects."""
  tree = ast.parse(source)
  names = _sleep_names(tree)
  parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
  markers = _markers(source)
  unclaimed = set(markers)
  found: list[Violation] = []
  for node in ast.walk(tree):
    if not isinstance(node, ast.Call) or not _names_sleep(node.func, names):
      continue
    lines = range(node.lineno, (node.end_lineno or node.lineno) + 1)
    marked = [marker for line in lines for marker in markers.get(line, [])]
    unclaimed.difference_update(lines)
    admitted, what = _classify(node, parents)
    if len(marked) > 1:
      found.append(Violation(node.lineno, f'{len(marked)} sleep markers on one call'))
    elif admitted:
      if len(marked) == 1:
        found.append(Violation(node.lineno, f'`# sleep: {marked[0]}` on {what}'))
    elif len(marked) == 0:
      found.append(Violation(node.lineno, what))
    elif marked[0] not in MARKED_CLASSES:
      found.append(Violation(node.lineno, f'unknown sleep class {marked[0]!r}'))
  found.extend(Violation(line, 'a sleep marker with no sleep') for line in unclaimed)
  return sorted(found, key=lambda violation: violation.line)


def test_modules(repo_root: Path) -> list[str]:
  listing = subprocess.run(
    ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '--', '*.py'],
    capture_output=True,
    text=True,
    check=True,
    cwd=repo_root,
  ).stdout
  return [
    path for path in listing.splitlines() if is_test_module(path) and (repo_root / path).is_file()
  ]


def assert_sleep_policy(repo_root: Path) -> None:
  modules = test_modules(repo_root)
  if len(modules) == 0:
    raise AssertionError(f'no test modules found under {repo_root}')
  problems = [
    f'{path}:{violation.line}: {violation.reason}'
    for path in modules
    for violation in violations((repo_root / path).read_text())
  ]
  if len(problems) > 0:
    raise AssertionError('\n'.join([RULE, *problems]))
