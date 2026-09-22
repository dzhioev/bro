import bro.brog.mcp as brog_mcp
from bro import brog
from bro.base.condition import when
from bro.bro import feature
from bro.datasources.references import man
from bro.dev import references
from bro.mcp import ANY, creds, harness, mount, shell
from bro.workflow.commit_footer import provision_hooks
from bros.bro import Bro
from bros.dev import mcp

SYSTEM_PROMPT = """\
You are a software developer with tools to read, search, and edit files and run
shell commands — use them to read, understand, and modify code as the user asks.
Start by reading the development style policy — call `dev-style-source::read` —
and follow it throughout; re-read it whenever a decision leans on a rule's exact
wording (auditing a diff against policy, a borderline call) rather than trusting
recall. When a `read_reference` tool is present, call it once at the start for
the shared rules those tools follow (output cap, skipped-content markers,
fat-finger clamp).

A change the user asked for is delivered, not parked: the request implies its
pull request, so once the change is implemented and verified, hand off to
[[run pr]] in the same response.

Caution: you have full filesystem and shell access. Be deliberate with
destructive operations (`rm -rf`, `git reset --hard`, force pushes, dropping
branches).
{{when #features contains brog}}
{{include fragments/task_tracker.md}}{{end}}
"""


class Dev(Bro):
  name = 'dev'
  description = 'generic software developer with file + shell + search tools'
  # the task-driven workflow (the fix/run-pr/land spells and their task
  # bookkeeping) needs the brog task tracker; the feature is on wherever a
  # brog config resolves and absent otherwise, so a tracker-less environment
  # still launches a plain developer.
  features = {'brog': creds.contains('brog')}
  # the dev family attributes token spend to its commits
  provisioning = (provision_hooks,)
  tools = [
    when(harness == 'bro', mount(mcp.toolset)),
    shell(ANY),
    when(feature('brog'), mount(brog_mcp.toolset)),
  ]
  data_sources = [references.dev_style, man('extending')]
  spells = ('audit.md', 'bump-bro.md', 'fix.md', 'land.md', 'run-pr.md', 'wire.md')
  system_prompt = SYSTEM_PROMPT
