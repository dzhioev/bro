from __future__ import annotations

import argparse
import codecs
import contextlib
import errno
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Optional

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

__cli_name__ = 'brash'

REFUSED_STATUS = 126
NOT_FOUND_STATUS = 127
_BUILTINS = frozenset({'cd', 'pwd', 'echo', 'exit', 'true', 'false', ':', 'wait'})
_UNIMPLEMENTED_BASH_BUILTINS = frozenset(
  {
    '.',
    'alias',
    'bg',
    'bind',
    'break',
    'builtin',
    'caller',
    'command',
    'compgen',
    'complete',
    'compopt',
    'continue',
    'coproc',
    'declare',
    'dirs',
    'disown',
    'enable',
    'eval',
    'exec',
    'export',
    'fc',
    'fg',
    'getopts',
    'hash',
    'help',
    'history',
    'jobs',
    'let',
    'local',
    'logout',
    'mapfile',
    'popd',
    'pushd',
    'read',
    'readarray',
    'readonly',
    'return',
    'set',
    'shift',
    'shopt',
    'source',
    'suspend',
    'times',
    'trap',
    'type',
    'typeset',
    'ulimit',
    'umask',
    'unalias',
    'unset',
  }
)
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_ASSIGNMENT = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z', re.DOTALL)
_LANGUAGE = Language(tree_sitter_bash.language())


def _fork() -> int:
  with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    return os.fork()


class Refused(ValueError):
  def __init__(self, subject: str, reason: str, entries: Sequence[str]):
    self.subject = subject
    self.reason = reason
    self.entries = tuple(entries)
    super().__init__(self.message)

  @property
  def message(self) -> str:
    listing = ', '.join(f'`{entry}`' for entry in self.entries)
    if not listing:
      listing = '(empty)'
    return f'refused {self.subject}: {self.reason}; command list: {listing}'


@dataclass(frozen=True)
class _PatternWord:
  expression: re.Pattern[str]
  literal: Optional[str]

  def matches(self, value: str) -> bool:
    return self.expression.fullmatch(value) is not None


@dataclass(frozen=True)
class CommandPattern:
  source: str
  words: tuple[_PatternWord, ...]
  trailing: bool
  program_index: int

  @property
  def program(self) -> str:
    value = self.words[self.program_index].literal
    assert value is not None
    return value

  def matches(self, words: Sequence[str]) -> bool:
    if len(words) < len(self.words) or (not self.trailing and len(words) != len(self.words)):
      return False
    return all(pattern.matches(value) for pattern, value in zip(self.words, words, strict=False))


@dataclass(frozen=True)
class Policy:
  entries: tuple[str, ...]
  writable: bool

  @classmethod
  def read(cls, path: Path) -> Policy:
    try:
      value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
      raise ValueError(f'cannot read brash policy {path}: {error}') from error
    if not isinstance(value, dict):
      raise ValueError(f'brash policy {path} must be a JSON object')
    if set(value) != {'commands', 'files'}:
      raise ValueError(f'brash policy {path} must contain only commands and files')
    commands = value['commands']
    files = value['files']
    if not isinstance(commands, list) or any(not isinstance(entry, str) for entry in commands):
      raise ValueError(f'brash policy {path} commands must be a list of strings')
    if files not in ('read', 'write'):
      raise ValueError(f'brash policy {path} files must be "read" or "write"')
    entries = validate_entries(commands)
    return cls(entries=entries, writable=files == 'write')

  def write(self, path: Path) -> None:
    path.write_text(
      json.dumps({'commands': list(self.entries), 'files': 'write' if self.writable else 'read'})
    )


def _scan_entry(entry: str) -> list[list[tuple[str, bool, bool]]]:
  words: list[list[tuple[str, bool, bool]]] = []
  word: list[tuple[str, bool, bool]] = []
  word_started = False
  quote: Optional[str] = None
  index = 0
  while index < len(entry):
    character = entry[index]
    if quote == "'":
      if character == "'":
        quote = None
      else:
        word.append((character, False, True))
      index += 1
      continue
    if quote == '"':
      if character == '"':
        quote = None
      elif character == '\\' and index + 1 < len(entry) and entry[index + 1] in '$`"\\\n':
        index += 1
        if entry[index] != '\n':
          word.append((entry[index], False, True))
      elif character in '$`':
        raise ValueError('a command-list entry must contain literal words')
      else:
        word.append((character, False, True))
      index += 1
      continue
    if character.isspace():
      if word_started:
        words.append(word)
        word = []
        word_started = False
      index += 1
      continue
    if character in '|&;()<>\n':
      raise ValueError(
        'a command-list entry names one simple command and contains no shell operators'
      )
    if character in '$`':
      raise ValueError('a command-list entry must contain literal words')
    if character == "'" or character == '"':
      word_started = True
      quote = character
      index += 1
      continue
    if character == '\\':
      index += 1
      if index == len(entry):
        raise ValueError('a command-list entry cannot end with a backslash')
      word.append((entry[index], False, True))
      word_started = True
      index += 1
      continue
    word.append((character, character == '*', False))
    word_started = True
    index += 1
  if quote is not None:
    raise ValueError('a command-list entry has an unterminated quote')
  if word_started:
    words.append(word)
  return words


def _compile_word(characters: list[tuple[str, bool, bool]]) -> _PatternWord:
  expression = ''.join(
    '.*' if pattern else re.escape(character) for character, pattern, _ in characters
  )
  literal = (
    None
    if any(pattern for _, pattern, _ in characters)
    else ''.join(character for character, _, _ in characters)
  )
  return _PatternWord(re.compile(expression, re.DOTALL), literal)


def _validate_entry_syntax(entry: str) -> None:
  root = Parser(_LANGUAGE).parse(entry.encode()).root_node
  children = root.named_children
  if root.has_error or len(children) != 1 or children[0].type != 'command':
    raise ValueError('a command-list entry names one simple command and contains no shell syntax')


