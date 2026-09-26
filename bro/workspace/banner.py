import os
from dataclasses import dataclass
from typing import Optional

from bro import summon
from bro.base.scope import launch_names
from bro.monitor import health, trail_pointer
from bro.worker_types import Launch
from bro.workspace.paths import ISOLATION_ENV

# six-line block-letter "B R O" rendered with box-drawing characters;
# shown on top of the `ride banner` output when the session carries a bro (RIDE_BRO).
_BRO_LOGO = """\
██████╗   ██████╗    ██████╗
██╔══██╗  ██╔══██╗  ██╔═══██╗
██████╔╝  ██████╔╝  ██║   ██║
██╔══██╗  ██╔══██╗  ██║   ██║
██████╔╝  ██║  ██║  ╚██████╔╝
╚═════╝   ╚═╝  ╚═╝   ╚═════╝\
"""


def _render_summon_targets(names: tuple[str, ...]) -> str:
  """the allow-list entries, each marked with the further names it answers to."""
  # lazy: the registry import pulls the bro class graph, which importers of
  # this module must not pay for up front
  from bro.registry import lineage

  marked: list[str] = []
  for name in names:
    inherited = lineage(name)[1:]
    marked.append(f'{name} ({", ".join(inherited)})' if len(inherited) > 0 else name)
  return ', '.join(marked)


