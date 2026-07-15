import os
import subprocess
from dataclasses import dataclass
from typing import Optional

import session_log_health
from cw.paths import _in_container, _project_root, _worktrees_dir
from cw.workspace import _format_ref

# six-line block-letter "B R O" rendered with box-drawing characters;
# shown on top of the `cw banner` output in --bro sessions.
_BRO_LOGO = """\
██████╗   ██████╗    ██████╗
██╔══██╗  ██╔══██╗  ██╔═══██╗
██████╔╝  ██████╔╝  ██║   ██║
██╔══██╗  ██╔══██╗  ██║   ██║
██████╔╝  ██║  ██║  ╚██████╔╝
╚═════╝   ╚═╝  ╚═╝   ╚═════╝\
"""

# tokens after which the rest of an unquoted launch command is the user-typed
# prompt — `dive-in --new <seed>`, `cw ss -p <prompt>`, `cw ss ... -- <prompt>`.
# rfind so the *last* marker wins if more than one is present.
_PROMPT_MARKERS = (' --new ', ' --prompt ', ' -p ', ' -- ')


def _split_launch_prompt(command: str) -> tuple[str, Optional[str]]:
  """split a launch command into (prefix, prompt) at the prompt marker, if any.

  prefix keeps the marker token (e.g. 'dive-in --new ') so callers can append a
  placeholder. returns (command, None) when no marker is present or nothing
  follows it.
  """
  for marker in _PROMPT_MARKERS:
    index = command.rfind(marker)
    if index < 0:
      continue
    head = command[: index + len(marker)]
    tail = command[index + len(marker) :].strip()
    if len(tail) > 0:
      return head, tail
  return command, None


