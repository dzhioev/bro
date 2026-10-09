import contextlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from bro import brash
from bro.base.spawn import console_script


def _environment(path: Path | None = None) -> dict[str, str]:
  return {
    'HOME': '/tmp',
    'PATH': os.environ['PATH'] if path is None else f'{path}:{os.environ["PATH"]}',
  }


def _run(line: str, *entries: str, writable: bool = False) -> int:
  return brash.run(line, entries=entries, writable=writable, environment=_environment())


def _run_command(
  line: str,
  *entries: str,
  writable: bool,
  environment: dict[str, str],
  directory: Path,
  policy: Path,
) -> subprocess.CompletedProcess[bytes]:
  """run `line` through the `brash` command, which applies a descriptor-numbered
  redirect to a process of its own rather than to the descriptors this test
  process holds, its test runner's among them."""
  policy.write_text(
    json.dumps({'commands': list(entries), 'files': 'write' if writable else 'read'})
  )
  return subprocess.run(
    [console_script('brash'), '--policy', str(policy), '-c', line],
    cwd=directory,
    env=environment,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    check=False,
  )


@contextlib.contextmanager
def _redirected_descriptor(target: int, source: int):
  original = os.dup(target)
  os.dup2(source, target)
  try:
    yield
  finally:
    os.dup2(original, target)
    os.close(original)


class TestCommandList:
  def test_words_match_by_position(self):
    pattern = brash.parse_entry('gh pr view * --json=*')

    assert pattern.matches(['gh', 'pr', 'view', '12', '--json=title'])
    assert not pattern.matches(['gh', 'pr', 'view', '12', 'body'])
    assert not pattern.matches(['gh', 'pr', 'view', '--web', '12'])

  def test_trailing_ellipsis_admits_no_further_arguments(self):
    pattern = brash.parse_entry('git log ...')

    assert pattern.matches(['git', 'log'])
    assert pattern.matches(['git', 'log', '--oneline', '-2'])

  def test_quoted_patterns_are_literal(self):
    star = brash.parse_entry("grep -E 'a*b' ...")
    ellipsis = brash.parse_entry("printf '...'")

    assert star.matches(['grep', '-E', 'a*b', 'file'])
    assert not star.matches(['grep', '-E', 'axxb', 'file'])
    assert ellipsis.matches(['printf', '...'])

  def test_empty_quoted_word_is_a_real_argument(self):
    pattern = brash.parse_entry("printf ''")

    assert pattern.matches(['printf', ''])
    assert not pattern.matches(['printf'])

  def test_prefix_assignments_are_part_of_the_pattern(self):
    pattern = brash.parse_entry('NO_COLOR=* gh pr list')

    assert pattern.matches(['NO_COLOR=1', 'gh', 'pr', 'list'])
    assert not pattern.matches(['gh', 'pr', 'list'])

  @pytest.mark.parametrize(
    ('entry', 'message'),
    [
      ('', 'non-empty'),
      ('git status | cat', 'one simple command'),
      ('! git status', 'one simple command'),
      ('{ git status; }', 'one simple command'),
      ('if true', 'one simple command'),
      ('git\nstatus', 'one simple command'),
      ('git $(program)', 'literal words'),
      ('* status', 'literal program'),
      ('git ... status', 'only as the final'),
      ('x=1', 'name a program'),
      ('echo hi', 'builtin'),
      ('export X=1', 'does not implement'),
      ("git 'status", 'unterminated quote'),
    ],
  )
  def test_invalid_entries_are_refused(self, entry, message):
    with pytest.raises(ValueError, match=message):
      brash.parse_entry(entry)

  def test_duplicate_entries_are_refused(self):
    with pytest.raises(ValueError, match='duplicate'):
      brash.validate_entries(['git status', 'git status'])