def parse_entry(entry: str) -> CommandPattern:
  if not isinstance(entry, str):
    raise TypeError('brash command-list entries must be strings')
  normalized = entry.strip()
  if not normalized:
    raise ValueError('a brash command-list entry must be non-empty')
  scanned = _scan_entry(normalized)
  if not scanned:
    raise ValueError('a brash command-list entry must name a program')
  trailing = False

  def unquoted_ellipsis(word: list[tuple[str, bool, bool]]) -> bool:
    return word == [('.', False, False), ('.', False, False), ('.', False, False)]

  ellipses = [index for index, word in enumerate(scanned) if unquoted_ellipsis(word)]
  if ellipses:
    if ellipses != [len(scanned) - 1]:
      raise ValueError('an unquoted ... is allowed only as the final command-list word')
    trailing = True
    scanned.pop()
  if not scanned:
    raise ValueError('a command-list entry must name a program before ...')

  program_index = 0
  while program_index < len(scanned):
    literal = ''.join(character for character, _, _ in scanned[program_index])
    if _ASSIGNMENT.fullmatch(literal) is None:
      break
    program_index += 1
  if program_index == len(scanned):
    raise ValueError('a command-list entry must name a program')
  words = tuple(_compile_word(word) for word in scanned)
  program = words[program_index].literal
  if program is None:
    raise ValueError('a command-list entry needs a literal program word')
  if program in _BUILTINS:
    raise ValueError(f'{program!r} is a brash builtin and needs no command-list entry')
  if program in _UNIMPLEMENTED_BASH_BUILTINS:
    raise ValueError(f'{program!r} is a bash builtin brash does not implement')
  _validate_entry_syntax(normalized)
  return CommandPattern(normalized, words, trailing, program_index)


def validate_entries(entries: Sequence[str]) -> tuple[str, ...]:
  normalized = tuple(parse_entry(entry).source for entry in entries)
  if len(set(normalized)) != len(normalized):
    raise ValueError(f'a brash command list contains duplicate entries: {normalized!r}')
  return normalized


@dataclass
class _Fragment:
  text: str
  quoted: bool


@dataclass
class _ExpandedField:
  text: str
  pattern: str


@dataclass(frozen=True)
class _ChildProcess:
  pid: int
  refusal_descriptor: int


class _Exit(Exception):
  def __init__(self, status: int):
    self.status = status


class _RedirectFailed(Exception):
  pass


class _ChildRefused(Exception):
  pass


