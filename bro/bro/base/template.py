"""conditional template engine for static agent-facing text (tool descriptions,
skill bodies): when/iff/eliff/else blocks and assert guards in `{{…}}` groups
terminated by `{{end}}`, their conditions lowered onto `base.condition`
objects — one evaluator and one fail-fast semantics shared with code-built
conditions, any violation surfacing as `TemplateError`. The grammar and full
semantics live in `reference/template.md`.

Only `{{` groups whose first token is a directive keyword are parsed; any other
`{{…}}` is literal text, so braces in code samples survive rendering.
"""

import re
from dataclasses import dataclass
from typing import Optional

from base.condition import Condition, ConditionError, Contains, Equals, Operand, Variable, Variables

_NAME = r'[A-Za-z0-9_-]+'

_DIRECTIVE_RE = re.compile(
  r'\{\{\s*(?P<keyword>when|iff|eliff|else|end|assert)\b(?P<argument>[^}]*)\}\}'
)

_CONDITION_RE = re.compile(
  rf'^\s*(?P<left>\#?{_NAME})'
  rf'(?:\s*(?P<equals>=)\s*|\s+(?P<contains>contains)\s+)'
  rf'(?P<right>\#?{_NAME})\s*$'
)


class TemplateError(ValueError):
  """a malformed template, an unknown variable, a type-mismatched condition, a
  failed `{{assert}}`, or an iff-chain that no branch matched."""


_BUILTINS: Variables = {'true': True, 'false': False}


@dataclass(frozen=True)
class _Directive:
  keyword: str
  argument: str
  start: int
  end: int


def render(text: str, variables: Variables) -> str:
  """render the template against `variables` (plus the built-in `true`/`false`)."""
  overlap = set(variables) & set(_BUILTINS)
  if len(overlap) > 0:
    raise TemplateError(f'variables shadow built-ins: {", ".join(sorted(overlap))}')
  return _Renderer(text, {**_BUILTINS, **variables}).render()


def _operand(token: str) -> Operand:
  return Variable(token[1:]) if token.startswith('#') else token


class _Renderer:
  def __init__(self, text: str, variables: Variables):
    self._text = text
    self._variables = variables
    self._directives = [
      _Directive(match.group('keyword'), match.group('argument'), match.start(), match.end())
      for match in _DIRECTIVE_RE.finditer(text)
    ]
    self._position = 0

  def render(self) -> str:
    output, _ = self._render_until(stops=(), emit=True, cursor=0)
    return output

  def _next(self) -> Optional[_Directive]:
    if self._position >= len(self._directives):
      return None
    directive = self._directives[self._position]
    self._position += 1
    return directive

  def _render_until(
    self, stops: tuple[str, ...], emit: bool, cursor: int
  ) -> tuple[str, Optional[_Directive]]:
    """walk directives until one from `stops` at this nesting level (or the end
    of text), rendering (`emit`) or skipping the content in between. returns the
    rendered output and the stopping directive (None at end of text)."""
    parts: list[str] = []
    while True:
      directive = self._next()
      if directive is None:
        if emit:
          parts.append(self._text[cursor:])
        return ''.join(parts), None
      if emit:
        parts.append(self._text[cursor : directive.start])
      cursor = directive.end
      if directive.keyword in stops:
        return ''.join(parts), directive
      if directive.keyword == 'iff':
        parts.append(self._render_iff(directive, emit))
        cursor = self._directives[self._position - 1].end
      elif directive.keyword == 'when':
        parts.append(self._render_when(directive, emit))
        cursor = self._directives[self._position - 1].end
      elif directive.keyword == 'assert':
        if emit:
          self._assert(directive)
      else:
        raise TemplateError(f'{{{{{directive.keyword}}}}} without a matching {{{{iff}}}}')

  def _render_when(self, opening: _Directive, emit: bool) -> str:
    """render a when/end block whose `{{when}}` was just consumed: the body when
    the condition holds, nothing otherwise. absence is meaningful — the
    optional-inclusion counterpart of an exhaustive `{{iff}}` chain."""
    body_holds = self._evaluate(opening)
    output, stop = self._render_until(
      stops=('eliff', 'else', 'end'),
      emit=emit and body_holds,
      cursor=self._directives[self._position - 1].end,
    )
    if stop is None:
      raise TemplateError(f'{{{{when{opening.argument}}}}} is missing its {{{{end}}}}')
    if stop.keyword != 'end':
      raise TemplateError(
        f'{{{{{stop.keyword}}}}} inside {{{{when}}}}; a when block has no branches'
      )
    if len(stop.argument.strip()) > 0:
      raise TemplateError(f'{{{{end}}}} takes no argument: {stop.argument.strip()!r}')
    return output if body_holds else ''

  def _render_iff(self, opening: _Directive, emit: bool) -> str:
    """render an iff/eliff/else/end chain whose `{{iff}}` was just consumed.
    conditions of every branch are evaluated for validation; at most one branch
    emits. a chain without `{{else}}` whose branches all fail raises when the
    chain itself is in emitted text — the implicit exhaustiveness guard.
    leaves the position right after the chain's `{{end}}`."""
    taken: Optional[str] = None
    branch_holds = self._evaluate(opening)
    seen_else = False
    while True:
      emit_branch = emit and branch_holds and taken is None
      output, stop = self._render_until(
        stops=('eliff', 'else', 'end'),
        emit=emit_branch,
        cursor=self._directives[self._position - 1].end,
      )
      if emit_branch:
        taken = output
      if stop is None:
        raise TemplateError(f'{{{{iff{opening.argument}}}}} is missing its {{{{end}}}}')
      if stop.keyword in ('else', 'end') and len(stop.argument.strip()) > 0:
        raise TemplateError(f'{{{{{stop.keyword}}}}} takes no argument: {stop.argument.strip()!r}')
      if stop.keyword == 'end':
        if taken is None and emit and not seen_else:
          raise TemplateError(
            f'no branch of {{{{iff{opening.argument}}}}} matched and there is no {{{{else}}}}'
          )
        return taken if taken is not None else ''
      if seen_else:
        raise TemplateError(f'{{{{{stop.keyword}}}}} after {{{{else}}}}')
      if stop.keyword == 'eliff':
        branch_holds = self._evaluate(stop)
      else:
        seen_else = True
        branch_holds = True

  def _assert(self, directive: _Directive) -> None:
    if not self._evaluate(directive):
      raise TemplateError(f'assertion failed: {directive.argument.strip()}')

  def _evaluate(self, directive: _Directive) -> bool:
    """lower the directive's condition text onto the `base.condition` model and
    evaluate it, surfacing any semantic violation as `TemplateError`."""
    match = _CONDITION_RE.match(directive.argument)
    if match is None:
      raise TemplateError(
        f'malformed condition {directive.argument.strip()!r} in {{{{{directive.keyword}}}}}'
      )
    left = _operand(match.group('left'))
    right = _operand(match.group('right'))
    lowered: Condition = (
      Equals(left, right) if match.group('equals') is not None else Contains(right, left)
    )
    try:
      return lowered.evaluate(self._variables)
    except ConditionError as error:
      raise TemplateError(str(error)) from error