@dataclass(frozen=True)
class SessionFacts:
  """the session facts `cw banner` renders, collected from env + /.dockerenv.

  The fields are the documentation — what each renderer may show:
    - in_container — /.dockerenv presence
    - name — workspace name (CW_NAME)
    - bro — the bro the session runs as (CW_BRO): the `--bro` bro, or a
      cw-session's persona
    - host_workspace — host-side path to the workspace dir
    - container_workspace — '/workspace' inside a container, else None
    - exec_command — `cw exec <name>` for container sessions
    - cw_command — the canonical `cw ss …` invocation (CW_COMMAND)
    - shell_command — the outer launch command (PPP_SHELL_COMMAND). For wrappers
      like dive-in this differs from cw_command; for direct `cw ss` use the two
      are equal and the banner suppresses the duplicate
    - prompt — the user-typed prompt extracted from shell_command when a
      `--new`/`-p`/`--prompt`/`--` marker is found; shell_command is shown with
      the prompt portion replaced by a placeholder in this case
    - sync_warning — set when the session-log sync health file reports a failure,
      so the banner can warn that logs aren't reaching S3
  """

  in_container: bool
  name: Optional[str]
  bro: Optional[str]
  host_workspace: Optional[str]
  container_workspace: Optional[str]
  exec_command: Optional[str]
  cw_command: Optional[str]
  shell_command: Optional[str]
  prompt: Optional[str]
  sync_warning: Optional[str]

  @classmethod
  def collect(cls, bro_override: Optional[str] = None) -> 'SessionFacts':
    """collect session facts from env + /.dockerenv for `cw banner`.

    read-only; never raises. bro_override forces the `bro` fact regardless of
    `CW_BRO` — for in-process callers that know the bro they run: an in-process
    run (e.g. `ask <bro> --host`) reads the launching environment, whose
    `CW_BRO` is the launcher's own persona or absent.
    """
    in_container = _in_container()
    name = os.environ.get('CW_NAME') or None
    bro = bro_override if bro_override is not None else (os.environ.get('CW_BRO') or None)
    cw_command = os.environ.get('CW_COMMAND') or None
    shell_command = os.environ.get('PPP_SHELL_COMMAND') or cw_command
    host_workspace: Optional[str] = os.environ.get('CW_HOST_WORKSPACE') or None
    container_workspace: Optional[str] = '/workspace' if in_container else None

    if not in_container and host_workspace is None and name is not None:
      # host worktree case — derive path from the project root + worktree name
      try:
        project = _project_root()
      except subprocess.CalledProcessError:
        project = None
      if project is not None:
        candidate = _worktrees_dir(project) / name
        if candidate.is_dir():
          host_workspace = str(candidate)

    exec_command = f'cw exec {name}' if in_container and name is not None else None

    prompt: Optional[str] = None
    if shell_command is not None:
      shell_command, prompt = _split_launch_prompt(shell_command)

    sync_warning: Optional[str] = None
    if session_log_health.is_failing():
      sync_warning = 'session-log sync FAILING — run setup/bootstrap_session_log.sh'

    return cls(
      in_container=in_container,
      name=name,
      bro=bro,
      host_workspace=host_workspace,
      container_workspace=container_workspace,
      exec_command=exec_command,
      cw_command=cw_command,
      shell_command=shell_command,
      prompt=prompt,
      sync_warning=sync_warning,
    )

  @property
  def display_ref(self) -> str:
    """the workspace name with the container `c:` prefix when in a container."""
    name = self.name if self.name is not None else '(unnamed)'
    return _format_ref(name, self.in_container)

  def render_visual(self) -> str:
    """render the banner with ANSI colour + the Bro logo for bro sessions."""
    red = '\033[31m'
    bold = '\033[1m'
    bold_white = '\033[1;97m'  # bright-white bold — emphasis for the @prompt@ slot
    dim = '\033[2m'
    reset = '\033[0m'

    lines: list[str] = []
    if self.sync_warning is not None:
      # most prominent slot — above the logo, red+bold so a broken sync is the
      # first thing the eye lands on in a `cw exec` shell
      lines.append(f'{red}{bold}⚠ {self.sync_warning}{reset}')
      lines.append('')
    if self.bro is not None:
      # annotate the bottom line of the logo with a `// <bro>` signature — dim
      # slashes (comment style), bro name in bright-white bold so it stands out
      logo_lines: list[str] = list(_BRO_LOGO.split('\n'))
      logo_lines[-1] = f'{logo_lines[-1]} {dim}//{reset} {bold_white}{self.bro}{reset}'
      lines.extend(logo_lines)
      lines.append('')

    # collect rows as (label, label_style, value) — label_style is applied to
    # the padded label so width math runs on the raw text, not on ANSI bytes
    rows: list[tuple[str, str, str]] = [
      ('cw session:', '', f'{bold}{self.display_ref}{reset}'),
    ]

    # `cw command` is the canonical `cw ss …` invocation; suppress when it's
    # the same string as `launched` (direct `cw ss` use) so we don't show the
    # same text twice
    if self.cw_command is not None and self.cw_command != self.shell_command:
      rows.append(('cw command:', '', f'{dim}{self.cw_command}{reset}'))

    if self.in_container:
      # /workspace inside, host bind-mount path below — both are useful and
      # packing them onto one line crowded the eye
      rows.append(('workspace:', '', str(self.container_workspace)))
      if self.host_workspace is not None:
        rows.append(('host path:', '', f'{dim}{self.host_workspace}{reset}'))
    elif self.host_workspace is not None:
      # host-mode worktree path printed in red as a "this is your actual repo
      # on disk — careless edits leak out of the session" reminder
      rows.append(('workspace:', '', f'{red}{self.host_workspace}{reset}'))
    else:
      rows.append(
        ('workspace:', '', f'{dim}(unknown — no CW_NAME / not a registered worktree){reset}')
      )

    if self.exec_command is not None:
      # "docker shell" because the command opens a shell *inside* the docker
      # container — the label tracks the destination, not the host that launches it
      rows.append(('docker shell:', '', f'{dim}{self.exec_command}{reset}'))

    if self.shell_command is not None:
      launched = f'{dim}{self.shell_command}{reset}'
      if self.prompt is not None:
        launched += f'{bold_white}@prompt@{reset}'
      rows.append(('launched:', '', launched))

    if self.prompt is not None:
      rows.append(('prompt:', bold_white, self.prompt))

    # auto-align the value column to one space past the widest label
    width = max(len(label) for label, _, _ in rows)
    for label, label_style, value in rows:
      padded = label.ljust(width)
      styled_label = f'{label_style}{padded}{reset}' if len(label_style) > 0 else padded
      lines.append(f'{styled_label} {value}')

    return '\n'.join(lines)

  def render_llm(self) -> str:
    """render the banner as plain key:value lines for an LLM Bash tool result.

    cw_command is suppressed when it equals launch_command (no wrapper involved).
    The user prompt is deliberately *not* emitted — the LLM already has it as
    the first message of the conversation, so re-printing it would just burn
    context. launch_command keeps its trailing marker (e.g. `dive-in --new `)
    as the signal that a seed prompt exists.
    """
    lines: list[str] = []
    if self.sync_warning is not None:
      # first line so it lands in Claude's collapsed tool-output preview without
      # needing expansion; the agent should relay it to the user
      lines.append('session_log_sync: FAILING — run setup/bootstrap_session_log.sh')
    lines.append(f'kind: {"container" if self.in_container else "host worktree"}')
    pairs: list[tuple[str, str]] = [
      ('name', 'name'),
      ('bro', 'bro'),
      ('host_workspace', 'workspace_host_path'),
      ('container_workspace', 'workspace_container_path'),
      ('exec_command', 'docker_shell_command'),
    ]
    if self.cw_command is not None and self.cw_command != self.shell_command:
      pairs.append(('cw_command', 'cw_command'))
    pairs.append(('shell_command', 'launch_command'))
    for attribute, label in pairs:
      value = getattr(self, attribute)
      if value is not None:
        lines.append(f'{label}: {value}')
    return '\n'.join(lines)


def render_banner(llm: bool = False, bro: Optional[str] = None) -> str:
  """render the banner string for the current session. visual (ANSI + logo) by
  default; --llm for plain key:value text. exposed so in-process callers (e.g.
  `call`'s opening bro message, the `bro::banner` service tool) can render
  without a shell-out. bro overrides the `bro` fact — an in-process run's
  environment carries the launcher's `CW_BRO` (or none), not the running
  bro's; None falls back to the env."""
  facts = SessionFacts.collect(bro_override=bro)
  return facts.render_llm() if llm else facts.render_visual()


def banner(llm: bool) -> int:
  """print the banner. visual by default; --llm for plain text."""
  print(render_banner(llm))
  return 0