class Interpreter:
  def __init__(
    self,
    line: str,
    *,
    entries: Sequence[str],
    writable: bool,
    environment: Optional[dict[str, str]] = None,
  ):
    self.line = line
    self.source = line.encode()
    self.entries = tuple(entries)
    self.patterns = tuple(parse_entry(entry) for entry in entries)
    self.writable = writable
    self.environment = dict(os.environ if environment is None else environment)
    self.variables: dict[str, str] = {}
    self.cwd = Path.cwd()
    self.environment['PWD'] = str(self.cwd)
    self.status = 0
    self.shell_pid = os.getpid()
    self.last_background: Optional[int] = None
    self.last_argument = ''
    self.background: list[_ChildProcess] = []
    self.process_substitutions: list[_ChildProcess] = []
    self.inherited_fds: set[int] = set()
    self.redirect_fds: set[int] = set()
    self.child_refusal_descriptor: Optional[int] = None
    self.substitution_output_fd: Optional[int] = None
    parser = Parser(_LANGUAGE)
    self.tree = parser.parse(self.source)

  def text(self, node: Node) -> str:
    return self.source[node.start_byte : node.end_byte].decode()

  def refuse(self, node: Node | str, reason: str) -> NoReturn:
    subject = node if isinstance(node, str) else self.text(node)
    raise Refused(repr(subject), reason, self.entries)

  def _contains_unquoted(self, target: str) -> bool:
    quote: Optional[str] = None
    index = 0
    while index < len(self.line):
      character = self.line[index]
      if character == '\\' and quote != "'":
        if self.line.startswith(target, index):
          return quote is None
        index += 2
        continue
      if character in ("'", '"'):
        if quote is None:
          quote = character
        elif quote == character:
          quote = None
      if quote is None and self.line.startswith(target, index):
        return True
      index += 1
    return False

  def validate(self) -> None:
    if not self.line.strip():
      raise ValueError('command must be non-empty')
    if '\x00' in self.line or any(
      ord(character) < 32 and character not in '\t\n' for character in self.line
    ):
      self.refuse('control character', 'only tabs and newlines are admitted')
    if self._contains_unquoted('\\\n'):
      self.refuse('backslash-newline', 'a backslash-newline outside quotes is not implemented')
    if self._contains_unquoted('$"'):
      self.refuse('$"..."', 'translated strings are not implemented')
    if self.tree.root_node.has_error:
      self.refuse(self.tree.root_node, 'the line is not valid in brash')
    self._validate_node(self.tree.root_node)

  def _validate_node(self, node: Node) -> None:
    allowed = {
      'program',
      'list',
      'pipeline',
      'command',
      'command_name',
      'word',
      'raw_string',
      'string',
      'string_content',
      'ansi_c_string',
      'concatenation',
      'simple_expansion',
      'expansion',
      'variable_name',
      'special_variable_name',
      'command_substitution',
      'process_substitution',
      'subshell',
      'compound_statement',
      'if_statement',
      'elif_clause',
      'else_clause',
      'for_statement',
      'while_statement',
      'do_group',
      'case_statement',
      'case_item',
      'extglob_pattern',
      'negated_command',
      'redirected_statement',
      'file_redirect',
      'heredoc_redirect',
      'herestring_redirect',
      'file_descriptor',
      'number',
      'heredoc_start',
      'heredoc_body',
      'heredoc_content',
      'heredoc_end',
      'variable_assignment',
      'empty_value',
      'brace_expression',
      'comment',
    }
    if node.is_named and node.type not in allowed:
      suggestion = ''
      if node.type in {'arithmetic_expansion'}:
        suggestion = '; use a program such as `expr` instead'
      elif node.type == 'command_substitution' and self.text(node).startswith('`'):
        suggestion = '; use $(...) instead'
      self.refuse(node, f'{node.type} is not implemented{suggestion}')
    if node.type == 'command_substitution' and self.text(node).startswith('`'):
      self.refuse(node, 'backticks are not implemented; use $(...) instead')
    if node.type == 'expansion':
      names = [child for child in node.named_children if child.type == 'variable_name']
      if len(names) != 1 or self.text(node) != '${' + self.text(names[0]) + '}':
        self.refuse(node, 'parameter-expansion operators are not implemented; use ${name}')
    if node.type == 'variable_assignment' and (
      node.parent is None or node.parent.type != 'command'
    ):
      name = node.child_by_field_name('name')
      assert name is not None
      if self.text(name) in self.environment:
        self.refuse(node, f'{self.text(name)} is an environment variable and cannot be assigned')
    if node.type == 'for_statement':
      variable = node.child_by_field_name('variable')
      assert variable is not None
      if self.text(variable) in self.environment:
        self.refuse(
          node, f'{self.text(variable)} is an environment variable and cannot be assigned'
        )
    if node.type == 'command':
      self._validate_literal_program(node)
    if (
      node.type == 'command_substitution'
      and node.named_children
      and all(
        child.type in {'file_redirect', 'heredoc_redirect', 'herestring_redirect'}
        for child in node.named_children
      )
    ):
      self.refuse(node, 'a command of redirects alone is not implemented')
    if node.type == 'redirected_statement':
      body = node.child_by_field_name('body')
      if body is None or body.type not in {
        'command',
        'pipeline',
        'list',
        'negated_command',
        'subshell',
        'compound_statement',
        'if_statement',
        'for_statement',
        'while_statement',
        'case_statement',
      }:
        self.refuse(node, 'a command of redirects alone is not implemented')
    for child in node.named_children:
      self._validate_node(child)

  def _literal_word(self, node: Node) -> Optional[str]:
    if node.type == 'command_name':
      children = node.named_children
      return self._literal_word(children[0]) if len(children) == 1 else None
    if node.type == 'raw_string':
      return self.text(node)[1:-1]
    if node.type == 'ansi_c_string':
      return self._ansi_c(self.text(node)[2:-1])
    if node.type == 'word':
      if node.named_children:
        return None
      return self._unescape_literal(self.text(node))
    if node.type == 'string':
      if node.named_children:
        return None
      return self._unescape_double(self.text(node)[1:-1])
    if node.type == 'concatenation':
      values = [self._literal_word(child) for child in node.named_children]
      if any(value is None for value in values):
        return None
      return ''.join(value for value in values if value is not None)
    return None

  def _validate_literal_program(self, node: Node) -> None:
    name = node.child_by_field_name('name')
    if name is None:
      return
    program = self._literal_word(name)
    if program is None:
      return
    if program in _BUILTINS:
      return
    if program in _UNIMPLEMENTED_BASH_BUILTINS:
      self.refuse(name, f'{program!r} is a bash builtin brash does not implement')
    if not any(pattern.program == program for pattern in self.patterns):
      self.refuse(name, f'program {program!r} has no command-list entry')

  def run(self) -> int:
    self.validate()
    try:
      self.status = self._run(self.tree.root_node)
    except _Exit as stopped:
      self.status = stopped.status
    except _ChildRefused:
      self.status = REFUSED_STATUS
    return self.status

  def _run(self, node: Node) -> int:
    method = getattr(self, f'_run_{node.type}', None)
    if method is None:
      self.refuse(node, f'{node.type} is not implemented')
    status = int(method(node))
    self.status = status
    return status

  def _run_program(self, node: Node) -> int:
    return self._run_sequence(node)

  def _run_sequence(self, node: Node, *, skip: frozenset[str] = frozenset({'comment'})) -> int:
    status = 0
    children = node.children
    for index, child in enumerate(children):
      if not child.is_named or child.type in skip:
        continue
      next_separator = next(
        (
          candidate.type
          for candidate in children[index + 1 :]
          if not candidate.is_named or candidate.type not in skip
        ),
        None,
      )
      if next_separator == '&':
        status = self._run_background(child)
      else:
        status = self._run(child)
    return status

  def _child_status(self, callback: Callable[[], int]) -> int:
    try:
      return callback()
    except Refused as error:
      os.write(2, f'brash: {error.message}\n'.encode())
      if self.child_refusal_descriptor is not None:
        os.write(self.child_refusal_descriptor, b'1')
      return REFUSED_STATUS
    except _Exit as stopped:
      return stopped.status
    except _RedirectFailed:
      return 1
    except _ChildRefused:
      if self.child_refusal_descriptor is not None:
        os.write(self.child_refusal_descriptor, b'1')
      return REFUSED_STATUS

  def _start_child(
    self,
    callback: Callable[[], int],
    setup: Optional[Callable[[], None]] = None,
  ) -> _ChildProcess:
    refusal_descriptor, child_refusal_descriptor = os.pipe()
    pid = _fork()
    if pid == 0:
      os.close(refusal_descriptor)
      self.child_refusal_descriptor = child_refusal_descriptor
      if setup is not None:
        setup()
      os._exit(self._child_status(callback))
    os.close(child_refusal_descriptor)
    os.set_blocking(refusal_descriptor, False)
    return _ChildProcess(pid, refusal_descriptor)

  def _wait_child(self, child: _ChildProcess) -> tuple[int, bool]:
    status = self._wait_status(child.pid)
    try:
      refused = bool(os.read(child.refusal_descriptor, 1))
    except BlockingIOError:
      refused = False
    os.close(child.refusal_descriptor)
    return status, refused

  def _wait_child_or_refuse(self, child: _ChildProcess) -> int:
    status, refused = self._wait_child(child)
    if refused:
      raise _ChildRefused
    return status

  def _run_background(self, node: Node) -> int:
    child = self._start_child(lambda: self._run(node))
    self.background.append(child)
    self.last_background = child.pid
    return 0

  def _run_list(self, node: Node) -> int:
    children = node.children
    operator = next((child.type for child in children if child.type in {'&&', '||'}), None)
    if operator is None:
      return self._run_sequence(node)
    operands = [child for child in children if child.is_named and child.type != 'comment']
    if len(operands) != 2:
      self.refuse(node, 'malformed boolean list')
    status = self._run(operands[0])
    if (operator == '&&' and status == 0) or (operator == '||' and status != 0):
      status = self._run(operands[1])
    return status

  def _run_pipeline(self, node: Node) -> int:
    commands = [child for child in node.named_children if child.type != 'comment']
    pipes = [os.pipe() for _ in range(len(commands) - 1)]
    children: list[_ChildProcess] = []
    for index, command in enumerate(commands):

      def setup(index: int = index) -> None:
        if index > 0:
          os.dup2(pipes[index - 1][0], 0)
        if index < len(commands) - 1:
          os.dup2(pipes[index][1], 1)
        for read_fd, write_fd in pipes:
          os.close(read_fd)
          os.close(write_fd)

      children.append(self._start_child(lambda command=command: self._run(command), setup))
    for read_fd, write_fd in pipes:
      os.close(read_fd)
      os.close(write_fd)
    outcomes = [self._wait_child(child) for child in children]
    if any(refused for _, refused in outcomes):
      raise _ChildRefused
    return outcomes[-1][0]

  def _run_negated_command(self, node: Node) -> int:
    children = [child for child in node.named_children if child.type != 'comment']
    if len(children) != 1:
      self.refuse(node, 'malformed negated command')
    return 0 if self._run(children[0]) != 0 else 1

  def _run_subshell(self, node: Node) -> int:
    return self._fork_and_wait(lambda: self._run_sequence(node))

  def _run_compound_statement(self, node: Node) -> int:
    return self._run_sequence(node)

  def _run_if_statement(self, node: Node) -> int:
    children = node.named_children
    clauses = [child for child in children if child.type in {'elif_clause', 'else_clause'}]
    body = [child for child in children if child not in clauses]
    condition = node.child_by_field_name('condition')
    if condition is None:
      self.refuse(node, 'if statement has no condition')
    condition_index = body.index(condition)
    condition_nodes = body[: condition_index + 1]
    then_nodes = body[condition_index + 1 :]
    if self._run_nodes(condition_nodes) == 0:
      return self._run_nodes(then_nodes)
    for clause in clauses:
      if clause.type == 'elif_clause':
        result = self._run_elif_clause(clause)
        if result is not None:
          return result
      else:
        return self._run_else_clause(clause)
    return 0

  def _run_elif_clause(self, node: Node) -> Optional[int]:
    children = [child for child in node.named_children if child.type != 'comment']
    if not children:
      self.refuse(node, 'elif clause has no condition')
    nested = next((child for child in children if child.type == 'else_clause'), None)
    effective = [child for child in children if child != nested]
    if self._run(effective[0]) == 0:
      return self._run_nodes(effective[1:])
    if nested is not None:
      return self._run_else_clause(nested)
    return None

  def _run_else_clause(self, node: Node) -> int:
    return self._run_sequence(node)

  def _run_for_statement(self, node: Node) -> int:
    variable = node.child_by_field_name('variable')
    body = node.child_by_field_name('body')
    if variable is None or body is None:
      self.refuse(node, 'malformed for statement')
    values: list[str] = []
    for index, child in enumerate(node.children):
      if node.field_name_for_child(index) == 'value':
        values.extend(self._expand_word(child))
    status = 0
    for value in values:
      self.variables[self.text(variable)] = value
      status = self._run(body)
    return status

  def _run_while_statement(self, node: Node) -> int:
    body = node.child_by_field_name('body')
    condition = node.child_by_field_name('condition')
    if body is None or condition is None:
      self.refuse(node, 'malformed loop')
    condition_nodes: list[Node] = []
    for child in node.named_children:
      if child == body:
        break
      condition_nodes.append(child)
    until = node.children[0].type == 'until'
    status = 0
    while (self._run_nodes(condition_nodes) == 0) != until:
      status = self._run(body)
    return status

  def _run_do_group(self, node: Node) -> int:
    return self._run_sequence(node)

  def _run_case_statement(self, node: Node) -> int:
    value = node.child_by_field_name('value')
    if value is None:
      self.refuse(node, 'case statement has no value')
    expanded = self._expand_word(value)
    subject = expanded[0] if expanded else ''
    for item in [child for child in node.named_children if child.type == 'case_item']:
      patterns: list[Node] = []
      body: list[Node] = []
      seen_body = False
      for child in item.named_children:
        if child.type == 'comment':
          continue
        if child.type in {'extglob_pattern', 'word', 'raw_string', 'string'} and not seen_body:
          patterns.append(child)
        else:
          seen_body = True
          body.append(child)
      for pattern in patterns:
        alternatives = self._word_fragments(pattern, quoted=False)
        if len(alternatives) != 1:
          self.refuse(pattern, 'a case pattern must expand to one pattern')
        expression = ''.join(
          glob.escape(fragment.text) if fragment.quoted else fragment.text
          for fragment in alternatives[0]
        )
        if fnmatch.fnmatchcase(subject, expression):
          return self._run_nodes(body)
    return 0

  def _run_redirected_statement(self, node: Node) -> int:
    body = node.child_by_field_name('body')
    redirects = [child for child in node.named_children if child != body]
    if body is None:
      self.refuse(node, 'a command of redirects alone is not implemented')
    try:
      with self._substitution_output(), self._redirected(redirects):
        return self._run(body)
    except _RedirectFailed:
      return 1

  def _run_variable_assignment(self, node: Node) -> int:
    name = node.child_by_field_name('name')
    value = node.child_by_field_name('value')
    assert name is not None
    variable = self.text(name)
    if variable in self.environment:
      self.refuse(node, f'{variable} is an environment variable and cannot be assigned')
    self.variables[variable] = (
      '' if value is None else ''.join(self._expand_word(value, split=False, pathname=False))
    )
    return 0

  def _run_command(self, node: Node) -> int:
    with self._command_resources():
      return self._run_command_with_resources(node)

  def _run_command_with_resources(self, node: Node) -> int:
    assignments: list[str] = []
    redirects: list[Node] = []
    name = node.child_by_field_name('name')
    arguments: list[Node] = []
    for child in node.named_children:
      if (
        child.type == 'variable_assignment'
        and name is not None
        and child.end_byte <= name.start_byte
      ):
        variable = child.child_by_field_name('name')
        value = child.child_by_field_name('value')
        assert variable is not None
        rendered = (
          '' if value is None else ''.join(self._expand_word(value, split=False, pathname=False))
        )
        assignments.append(f'{self.text(variable)}={rendered}')
      elif child.type in {'file_redirect', 'heredoc_redirect', 'herestring_redirect'}:
        redirects.append(child)
      elif child != name:
        arguments.append(child)
    if name is None:
      if len(assignments) != 1:
        self.refuse(node, 'a command must name a program or one plain assignment')
      variable, value = assignments[0].split('=', 1)
      if variable in self.environment:
        self.refuse(node, f'{variable} is an environment variable and cannot be assigned')
      self.variables[variable] = value
      return 0
    argv = self._expand_word(name)
    for argument in arguments:
      argv.extend(self._expand_word(argument))
    try:
      with self._redirected(redirects):
        return self._run_expanded_command(assignments, argv)
    except _RedirectFailed:
      return 1

  def _run_expanded_command(self, assignments: list[str], argv: list[str]) -> int:
    if not argv:
      return 0
    program = argv[0]
    command_words = [*assignments, *argv]
    if program not in _BUILTINS:
      self._admit(command_words)
    self.last_argument = argv[-1]
    command_environment = dict(self.environment)
    for assignment in assignments:
      variable, value = assignment.split('=', 1)
      command_environment[variable] = value
    if program in _BUILTINS:
      prefix = dict(assignment.split('=', 1) for assignment in assignments)
      with self._environment_overlay(prefix):
        return self._run_builtin(argv)
    if program in _UNIMPLEMENTED_BASH_BUILTINS:
      self.refuse(program, f'{program!r} is a bash builtin brash does not implement')
    executable = self._find_program(program)
    if executable is None:
      os.write(2, f'brash: {program}: command not found\n'.encode())
      return NOT_FOUND_STATUS
    try:
      completed = subprocess.run(
        argv,
        executable=executable,
        cwd=self.cwd,
        env=command_environment,
        pass_fds=tuple(self.inherited_fds | self.redirect_fds),
        check=False,
      )
    except OSError as error:
      if error.errno == errno.ENOEXEC:
        shell = self._find_program('bash')
        if shell is None:
          os.write(2, f'brash: {program}: {error.strerror}\n'.encode())
          return REFUSED_STATUS
        completed = subprocess.run(
          [shell, executable, *argv[1:]],
          cwd=self.cwd,
          env=command_environment,
          pass_fds=tuple(self.inherited_fds | self.redirect_fds),
          check=False,
        )
      else:
        os.write(2, f'brash: {program}: {error.strerror}\n'.encode())
        if error.errno in {errno.EACCES, errno.EISDIR}:
          return REFUSED_STATUS
        return NOT_FOUND_STATUS
    return 128 - completed.returncode if completed.returncode < 0 else completed.returncode

  def _find_program(self, program: str) -> Optional[str]:
    if '/' in program:
      path = Path(program)
      candidate = path if path.is_absolute() else self.cwd / path
      return str(candidate) if candidate.exists() else None
    inaccessible: Optional[Path] = None
    for entry in self.environment.get('PATH', os.defpath).split(os.pathsep):
      directory = Path(entry or '.')
      if not directory.is_absolute():
        directory = self.cwd / directory
      candidate = directory / program
      if not candidate.exists():
        continue
      if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
      if inaccessible is None:
        inaccessible = candidate
    return None if inaccessible is None else str(inaccessible)

  def _admit(self, words: Sequence[str]) -> None:
    if any(pattern.matches(words) for pattern in self.patterns):
      return
    self.refuse(' '.join(words), 'the expanded command does not match any command-list entry')

  def _run_builtin(self, argv: list[str]) -> int:
    program = argv[0]
    if program == 'true' or program == ':':
      return 0
    if program == 'false':
      return 1
    if program == 'exit':
      if len(argv) > 2:
        os.write(2, b'brash: exit: too many arguments\n')
        return 1
      status = self.status if len(argv) == 1 else self._status_argument(argv[1], 'exit')
      raise _Exit(status)
    if program == 'pwd':
      if any(argument not in ('-L', '-P') for argument in argv[1:]):
        os.write(2, b'brash: pwd: invalid option\n')
        return 2
      self._write(1, f'{self.cwd}\n')
      return 0
    if program == 'cd':
      arguments = [argument for argument in argv[1:] if argument not in ('-L', '-P')]
      if len(arguments) > 1:
        os.write(2, b'brash: cd: too many arguments\n')
        return 1
      target = arguments[0] if arguments else self.environment.get('HOME')
      if target is None:
        os.write(2, b'brash: cd: HOME not set\n')
        return 1
      path = Path(target)
      if not path.is_absolute():
        path = self.cwd / path
      previous = self.cwd
      try:
        self.cwd = path.resolve(strict=True)
      except OSError as error:
        os.write(2, f'brash: cd: {error}\n'.encode())
        return 1
      if not self.cwd.is_dir():
        self.cwd = previous
        os.write(2, f'brash: cd: {target}: not a directory\n'.encode())
        return 1
      self.environment['OLDPWD'] = str(previous)
      self.environment['PWD'] = str(self.cwd)
      return 0
    if program == 'echo':
      newline = True
      escapes = False
      index = 1
      while index < len(argv) and argv[index] in ('-n', '-e', '-E'):
        newline = newline and argv[index] != '-n'
        if argv[index] == '-e':
          escapes = True
        elif argv[index] == '-E':
          escapes = False
        index += 1
      text = ' '.join(argv[index:])
      if escapes:
        text = self._ansi_c(text)
      self._write(1, text + ('\n' if newline else ''))
      return 0
    if program == 'wait':
      if len(argv) != 1:
        os.write(2, b'brash: wait: only plain wait is implemented\n')
        return 2
      status = 0
      while self.background:
        status = self._wait_child_or_refuse(self.background.pop(0))
      return status
    raise AssertionError(program)

  def _status_argument(self, value: str, builtin: str) -> int:
    try:
      return int(value) % 256
    except ValueError:
      os.write(2, f'brash: {builtin}: {value}: numeric argument required\n'.encode())
      return 2

  def _run_nodes(self, nodes: Sequence[Node]) -> int:
    status = 0
    for node in nodes:
      if node.type == 'comment':
        continue
      status = self._run(node)
    return status

  def _expand_word(self, node: Node, *, split: bool = True, pathname: bool = True) -> list[str]:
    alternatives = self._word_fragments(node, quoted=False)
    values: list[str] = []
    for fragments in alternatives:
      if split:
        fields = self._split_fragments(fragments)
      else:
        text = ''.join(fragment.text for fragment in fragments)
        pattern = ''.join(
          glob.escape(fragment.text) if fragment.quoted else fragment.text for fragment in fragments
        )
        fields = [_ExpandedField(text, pattern)]
      if not fields and any(fragment.quoted for fragment in fragments):
        fields = [_ExpandedField('', '')]
      for field in fields:
        values.extend(self._pathname_expand(field) if pathname else [field.text])
    return values

  def _word_fragments(self, node: Node, *, quoted: bool) -> list[list[_Fragment]]:
    if node.type == 'command_name':
      children = node.named_children
      if len(children) != 1:
        self.refuse(node, 'malformed command name')
      return self._word_fragments(children[0], quoted=quoted)
    if node.type == 'raw_string':
      return [[_Fragment(self.text(node)[1:-1], True)]]
    if node.type == 'ansi_c_string':
      return [[_Fragment(self._ansi_c(self.text(node)[2:-1]), True)]]
    if node.type in {'simple_expansion', 'expansion'}:
      source = self.text(node)
      prefix = source[: source.find('$')]
      fragments = [] if not prefix else [_Fragment(prefix, quoted)]
      name_node = next(
        (
          child
          for child in node.named_children
          if child.type in {'variable_name', 'special_variable_name'}
        ),
        None,
      )
      if name_node is None:
        self.refuse(node, 'malformed parameter expansion')
      if self.text(name_node) != '@':
        fragments.append(_Fragment(self._expand_variable(node), quoted))
      return [fragments]
    if node.type == 'command_substitution':
      return [[_Fragment(self._command_substitution(node), quoted)]]
    if node.type == 'process_substitution':
      return [[_Fragment(self._process_substitution(node), quoted)]]
    if node.type == 'brace_expression':
      return [[_Fragment(value, quoted)] for value in self._brace_values(node)]
    if node.type == 'string':
      if not node.named_children:
        return [[_Fragment('', True)]]
      return self._fragments_from_children(node, quoted=True, trim=1)
    if node.type in {'word', 'concatenation', 'extglob_pattern'}:
      if node.type == 'concatenation' and all(
        descendant.type in {'word', 'number', 'brace_expression'}
        for descendant in node.named_children
      ):
        values = self._brace_expand_literal(self.text(node))
        if len(values) > 1:
          return [self._literal_fragments(value, quoted=quoted) for value in values]
      return self._fragments_from_children(node, quoted=quoted, trim=0)
    if node.type in {'number', 'empty_value', 'string_content'}:
      return [self._literal_fragments(self.text(node), quoted=quoted)]
    self.refuse(node, f'{node.type} is not a word form brash implements')

  def _fragments_from_children(
    self, node: Node, *, quoted: bool, trim: int
  ) -> list[list[_Fragment]]:
    start = node.start_byte + trim
    end = node.end_byte - trim
    alternatives: list[list[_Fragment]] = [[]]
    for child in node.named_children:
      if child.start_byte > start:
        literal = self.source[start : child.start_byte].decode()
        for fragments in alternatives:
          fragments.extend(self._literal_fragments(literal, quoted=quoted))
      child_alternatives = self._word_fragments(child, quoted=quoted)
      alternatives = [left + right for left in alternatives for right in child_alternatives]
      start = child.end_byte
    if start < end:
      literal = self.source[start:end].decode()
      for fragments in alternatives:
        fragments.extend(self._literal_fragments(literal, quoted=quoted))
    if node.type == 'word':
      for fragments in alternatives:
        if fragments and fragments[0].text.startswith('~') and not fragments[0].quoted:
          home = self.environment.get('HOME')
          if home is not None and (fragments[0].text == '~' or fragments[0].text.startswith('~/')):
            fragments[0].text = home + fragments[0].text[1:]
    return alternatives

  def _literal_fragments(self, value: str, *, quoted: bool) -> list[_Fragment]:
    if quoted:
      return [_Fragment(self._unescape_double(value), True)]
    fragments: list[_Fragment] = []
    start = 0
    index = 0
    while index < len(value):
      if value[index] != '\\':
        index += 1
        continue
      if index > start:
        fragments.append(_Fragment(value[start:index], False))
      index += 1
      if index == len(value):
        self.refuse(value, 'a word cannot end with a backslash')
      if value[index] != '\n':
        fragments.append(_Fragment(value[index], True))
      index += 1
      start = index
    if start < len(value):
      fragments.append(_Fragment(value[start:], False))
    return fragments

  def _expand_variable(self, node: Node) -> str:
    name_node = next(
      (
        child
        for child in node.named_children
        if child.type in {'variable_name', 'special_variable_name'}
      ),
      None,
    )
    if name_node is None:
      self.refuse(node, 'malformed parameter expansion')
    name = self.text(name_node)
    if name == '?':
      return str(self.status)
    if name == '$':
      return str(self.shell_pid)
    if name == '!':
      return '' if self.last_background is None else str(self.last_background)
    if name == '0':
      return 'brash'
    if name == '#':
      return '0'
    if name in ('@', '*'):
      return ''
    if name == '-':
      return ''
    if name == '_':
      return self.last_argument
    return self.variables.get(name, self.environment.get(name, ''))

  def _command_substitution(self, node: Node) -> str:
    read_fd, write_fd = os.pipe()

    def setup() -> None:
      os.close(read_fd)
      os.dup2(write_fd, 1)
      os.close(write_fd)

    child = self._start_child(lambda: self._run_sequence(node), setup)
    os.close(write_fd)
    with os.fdopen(read_fd, 'rb') as output:
      value = output.read()
    status = self._wait_child_or_refuse(child)
    self.status = status
    return value.decode(errors='surrogateescape').rstrip('\n')

  def _process_substitution(self, node: Node) -> str:
    read_fd, write_fd = os.pipe()
    opening = node.children[0].type

    def setup() -> None:
      if opening == '<(':
        os.close(read_fd)
        os.dup2(write_fd, 1)
      else:
        os.close(write_fd)
        os.dup2(read_fd, 0)
        if self.substitution_output_fd is not None:
          os.dup2(self.substitution_output_fd, 1)

    child = self._start_child(lambda: self._run_sequence(node), setup)
    self.process_substitutions.append(child)
    if opening == '<(':
      os.close(write_fd)
      self.inherited_fds.add(read_fd)
      return f'/dev/fd/{read_fd}'
    os.close(read_fd)
    self.inherited_fds.add(write_fd)
    return f'/dev/fd/{write_fd}'

  def _brace_expand_literal(self, value: str) -> list[str]:
    opening = value.find('{')
    if opening == -1:
      return [value]
    depth = 0
    for index in range(opening, len(value)):
      if value[index] == '{':
        depth += 1
      elif value[index] == '}':
        depth -= 1
        if depth == 0:
          inside = value[opening + 1 : index]
          if ',' in inside:
            alternatives = inside.split(',')
          elif '..' in inside:
            alternatives = self._brace_range(inside)
            if alternatives is None:
              return [value]
          else:
            return [value]
          suffixes = self._brace_expand_literal(value[index + 1 :])
          return [
            value[:opening] + alternative + suffix
            for alternative in alternatives
            for suffix in suffixes
          ]
    return [value]

  def _brace_range(self, text: str) -> Optional[list[str]]:
    parts = text.split('..')
    if len(parts) not in (2, 3):
      return None
    first, last = parts[:2]
    numeric = re.fullmatch(r'-?\d+', first) and re.fullmatch(r'-?\d+', last)
    alphabetic = len(first) == 1 and len(last) == 1 and first.isalpha() and last.isalpha()
    if not numeric and not alphabetic:
      return None
    start = int(first) if numeric else ord(first)
    stop = int(last) if numeric else ord(last)
    try:
      step = int(parts[2]) if len(parts) == 3 else (1 if start <= stop else -1)
    except ValueError:
      return None
    if step == 0:
      self.refuse(text, 'a brace range step cannot be zero')
    boundary = stop + (1 if step > 0 else -1)
    values = list(range(start, boundary, step))
    if alphabetic:
      return [chr(value) for value in values]
    width = max(len(first.lstrip('-')), len(last.lstrip('-')))
    padded = first.lstrip('-').startswith('0') or last.lstrip('-').startswith('0')
    if not padded:
      return [str(value) for value in values]
    return [f'-{abs(value):0{width}d}' if value < 0 else f'{value:0{width}d}' for value in values]

  def _brace_values(self, node: Node) -> list[str]:
    text = self.text(node)[1:-1]
    ranged = self._brace_range(text)
    return text.split(',') if ranged is None else ranged

  def _split_fragments(self, fragments: Sequence[_Fragment]) -> list[_ExpandedField]:
    if not fragments:
      return []
    fields = [_ExpandedField('', '')]
    separators = self.environment.get('IFS', ' \t\n')
    for fragment in fragments:
      if fragment.quoted:
        fields[-1].text += fragment.text
        fields[-1].pattern += glob.escape(fragment.text)
        continue
      pieces = re.split(f'([{re.escape(separators)}]+)', fragment.text)
      for piece in pieces:
        if not piece:
          continue
        if all(character in separators for character in piece):
          if fields[-1].text:
            fields.append(_ExpandedField('', ''))
          continue
        fields[-1].text += piece
        fields[-1].pattern += piece
    return [
      field for field in fields if field.text or any(fragment.quoted for fragment in fragments)
    ]

  def _pathname_expand(self, field: _ExpandedField) -> list[str]:
    if not glob.has_magic(field.pattern):
      return [field.text]
    pattern = field.pattern if os.path.isabs(field.pattern) else str(self.cwd / field.pattern)
    matches = glob.glob(pattern)
    if not matches:
      return [field.text]
    if os.path.isabs(field.text):
      return sorted(matches)
    return sorted(str(Path(match).relative_to(self.cwd)) for match in matches)

  @contextlib.contextmanager
  def _environment_overlay(self, values: dict[str, str]) -> Iterator[None]:
    missing = object()
    previous: dict[str, object] = {name: self.environment.get(name, missing) for name in values}
    self.environment.update(values)
    try:
      yield
    finally:
      for name, value in previous.items():
        if value is missing:
          del self.environment[name]
        else:
          assert isinstance(value, str)
          self.environment[name] = value

  @contextlib.contextmanager
  def _command_resources(self) -> Iterator[None]:
    original_fds = set(self.inherited_fds)
    original_substitutions = len(self.process_substitutions)
    try:
      yield
    finally:
      for descriptor in self.inherited_fds - original_fds:
        os.close(descriptor)
      self.inherited_fds = original_fds
      added_substitutions = self.process_substitutions[original_substitutions:]
      del self.process_substitutions[original_substitutions:]
      outcomes = [self._wait_child(child) for child in added_substitutions]
      if any(refused for _, refused in outcomes):
        raise _ChildRefused

  @contextlib.contextmanager
  def _substitution_output(self) -> Iterator[None]:
    previous = self.substitution_output_fd
    descriptor = os.dup(1)
    self.substitution_output_fd = descriptor
    try:
      yield
    finally:
      self.substitution_output_fd = previous
      os.close(descriptor)

  @contextlib.contextmanager
  def _redirected(self, redirects: Sequence[Node]) -> Iterator[None]:
    saved: dict[int, Optional[int]] = {}
    opened: list[int] = []
    original_redirect_fds = set(self.redirect_fds)
    try:
      for redirect in redirects:
        targets, source = self._prepare_redirect(redirect, opened)
        for target in targets:
          if target > 2:
            self.redirect_fds.add(target)
          if target not in saved:
            try:
              saved[target] = os.dup(target)
            except OSError:
              saved[target] = None
          if source is None:
            try:
              os.close(target)
            except OSError:
              pass
          else:
            try:
              os.dup2(source, target)
            except OSError as error:
              os.write(2, f'brash: redirect: {error.strerror}\n'.encode())
              raise _RedirectFailed from error
      yield
    finally:
      self.redirect_fds = original_redirect_fds
      for target, original in saved.items():
        if original is None:
          try:
            os.close(target)
          except OSError:
            pass
        else:
          os.dup2(original, target)
          os.close(original)
      for descriptor in opened:
        try:
          os.close(descriptor)
        except OSError:
          pass

  @staticmethod
  def _input_descriptor(text: str, opened: list[int]) -> int:
    with tempfile.TemporaryFile() as stream:
      stream.write(text.encode())
      stream.seek(0)
      descriptor = os.dup(stream.fileno())
    opened.append(descriptor)
    return descriptor

  def _prepare_redirect(
    self, node: Node, opened: list[int]
  ) -> tuple[tuple[int, ...], Optional[int]]:
    if node.type == 'heredoc_redirect':
      descriptor = node.child_by_field_name('descriptor')
      target = 0 if descriptor is None else int(self.text(descriptor))
      body = next((child for child in node.named_children if child.type == 'heredoc_body'), None)
      start = next((child for child in node.named_children if child.type == 'heredoc_start'), None)
      end = next((child for child in node.named_children if child.type == 'heredoc_end'), None)
      if start is None or end is None:
        self.refuse(node, 'malformed here-document')
      content_start = self.source.find(b'\n', start.end_byte, end.start_byte) + 1
      if content_start == 0:
        self.refuse(node, 'malformed here-document delimiter')
      operator = next(child.type for child in node.children if child.type in {'<<', '<<-'})
      strip_tabs = operator == '<<-'
      if any(marker in self.text(start) for marker in ("'", '"', '\\')):
        text = self.source[content_start : end.start_byte].decode()
        if strip_tabs:
          text = self._strip_heredoc_tabs(text)
      else:
        text = self._expand_heredoc(node, body, content_start, end.start_byte, strip_tabs)
      return (target,), self._input_descriptor(text, opened)
    if node.type == 'herestring_redirect':
      destination = node.child_by_field_name('destination')
      if destination is None and node.named_children:
        destination = node.named_children[-1]
      if destination is None:
        self.refuse(node, 'malformed here-string')
      values = self._expand_word(destination, split=False, pathname=False)
      descriptor = node.child_by_field_name('descriptor')
      target = 0 if descriptor is None else int(self.text(descriptor))
      return (target,), self._input_descriptor(''.join(values) + '\n', opened)
    if node.type != 'file_redirect':
      self.refuse(node, f'{node.type} redirect is not implemented')
    descriptor = node.child_by_field_name('descriptor')
    destination = node.child_by_field_name('destination')
    operator_node = next(
      (child for child in node.children if not child.is_named and child.type not in {'\n'}), None
    )
    if operator_node is None:
      self.refuse(node, 'redirect has no operator')
    operator = operator_node.type
    explicit = None if descriptor is None else int(self.text(descriptor))
    if operator in {'<&-', '>&-'}:
      return ((explicit if explicit is not None else (0 if operator == '<&-' else 1)),), None
    if destination is None:
      self.refuse(node, 'redirect has no destination')
    values = self._expand_word(destination)
    if len(values) != 1:
      self.refuse(node, 'redirect destination must expand to one path or descriptor')
    value = values[0]
    if operator in {'<&', '>&'} and (value.isdigit() or value == '-'):
      target = explicit if explicit is not None else (0 if operator == '<&' else 1)
      if value == '-':
        return (target,), None
      return (target,), int(value)
    if operator == '>&' and explicit is not None:
      self.refuse(node, 'a descriptor-prefixed >& accepts only a descriptor number or -')
    writing = operator in {'>', '>>', '>|', '&>', '&>>', '>&'}
    if not writing and operator != '<':
      self.refuse(node, f'redirect operator {operator!r} is not implemented')
    if writing and not self.writable and value not in {'/dev/null', '/dev/stdout', '/dev/stderr'}:
      self.refuse(node, 'writing a file requires writable files reach')
    path = Path(value)
    if not path.is_absolute():
      path = self.cwd / path
    flags = os.O_WRONLY | os.O_CREAT
    if operator in {'>>', '&>>'}:
      flags |= os.O_APPEND
    elif writing:
      flags |= os.O_TRUNC
    else:
      flags = os.O_RDONLY
    try:
      source = os.open(path, flags, 0o666)
    except OSError as error:
      os.write(2, f'brash: {value}: {error.strerror}\n'.encode())
      raise _RedirectFailed from error
    opened.append(source)
    if operator in {'&>', '&>>', '>&'}:
      return (1, 2), source
    return ((explicit if explicit is not None else (1 if writing else 0)),), source

  def _expand_heredoc(
    self,
    redirect: Node,
    body: Optional[Node],
    content_start: int,
    content_end: int,
    strip_tabs: bool,
  ) -> str:
    children = [] if body is None else list(body.named_children)
    children.extend(
      child
      for child in redirect.named_children
      if child != body
      and child.type not in {'heredoc_start', 'heredoc_end'}
      and child.end_byte > content_start
      and child.start_byte < content_end
    )
    children.sort(key=lambda child: child.start_byte)
    parts: list[str] = []
    line_start = True

    def append_literal(text: str) -> None:
      nonlocal line_start
      kept: list[str] = []
      for character in text:
        if strip_tabs and line_start and character == '\t':
          continue
        kept.append(character)
        line_start = character == '\n'
      parts.append(self._heredoc_content(''.join(kept)))

    def append_expansion(text: str, source: str) -> None:
      nonlocal line_start
      for character in source:
        line_start = character == '\n'
      parts.append(text)

    start = content_start
    for child in children:
      child_start = max(child.start_byte, content_start)
      child_end = min(child.end_byte, content_end)
      if child_start > start:
        append_literal(self.source[start:child_start].decode())
      child_source = self.source[child_start:child_end].decode()
      if child.type in {'simple_expansion', 'expansion'}:
        append_expansion(self._expand_variable(child), child_source)
      elif child.type == 'command_substitution':
        append_expansion(self._command_substitution(child), child_source)
      else:
        append_literal(child_source)
      start = child_end
    if start < content_end:
      append_literal(self.source[start:content_end].decode())
    return ''.join(parts)

  @staticmethod
  def _strip_heredoc_tabs(text: str) -> str:
    return ''.join(line.lstrip('\t') for line in text.splitlines(keepends=True))

  def _heredoc_content(self, text: str) -> str:
    backslashes = 0
    for character in text:
      if character == '\\':
        backslashes += 1
      else:
        if character == '`' and backslashes % 2 == 0:
          self.refuse(text, 'backticks are not implemented; use $(...) instead')
        backslashes = 0
    return self._unescape_heredoc(text)

  @staticmethod
  def _unescape_heredoc(text: str) -> str:
    return re.sub(r'\\([$`\\])', r'\1', text.replace('\\\n', ''))

  def _fork_and_wait(self, callback: Callable[[], int]) -> int:
    return self._wait_child_or_refuse(self._start_child(callback))

  def _wait_status(self, pid: int) -> int:
    _, status = os.waitpid(pid, 0)
    if os.WIFSIGNALED(status):
      return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)

  @staticmethod
  def _write(descriptor: int, text: str) -> None:
    os.write(descriptor, text.encode(errors='surrogateescape'))

  @staticmethod
  def _ansi_c(value: str) -> str:
    return codecs.decode(value, 'unicode_escape')

  @staticmethod
  def _unescape_literal(value: str) -> str:
    return re.sub(r'\\(.)', r'\1', value, flags=re.DOTALL)

  @staticmethod
  def _unescape_double(value: str) -> str:
    return re.sub(
      r'\\([$`"\\\n])', lambda match: '' if match.group(1) == '\n' else match.group(1), value
    )


def run(
  line: str,
  *,
  entries: Sequence[str],
  writable: bool,
  environment: Optional[dict[str, str]] = None,
) -> int:
  return Interpreter(
    line, entries=validate_entries(entries), writable=writable, environment=environment
  ).run()


def main(argv: list[str]) -> Optional[int]:
  parser = argparse.ArgumentParser(prog=argv[0], description='run a line under a brash policy')
  parser.add_argument('-c', '--command', required=True)
  parser.add_argument('--policy', type=Path, required=True)
  arguments = parser.parse_args(argv[1:])
  try:
    policy = Policy.read(arguments.policy)
    return run(arguments.command, entries=policy.entries, writable=policy.writable)
  except Refused as error:
    print(f'brash: {error.message}', file=sys.stderr)
    return REFUSED_STATUS
  except ValueError as error:
    parser.error(str(error))
  return None