@dataclass(frozen=True)
class SessionFacts:
  """the session facts `ride banner` renders, collected from the session environment.

  The fields are the documentation — what each renderer may show:
    - isolation — boxed or unboxed (RIDE_ISOLATION), or None outside a ride
    - name — workspace name (RIDE_WORKSPACE)
    - repo — resolved repository attachment (RIDE_REPO), or None when detached
    - bro — the bro the session runs as (RIDE_BRO)
    - host_workspace — launcher-side path to the workspace tree
    - container_workspace — '/workspace' in a managed container session, else None
    - exec_command — `ride exec <name>` for container sessions
    - ride_command — the canonical `ride solo|along …` invocation (RIDE_COMMAND)
    - recording_problem — set when the session-recorder health file reports a
      failing or a stopped recorder, so the banner can warn that the transcript
      is not being recorded
    - may_summon — the bros the session may summon, as its launch fixed them;
      empty when it may summon none, None when it was launched by a surface that
      publishes no list
    - talk — the fixed chat rights of this run's own quest, under the same publication rule
    - launch — the mission types and payload values the session may launch
    - party_member — the member name when this session joined an existing party
    - summoned — whether another session summoned this one and waits on its result
    - trail_id — the trail the session is being recorded into; None when nothing
      publishes one (recording off, or not started yet)
  """

  isolation: Optional[str]
  name: Optional[str]
  bro: Optional[str]
  host_workspace: Optional[str]
  container_workspace: Optional[str]
  exec_command: Optional[str]
  ride_command: Optional[str]
  recording_problem: Optional[str]
  may_summon: Optional[tuple[str, ...]]
  summoned: bool
  trail_id: Optional[str]
  repo: Optional[str] = None
  launch: Optional[Launch] = None
  party_member: Optional[str] = None
  talk: Optional[tuple[str, ...]] = None

  @classmethod
  def collect(
    cls, bro_override: Optional[str] = None, trail_id_override: Optional[str] = None
  ) -> 'SessionFacts':
    """collect session facts from env + session-local state for `ride banner`.

    Read-only; an invalid published isolation fails rather than being guessed.
    bro_override forces the `bro` fact regardless of
    `RIDE_BRO` — for in-process callers that know the bro they run: an in-process
    run (e.g. `bro run <bro>`) reads the launching environment, whose
    `RIDE_BRO` is the launcher's own persona or absent. trail_id_override likewise
    carries an in-process run's own trail, which no session recorder publishes.
    """
    isolation = os.environ.get(ISOLATION_ENV) or None
    if isolation not in (None, 'boxed', 'unboxed'):
      raise ValueError(f'invalid {ISOLATION_ENV}: {isolation!r}')
    name = os.environ.get('RIDE_WORKSPACE') or None
    repo = os.environ.get('RIDE_REPO') or None
    bro = bro_override if bro_override is not None else (os.environ.get('RIDE_BRO') or None)
    ride_command = os.environ.get('RIDE_COMMAND') or None
    host_workspace: Optional[str] = os.environ.get('RIDE_HOST_WORKSPACE') or None
    container_workspace: Optional[str] = (
      '/workspace' if isolation == 'boxed' and name is not None else None
    )

    exec_command = f'ride exec {name}' if isolation == 'boxed' and name is not None else None

    trail_id = trail_id_override
    if trail_id is None:
      pointer = trail_pointer.path()
      trail_id = trail_pointer.read(pointer) if pointer is not None else None

    return cls(
      isolation=isolation,
      name=name,
      repo=repo,
      bro=bro,
      host_workspace=host_workspace,
      container_workspace=container_workspace,
      exec_command=exec_command,
      ride_command=ride_command,
      recording_problem=health.problem(),
      may_summon=summon.may_summon(),
      launch=summon.launch(),
      talk=summon.talk(),
      party_member=summon.party_member(),
      summoned=summon.summoned(),
      trail_id=trail_id,
    )

  @property
  def display_name(self) -> str:
    return self.name if self.name is not None else '(unnamed)'

  def render_visual(self) -> str:
    """render the banner with ANSI colour + the Bro logo for bro sessions."""
    red = '\033[31m'
    bold = '\033[1m'
    bold_white = '\033[1;97m'  # bright-white bold — emphasis for the bro name
    dim = '\033[2m'
    reset = '\033[0m'

    lines: list[str] = []
    if self.recording_problem is not None:
      # most prominent slot — above the logo, red+bold so broken recording is
      # the first thing the eye lands on in a `ride exec` shell
      lines.append(f'{red}{bold}⚠ session recording {self.recording_problem}{reset}')
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
      ('ride session:', '', f'{bold}{self.display_name}{reset}'),
      (
        'repository:',
        '',
        f'{dim}(no repository attached){reset}'
        if self.repo is None
        else f'{dim}{self.repo}{reset}',
      ),
    ]

    if self.isolation == 'boxed':
      # /workspace inside, host bind-mount path below — both are useful and
      # packing them onto one line crowded the eye
      if self.container_workspace is not None:
        rows.append(('workspace:', '', self.container_workspace))
      else:
        rows.append(('workspace:', '', f'{dim}(boxed session without a workspace){reset}'))
      if self.host_workspace is not None:
        rows.append(('host path:', '', f'{dim}{self.host_workspace}{reset}'))
    elif self.isolation == 'unboxed' and self.host_workspace is not None:
      # an unboxed tree has no container boundary, so keep its launcher path prominent
      rows.append(('workspace:', '', f'{red}{self.host_workspace}{reset}'))
    else:
      rows.append(
        (
          'workspace:',
          '',
          f'{dim}(unknown — no RIDE_WORKSPACE / not a registered workspace){reset}',
        )
      )

    if self.exec_command is not None:
      # "docker shell" because the command opens a shell *inside* the docker
      # container — the label tracks the destination, not the host that launches it
      rows.append(('docker shell:', '', f'{dim}{self.exec_command}{reset}'))

    if self.party_member is not None:
      rows.append(('party:', '', f'{dim}joined as {self.party_member} — the tree is shared{reset}'))

    if self.summoned:
      rows.append(('summoned:', '', f'{dim}yes — a summoner is waiting on the answer{reset}'))

    if self.may_summon is not None:
      targets = _render_summon_targets(self.may_summon) if len(self.may_summon) > 0 else '(none)'
      rows.append(('may summon:', '', f'{dim}{targets}{reset}'))

    if self.talk is not None:
      rights = ', '.join(self.talk) or '(none)'
      rows.append(('talk:', '', f'{dim}{rights}{reset}'))

    if self.launch is not None:
      permissions = ', '.join(launch_names(self.launch, include_bros=False)) or '(none)'
      rows.append(('permits:', '', f'{dim}{permissions}{reset}'))

    if self.trail_id is not None:
      rows.append(('trail:', '', f'{dim}{self.trail_id}{reset}'))

    if self.ride_command is not None:
      rows.append(('launched:', '', f'{dim}{self.ride_command}{reset}'))

    # auto-align the value column to one space past the widest label
    width = max(len(label) for label, _, _ in rows)
    for label, label_style, value in rows:
      padded = label.ljust(width)
      styled_label = f'{label_style}{padded}{reset}' if len(label_style) > 0 else padded
      lines.append(f'{styled_label} {value}')

    return '\n'.join(lines)

  def render_llm(self) -> str:
    """render the agent-facing session facts as plain key:value lines."""
    lines: list[str] = []
    if self.recording_problem is not None:
      # first line so it lands in Claude's collapsed tool-output preview without
      # needing expansion; the agent should relay it to the user
      lines.append(f'session_recording: {self.recording_problem}')
    isolation = self.isolation if self.isolation is not None else 'none (not a ride session)'
    lines.append(f'isolation: {isolation}')
    lines.append(f'repo: {self.repo if self.repo is not None else "none (detached)"}')
    pairs: list[tuple[str, str]] = [
      ('name', 'name'),
      ('bro', 'bro'),
      ('host_workspace', 'workspace_host_path'),
      ('container_workspace', 'workspace_container_path'),
      ('exec_command', 'docker_shell_command'),
    ]
    if self.ride_command is not None:
      pairs.append(('ride_command', 'ride_command'))
    for attribute, label in pairs:
      value = getattr(self, attribute)
      if value is not None:
        lines.append(f'{label}: {value}')
    # stated either way, unlike the human banner's conditional row: an agent
    # asking whether it owes a summoner an answer must not have to read a `no`
    # out of a missing line
    lines.append(f'summoned: {"yes" if self.summoned else "no"}')
    if self.party_member is not None:
      lines.append(f'party: joined as {self.party_member} (shared tree)')
    if self.may_summon is not None:
      # spelled out when empty: "this session may summon nobody" is a different
      # answer from a launch surface that publishes no list at all
      targets = _render_summon_targets(self.may_summon) if len(self.may_summon) > 0 else 'none'
      lines.append(f'may_summon: {targets}')
    if self.talk is not None:
      lines.append(f'talk: {", ".join(self.talk) or "none"}')
    if self.launch is not None:
      permissions = ', '.join(launch_names(self.launch, include_bros=False)) or 'none'
      lines.append(f'permits: {permissions}')
    trail = self.trail_id if self.trail_id is not None else 'none (not published)'
    lines.append(f'trail_id: {trail}')
    return '\n'.join(lines)


def render_banner(
  llm: bool = False, bro: Optional[str] = None, trail_id: Optional[str] = None
) -> str:
  """render the banner string for the current session. visual (ANSI + logo) by
  default; --llm for plain key:value text. exposed so in-process callers (e.g.
  `call`'s opening bro message, the `bro::banner` service tool) can render
  without a shell-out. bro overrides the `bro` fact — an in-process run's
  environment carries the launcher's `RIDE_BRO` (or none), not the running
  bro's; None falls back to the env. trail_id overrides the recorded-trail fact
  the same way, for a run recording a trail of its own."""
  facts = SessionFacts.collect(bro_override=bro, trail_id_override=trail_id)
  return facts.render_llm() if llm else facts.render_visual()


def banner(llm: bool) -> int:
  """print the banner. visual by default; --llm for plain text."""
  print(render_banner(llm))
  return 0
