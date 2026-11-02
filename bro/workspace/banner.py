import os
from dataclasses import dataclass
from typing import Optional

from bro import summon
from bro.llm.resolved import ResolvedRecipe
from bro.monitor import health, trail_pointer
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
  """Session-local environment, live authority, and recording facts."""

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
  permits: Optional[tuple[str, ...]] = None
  may_give: Optional[tuple[str, ...]] = None
  summon_entries: Optional[tuple[str, ...]] = None
  party_member: Optional[str] = None
  talk: Optional[tuple[str, ...]] = None
  resolved_recipe: ResolvedRecipe | None = None

  @classmethod
  def collect(
    cls,
    bro_override: Optional[str] = None,
    trail_id_override: Optional[str] = None,
    resolved_recipe: ResolvedRecipe | None = None,
  ) -> 'SessionFacts':
    """Collect session facts, with explicit run facts overriding the environment."""
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

    from bro.authority_view import read

    authority = read()
    return cls(
      isolation=isolation,
      name=name,
      repo=repo,
      bro=bro,
      resolved_recipe=resolved_recipe
      if resolved_recipe is not None
      else ResolvedRecipe.from_environment(),
      host_workspace=host_workspace,
      container_workspace=container_workspace,
      exec_command=exec_command,
      ride_command=ride_command,
      recording_problem=health.problem(),
      may_summon=None if authority is None else tuple(authority['may_summon']),
      permits=None if authority is None else tuple(authority['permits']),
      may_give=None if authority is None else tuple(authority['may_give']),
      summon_entries=None if authority is None else tuple(authority['summon_entries']),
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

    if self.resolved_recipe is not None:
      rows.append(('llm:', '', self.resolved_recipe.display()))

    if self.exec_command is not None:
      # "docker shell" because the command opens a shell *inside* the docker
      # container — the label tracks the destination, not the host that launches it
      rows.append(('docker shell:', '', f'{dim}{self.exec_command}{reset}'))

    if self.party_member is not None:
      rows.append(('party:', '', f'{dim}joined as {self.party_member} — the tree is shared{reset}'))

    if self.summoned:
      rows.append(('summoned:', '', f'{dim}yes — a summoner is waiting on the answer{reset}'))

    if self.may_summon is not None:
      targets = (
        ', '.join(self.summon_entries)
        if self.summon_entries
        else _render_summon_targets(self.may_summon)
        if self.may_summon
        else '(none)'
      )
      rows.append(('may summon:', '', f'{dim}{targets}{reset}'))

    if self.talk is not None:
      rights = ', '.join(self.talk) or '(none)'
      rows.append(('talk:', '', f'{dim}{rights}{reset}'))

    for label, entries in (('permits:', self.permits), ('may give:', self.may_give)):
      if entries is not None:
        rows.append((label, '', f'{dim}{", ".join(entries) or "(none)"}{reset}'))

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

  def render_plain(self) -> str:
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
    if self.resolved_recipe is not None:
      lines.append(f'llm: {self.resolved_recipe.display()}')
    lines.append(f'summoned: {"yes" if self.summoned else "no"}')
    if self.party_member is not None:
      lines.append(f'party: joined as {self.party_member} (shared tree)')
    if self.may_summon is not None:
      # spelled out when empty: "this session may summon nobody" is a different
      # answer from a launch surface that publishes no list at all
      targets = (
        ', '.join(self.summon_entries)
        if self.summon_entries
        else _render_summon_targets(self.may_summon)
        if self.may_summon
        else 'none'
      )
      lines.append(f'may_summon: {targets}')
    if self.talk is not None:
      lines.append(f'talk: {", ".join(self.talk) or "none"}')
    for label, entries in (('permits', self.permits), ('may_give', self.may_give)):
      if entries is not None:
        lines.append(f'{label}: {", ".join(entries) or "none"}')
    trail = self.trail_id if self.trail_id is not None else 'none (not published)'
    lines.append(f'trail_id: {trail}')
    return '\n'.join(lines)


def render_banner(
  plain: bool = False,
  bro: Optional[str] = None,
  trail_id: Optional[str] = None,
  resolved_recipe: ResolvedRecipe | None = None,
) -> str:
  """Render session facts, with in-process facts overriding the environment."""
  facts = SessionFacts.collect(
    bro_override=bro, trail_id_override=trail_id, resolved_recipe=resolved_recipe
  )
  return facts.render_plain() if plain else facts.render_visual()


def banner(plain: bool) -> int:
  """Print the banner, with --plain selecting key: value lines."""
  print(render_banner(plain))
  return 0