class TestLanguage:
  def test_lists_booleans_pipelines_and_negation(self, capfd):
    status = _run('printf left | cat && ! false || printf wrong', 'printf ...', 'cat')

    assert status == 0
    assert capfd.readouterr().out == 'left'

  def test_subshell_does_not_change_the_calling_directory(self, capfd, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert _run('(cd /; pwd); pwd') == 0
    assert capfd.readouterr().out == f'/\n{tmp_path}\n'

  def test_group_changes_the_calling_directory(self, capfd, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert _run('{ cd /; pwd; }; pwd') == 0
    assert capfd.readouterr().out == '/\n/\n'

  def test_if_elif_and_else(self, capfd):
    line = 'if false; then echo no; elif true; then echo yes; else echo no; fi'

    assert _run(line) == 0
    assert capfd.readouterr().out == 'yes\n'

  def test_for_while_until_and_case(self, capfd):
    line = (
      'for x in a b; do echo "$x"; done; '
      'x=; while false; do echo no; done; until true; do echo no; done; '
      'case b in a) echo no;; b|c) echo yes;; esac'
    )

    assert _run(line) == 0
    assert capfd.readouterr().out == 'a\nb\nyes\n'

  def test_quoted_case_metacharacters_are_literal(self, capfd):
    line = "case xyz in 'x*') echo wrong;; *) echo no;; esac"

    assert _run(line) == 0
    assert capfd.readouterr().out == 'no\n'

  def test_background_job_and_plain_wait(self, capfd):
    assert _run('printf background & wait', 'printf ...') == 0
    assert capfd.readouterr().out == 'background'

  def test_comments_and_assignments(self, capfd):
    line = 'x=$(printf value); true && # between operands\necho "$x"'

    assert _run(line, 'printf value') == 0
    assert capfd.readouterr().out == 'value\n'

  def test_comment_after_elif_is_not_the_condition(self, capfd):
    line = 'if false; then :; elif # before condition\n true; then echo yes; fi'

    assert _run(line) == 0
    assert capfd.readouterr().out == 'yes\n'

  @pytest.mark.parametrize(
    ('line', 'output'),
    [
      ('if # note\n true; then echo yes; fi', 'yes\n'),
      ('for x in one; do # note\n echo "$x"; done', 'one\n'),
      ('while # note\n false; do echo no; done; echo yes', 'yes\n'),
      ('until # note\n true; do echo no; done; echo yes', 'yes\n'),
      ('case x in # note\n x) # body\n echo yes;; esac', 'yes\n'),
      ('! # note\n false; echo yes', 'yes\n'),
      ('(# note\n echo yes)', 'yes\n'),
      ('{ # note\n echo yes; }', 'yes\n'),
    ],
  )
  def test_comments_are_ignored_in_every_control_position(self, line, output, capfd):
    assert _run(line) == 0
    assert capfd.readouterr().out == output

  def test_comments_in_pipelines_are_not_operands(self, capfd):
    line = 'printf value | # between commands\ncat'

    assert _run(line, 'printf value', 'cat') == 0
    assert capfd.readouterr().out == 'value'


class TestWords:
  def test_quoting_splitting_globbing_tilde_and_ansi_c(self, capfd, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'one.txt').write_text('')
    (tmp_path / 'two.txt').write_text('')
    line = "printf '<%s>\\n' '$HOME' \"$HOME\" $value *.txt '~' ~ $'a\\tb'"

    status = brash.run(
      'value="a b"; ' + line,
      entries=("printf '<%s>\\n' ...",),
      writable=False,
      environment={'HOME': '/home/person', 'PATH': os.environ['PATH']},
    )

    assert status == 0
    assert capfd.readouterr().out.splitlines() == [
      '<$HOME>',
      '</home/person>',
      '<a>',
      '<b>',
      '<one.txt>',
      '<two.txt>',
      '<~>',
      '</home/person>',
      '<a\tb>',
    ]

  def test_command_and_process_substitution(self, capfd):
    line = 'cat <(printf input); printf output | tee >(cat)'

    assert _run(line, 'cat ...', 'printf ...', 'tee ...') == 0
    assert sorted(capfd.readouterr().out) == sorted('inputoutputoutput')

  def test_output_process_substitution_precedes_the_command_redirect(self, capfd):
    line = 'printf output | tee >(cat) >/dev/null'

    assert _run(line, 'cat ...', 'printf ...', 'tee ...') == 0
    assert capfd.readouterr().out == 'output'

  def test_split_fields_are_each_globbed(self, capfd, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'one.txt').touch()
    (tmp_path / 'two.py').touch()

    assert _run("x='*.txt *.py'; printf '<%s>\\n' $x", 'printf ...') == 0
    assert capfd.readouterr().out == '<one.txt>\n<two.py>\n'

  def test_brace_patterns_include_alphabetic_and_padded_ranges(self, capfd):
    line = 'printf "%s " pre{a,b}{1..2} {a..c} {01..03}'

    assert _run(line, 'printf ...') == 0
    assert capfd.readouterr().out == 'prea1 prea2 preb1 preb2 a b c 01 02 03 '

  @pytest.mark.parametrize(
    'line',
    [
      'echo ${x:-fallback}',
      'echo $((1 + 1))',
      'echo `pwd`',
      'echo $"translated"',
      'x[0]=value',
      'function f { echo no; }',
      '[[ -f x ]]',
      'echo one \\\n        two',
    ],
  )
  def test_unimplemented_word_and_language_forms_are_refused(self, line):
    with pytest.raises(brash.Refused):
      _run(line)


class TestBuiltinsAndPrograms:
  def test_builtins_need_no_entries(self, capfd, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert _run('pwd; echo -n value; :; true; false') == 1
    assert capfd.readouterr().out == f'{tmp_path}\nvalue'

  def test_exit_stops_the_line(self, capfd):
    assert _run('echo before; exit 7; echo after') == 7
    assert capfd.readouterr().out == 'before\n'

  def test_literal_programs_are_all_checked_before_execution(self, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(brash.Refused, match='program.*cat'):
      _run('printf ran > marker; cat marker', 'printf ...', writable=True)
    assert not (tmp_path / 'marker').exists()

  def test_real_argv_is_checked_after_expansion(self):
    with pytest.raises(brash.Refused, match='expanded command'):
      _run('target="one two"; printf $target', 'printf *')

  def test_prefix_assignments_reach_a_builtin_temporarily(self, capfd, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert _run('HOME=/ cd; pwd; cd; pwd') == 0
    assert capfd.readouterr().out == '/\n/tmp\n'

  def test_prefix_assignments_reach_the_program(self, capfd):
    line = 'VALUE=seen sh -c \'printf %s "$VALUE"\''

    assert _run(line, 'VALUE=* sh -c \'printf %s "$VALUE"\'') == 0
    assert capfd.readouterr().out == 'seen'

  def test_cd_updates_relative_execution_and_pwd(self, capfd, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)
    probe = tmp_path / 'probe'
    probe.write_text('#!/bin/sh\nprintf relative')
    probe.chmod(0o755)

    assert _run(f'cd {tmp_path}; ./probe; echo "$PWD"', './probe') == 0
    assert capfd.readouterr().out == f'relative{tmp_path}\n'

  def test_unknown_program_has_bash_status(self, capfd):
    assert _run('not-installed', 'not-installed') == brash.NOT_FOUND_STATUS
    assert 'command not found' in capfd.readouterr().err

  def test_existing_non_executable_program_has_bash_status(self, tmp_path, capfd):
    program = tmp_path / 'nonexec'
    program.write_text('echo no')

    status = brash.run(
      'nonexec',
      entries=('nonexec',),
      writable=False,
      environment=_environment(tmp_path),
    )

    assert status == brash.REFUSED_STATUS
    assert 'Permission denied' in capfd.readouterr().err

  def test_executable_text_without_shebang_uses_shell_fallback(self, tmp_path, capfd):
    program = tmp_path / 'textscript'
    program.write_text('[[ x == x ]] && echo ran')
    program.chmod(0o755)

    status = brash.run(
      'textscript',
      entries=('textscript',),
      writable=False,
      environment=_environment(tmp_path),
    )

    assert status == 0
    assert capfd.readouterr().out == 'ran\n'

  def test_signaled_program_uses_bash_status(self, capfd):
    line = 'sh -c \'kill -TERM $$\'; echo "$?"'

    assert _run(line, "sh -c 'kill -TERM $$'") == 0
    assert capfd.readouterr().out == '143\n'

  @pytest.mark.parametrize(
    'line',
    [
      "(sh -c 'exit 126'); echo after",
      "sh -c 'exit 126' | cat; echo after",
      'echo "$(sh -c \'exit 126\')"; echo after',
    ],
  )
  def test_ordinary_126_does_not_signal_a_refusal(self, line, capfd):
    assert _run(line, "sh -c 'exit 126'", 'cat') == 0
    assert capfd.readouterr().out.endswith('after\n')

  @pytest.mark.parametrize(
    'line',
    ['x=not-listed; ($x); echo after', 'x=not-listed; echo "$($x)"; echo after'],
  )
  def test_child_refusal_stops_the_line_without_a_python_traceback(self, line, capfd):
    assert _run(line, 'printf ...') == brash.REFUSED_STATUS
    captured = capfd.readouterr()
    assert captured.out == ''
    assert captured.err.count('refused') == 1
    assert 'Traceback' not in captured.err

  @pytest.mark.parametrize('program', ['eval', 'source', 'read', 'export', 'declare', 'set'])
  def test_unimplemented_bash_builtins_are_refused(self, program):
    with pytest.raises(brash.Refused):
      brash.run(program, entries=(), writable=False, environment=_environment())


class TestVariables:
  def test_plain_and_for_variables_are_local(self, capfd):
    assert _run('x=one; for y in two; do echo "$x $y"; done') == 0
    assert capfd.readouterr().out == 'one two\n'

  @pytest.mark.parametrize('line', ['PATH=/tmp; pwd', 'for PATH in /tmp; do pwd; done'])
  def test_environment_variables_cannot_be_assigned(self, line):
    with pytest.raises(brash.Refused, match='environment variable'):
      _run(line)

  def test_special_parameters(self, capfd):
    assert _run('false; echo "$? $# $@ $* $- $_"') == 0
    assert capfd.readouterr().out == '1 0    false\n'

  def test_quoted_at_with_no_parameters_contributes_no_argument(self, capfd):
    line = 'sh -c \'echo $#\' marker "$@"'

    assert _run(line, "sh -c 'echo $#' marker ...") == 0
    assert capfd.readouterr().out == '0\n'

  def test_shell_pid_is_stable_in_a_subshell(self, capfd):
    assert _run('echo $$; (echo $$)') == 0
    first, second = capfd.readouterr().out.splitlines()
    assert first == second


class TestRedirects:
  @pytest.mark.parametrize('operator', ['>', '>>', '>|', '&>', '&>>', '>&', '2>', '2>>', '2>|'])
  def test_file_writes_require_writable_files(self, operator, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    line = f'echo value {operator} output'

    with pytest.raises(brash.Refused, match='writable files'):
      _run(line)
    assert _run(line, writable=True) == 0
    assert (tmp_path / 'output').exists()

  @pytest.mark.parametrize('operator', ['<', '3<'])
  def test_file_reads_are_admitted_at_every_files_level(self, operator, tmp_path):
    (tmp_path / 'input').write_text('value')
    line = f'cat {operator} input' if operator == '<' else f'cat {operator} input </dev/null'

    completed = _run_command(
      line,
      'cat',
      writable=False,
      environment=_environment(),
      directory=tmp_path,
      policy=tmp_path / 'policy.json',
    )
    assert completed.returncode == 0, completed.stdout

  @pytest.mark.parametrize('redirect', ['2>&1', '<&0', '2>&-', '</dev/null', '>/dev/null'])
  def test_descriptor_and_device_redirects_are_always_admitted(self, redirect):
    assert _run(f'echo value {redirect}') == 0

  def test_here_document_and_here_string(self, capfd):
    document = "cat <<'EOF'\n$value\nEOF\ncat <<< value"

    assert _run(document, 'cat') == 0
    assert capfd.readouterr().out == '$value\nvalue\n'

  def test_a_redirect_only_command_is_refused(self, tmp_path):
    source = tmp_path / 'source'
    source.write_text('secret')

    with pytest.raises(brash.Refused, match='redirects alone'):
      _run(f'echo "$(< {source})"')

  def test_here_document_delimiter_quoting_and_expansion(self, capfd):
    line = 'false; cat <<EOF\n$? $(printf value) \\$HOME\nEOF\ncat <<\\EOF\n$HOME\nEOF'

    assert _run(line, 'cat', 'printf value') == 0
    assert capfd.readouterr().out == '1 value $HOME\n$HOME\n'

  @pytest.mark.parametrize(
    'line',
    [
      'cat <<-EOF\n\tvalue\nEOF',
      'cat <<-EOF\n\t\tvalue\nEOF',
      'cat <<-EOF\n\tvalue\n\tEOF',
      "cat <<-'EOF'\n\tvalue\n\tEOF",
    ],
  )
  def test_tab_stripping_here_document_operator(self, line, capfd):
    assert _run(line, 'cat') == 0
    assert capfd.readouterr().out == 'value\n'

  def test_tab_stripping_follows_source_lines_not_expansion_output(self, capfd):
    line = 'cat <<-EOF\n$X\tb\nEOF'

    status = brash.run(
      line,
      entries=('cat',),
      writable=False,
      environment={**_environment(), 'X': 'a\n'},
    )

    assert status == 0
    assert capfd.readouterr().out == 'a\n\tb\n'

  def test_unquoted_here_document_keeps_escaped_backticks(self, capfd):
    line = 'cat <<EOF\n\\`literal\\`\nEOF'

    assert _run(line, 'cat') == 0
    assert capfd.readouterr().out == '`literal`\n'

  def test_unquoted_here_document_refuses_backticks(self):
    line = 'cat <<EOF\n`printf value`\nEOF'

    with pytest.raises(brash.Refused, match=r'use \$\(\.\.\.\) instead'):
      _run(line, 'cat', 'printf value')

  def test_descriptor_prefixed_here_document(self, tmp_path):
    line = "sh -c 'cat <&3' 3<<'EOF'\nvalue\nEOF"

    completed = _run_command(
      line,
      "sh -c 'cat <&3'",
      writable=False,
      environment=_environment(),
      directory=tmp_path,
      policy=tmp_path / 'policy.json',
    )
    assert (completed.returncode, completed.stdout) == (0, b'value\n')

  def test_large_here_document_does_not_block_before_the_reader_starts(self):
    body = 'x' * 100_000
    line = f"cat <<'EOF' >/dev/null\n{body}\nEOF"

    assert _run(line, 'cat') == 0

  def test_redirect_io_failure_is_a_command_failure(self, capfd):
    assert _run('cat < absent', 'cat') == 1
    assert 'No such file or directory' in capfd.readouterr().err

  def test_bad_descriptor_is_a_command_failure_without_a_traceback(self, capfd):
    assert _run('echo value 2>&99') == 1
    error = capfd.readouterr().err
    assert 'Bad file descriptor' in error
    assert 'Traceback' not in error

  def test_descriptor_prefixed_file_duplication_is_refused(self):
    with pytest.raises(brash.Refused):
      _run('echo value 2>& output', writable=True)


class TestRefusalAndCli:
  def test_refusal_names_the_command_reason_and_entries(self):
    with pytest.raises(brash.Refused) as raised:
      _run('cat file', 'printf ...')

    message = raised.value.message
    assert "'cat'" in message
    assert 'no command-list entry' in message
    assert '`printf ...`' in message

  def test_cli_reads_the_policy_and_returns_126_on_refusal(self, tmp_path, capfd):
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'commands': ['printf ...'], 'files': 'read'}))

    assert brash.main(['brash', '--policy', str(policy), '-c', 'cat file']) == 126
    assert 'brash: refused' in capfd.readouterr().err

  @pytest.mark.parametrize(
    'policy',
    [
      [],
      {'commands': 'printf ...', 'files': 'read'},
      {'commands': [], 'files': 'other'},
      {'commands': [], 'files': 'read', 'other': True},
    ],
  )
  def test_policy_fails_fast_on_malformed_data(self, policy, tmp_path):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(policy))

    with pytest.raises(ValueError):
      brash.Policy.read(path)


_DIFFERENTIAL_LINES = (
  ('echo plain "two words"', (), False),
  ('false; echo "$?"', (), False),
  ('true && echo yes; false || echo fallback', (), False),
  ('! false', (), False),
  ('printf left | cat', ('printf ...', 'cat'), False),
  ('printf background & wait', ('printf ...',), False),
  ('(cd /; echo subshell); { echo group; }', (), False),
  ('if false; then echo no; elif true; then echo yes; else echo no; fi', (), False),
  ('for x in one "two three"; do echo "$x"; done', (), False),
  ('while false; do echo no; done; until true; do echo no; done', (), False),
  ("case xyz in 'x*') echo no;; x*) echo yes;; esac", (), False),
  ('true && # comment\necho yes', (), False),
  ('x="one two"; printf "<%s>\\n" $x', ('printf ...',), False),
  ('printf "<%s>\\n" "$HOME" $\'a\\tb\'', ('printf ...',), False),
  ('printf "%s " pre{a,b}{1..2} {a..c} {01..03}', ('printf ...',), False),
  ('printf substitution="$(printf value)"', ('printf ...',), False),
  ('cat <(printf input)', ('cat ...', 'printf ...'), False),
  ('printf output | tee >(cat)', ('printf ...', 'tee ...', 'cat'), False),
  ('printf value > output; cat < output', ('printf ...', 'cat'), True),
  ('printf next >> output; cat < output', ('printf ...', 'cat'), True),
  ('cat <<< value', ('cat',), False),
  ("cat <<'EOF'\n$value\nEOF", ('cat',), False),
  ('false; cat <<EOF\n$? $(printf value) \\$HOME\nEOF', ('cat', 'printf value'), False),
  ('cat <<\\EOF\n$HOME\nEOF', ('cat',), False),
  ('cat <<EOF\n\\`literal\\`\nEOF', ('cat',), False),
  ('cat <<-EOF\n\tvalue\nEOF', ('cat',), False),
  ('cat <<-EOF\n\t\tvalue\nEOF', ('cat',), False),
  ('cat <<-EOF\n\tvalue\n\tEOF', ('cat',), False),
  ("cat <<-'EOF'\n\tvalue\n\tEOF", ('cat',), False),
  ('cat <<-EOF\n$X\tb\nEOF', ('cat',), False),
  ("sh -c 'cat <&3' 3<<'EOF'\nvalue\nEOF", ("sh -c 'cat <&3'",), False),
  ('sh -c \'echo $#\' marker "$@"', ("sh -c 'echo $#' marker ...",), False),
  ('printf error >&2 2>&1', ('printf ...',), False),
  ('VALUE=seen sh -c \'printf %s "$VALUE"\'', ('VALUE=* sh -c *',), False),
  ("sh -c 'kill -TERM $$'", ("sh -c 'kill -TERM $$'",), False),
)


class TestDifferential:
  @pytest.mark.parametrize(('line', 'entries', 'writable'), _DIFFERENTIAL_LINES)
  def test_each_implemented_form_matches_bash(self, line, entries, writable, tmp_path):
    bash_directory = tmp_path / 'bash'
    brash_directory = tmp_path / 'brash'
    bash_directory.mkdir()
    brash_directory.mkdir()
    for directory in (bash_directory, brash_directory):
      (directory / 'one.txt').touch()
      (directory / 'two.py').touch()

    environment = {'HOME': '/home/person', 'PATH': os.environ['PATH'], 'X': 'a\n'}
    bash_result = subprocess.run(
      ['bash', '-c', line],
      cwd=bash_directory,
      env=environment,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      check=False,
    )
    brash_result = _run_command(
      line,
      *entries,
      writable=writable,
      environment=environment,
      directory=brash_directory,
      policy=tmp_path / 'policy.json',
    )

    bash_status = (
      128 - bash_result.returncode if bash_result.returncode < 0 else bash_result.returncode
    )
    assert (brash_result.returncode, brash_result.stdout) == (bash_status, bash_result.stdout)

  def test_corpus_matches_bash(self, tmp_path):
    lines = [
      'probe one "two three"',
      'value="two three"; probe one "$value"',
      'probe left | probe right',
      'if probe condition; then probe body; fi',
      'for x in one two; do probe "$x"; done',
      "probe <<'EOF'\ninput\nEOF",
      'probe first && probe second',
      '(probe subshell); { probe group; }',
    ]
    entries = ('probe ...',)

    for index, line in enumerate(lines):
      bash_directory = tmp_path / f'bash-{index}'
      brash_directory = tmp_path / f'brash-{index}'
      bash_directory.mkdir()
      brash_directory.mkdir()
      bash_result = self._run_with_probe('bash', line, bash_directory, entries)
      brash_result = self._run_with_probe('brash', line, brash_directory, entries)
      assert brash_result == bash_result, line

  def _run_with_probe(self, runner, line, directory, entries):
    probe = directory / 'probe'
    probe.write_text(
      '#!/usr/bin/env python3\n'
      'import json, os, sys\n'
      'stdin = sys.stdin.read()\n'
      'with open(os.environ["LOG"], "a") as stream:\n'
      '  stream.write(json.dumps([sys.argv[1:], stdin]) + "\\n")\n'
      'sys.stdout.write(" ".join(sys.argv[1:]))\n'
    )
    probe.chmod(0o755)
    environment = {'PATH': f'{directory}:{os.environ["PATH"]}', 'LOG': str(directory / 'log')}
    if runner == 'bash':
      completed = subprocess.run(
        ['bash', '-c', line], cwd=directory, env=environment, capture_output=True, check=False
      )
      status = completed.returncode
      output = completed.stdout
    else:
      read_descriptor, write_descriptor = os.pipe()
      with _redirected_descriptor(1, write_descriptor):
        os.close(write_descriptor)
        status = brash.run(line, entries=entries, writable=False, environment=environment)
      with os.fdopen(read_descriptor, 'rb') as stream:
        output = stream.read()
    log = (directory / 'log').read_text().splitlines() if (directory / 'log').exists() else []
    return status, output, sorted(log)


def _generated_active_sentinel_lines() -> tuple[str, ...]:
  command_contexts = (
    'sentinel',
    'sentinel | printf sink',
    'printf source | sentinel',
    'sentinel && printf next',
    'false || sentinel',
    '(sentinel)',
    '{ sentinel; }',
    'if sentinel; then printf body; fi',
    'if true; then sentinel; fi',
    'for x in one; do sentinel; done',
    'while sentinel; do printf body; done',
    'until sentinel; do printf body; done',
    'case x in x) sentinel;; esac',
    'sentinel & wait',
  )
  expansion_contexts = (
    '%s',
    'printf %s',
    'x=%s; printf "$x"',
    'for x in %s; do printf "$x"; done',
    'case %s in value) printf match;; esac',
    'if printf %s; then true; fi',
    'cat <<EOF\n%s\nEOF',
  )
  expansions = ('$(sentinel)', '"$(sentinel)"', 'pre$(sentinel)post', '"pre$(sentinel)post"')
  generated = [*command_contexts, 'printf <(sentinel)', 'printf >(sentinel)']
  generated.extend(
    context % expansion for context in expansion_contexts for expansion in expansions
  )
  return tuple(generated)


def _generated_single_quoted_lines() -> tuple[str, ...]:
  payload = "'a[$(sentinel)]'"
  return (
    f"printf '%s' {payload}",
    f'x={payload}; printf \'%s\' "$x"',
    f'for x in {payload}; do printf \'%s\' "$x"; done',
    f'case {payload} in {payload}) printf ok;; esac',
    f"printf '%s' pre{payload}",
    f"cat <<'EOF'\n{payload}\nEOF",
  )


class TestGeneratedAdmission:
  @pytest.mark.parametrize('line', _generated_active_sentinel_lines())
  def test_no_unlisted_sentinel_starts_on_any_execution_path(self, line, tmp_path):
    marker = tmp_path / 'started'
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text(f'#!/bin/sh\ntouch {marker}\n')
    sentinel.chmod(0o755)

    with pytest.raises(brash.Refused):
      brash.run(
        line,
        entries=('printf ...', 'cat ...'),
        writable=False,
        environment=_environment(tmp_path),
      )
    assert not marker.exists()

  @pytest.mark.parametrize('line', _generated_single_quoted_lines())
  def test_single_quoted_payload_never_starts_the_sentinel(self, line, tmp_path):
    marker = tmp_path / 'started'
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text(f'#!/bin/sh\ntouch {marker}\n')
    sentinel.chmod(0o755)

    assert (
      brash.run(
        line,
        entries=('printf ...', 'cat ...'),
        writable=False,
        environment=_environment(tmp_path),
      )
      == 0
    )
    assert not marker.exists()
