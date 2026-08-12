import bro.brog.mcp as brog_mcp
from bro import brog
from bro.base.condition import when
from bro.bro import feature

# a self-reference (resolved through sys.modules mid-initialization) plus the
# submodule import that binds `mcp` on the package — so the declaration below
# spells its entry by qualified pack path, readable without this header
from bro.bros import dev
from bro.bros.bro import Bro
from bro.bros.dev import mcp
from bro.datasources import references
from bro.llm.mcp import creds, harness

SYSTEM_PROMPT = """\
You are a software developer with tools to read, search, and edit files and run
shell commands — use them to read, understand, and modify code as the user asks.
Start by reading the development style policy — call `dev-style-source::read` —
and follow it throughout; re-read it whenever a decision leans on a rule's exact
wording (auditing a diff against policy, a borderline call) rather than trusting
recall. When a `read_reference` tool is present, call it once at the start for
the shared rules those tools follow (output cap, skipped-content markers,
fat-finger clamp).

Caution:
- You have full filesystem and shell access. Be deliberate with destructive
  operations (`rm -rf`, `git reset --hard`, force pushes, dropping branches).
- For state shared beyond the local machine (pushing code, opening PRs, sending
  messages, deploying), confirm before acting unless the user already
  authorized it.
{{when #features contains brog}}
{{include fragments/task_tracker.md}}{{end}}
"""


class Dev(Bro):
  name = 'dev'
  description = 'generic software developer with file + shell + search tools'
  # brog: the task-driven workflow (the fix/run-pr/land scripts and their task
  # bookkeeping) needs the brog task tracker; the feature is on wherever a
  # brog config resolves and absent otherwise, so a tracker-less environment
  # still launches a plain developer.
  # commit-accounting: the dev family attributes token spend to its commits —
  # session-start provisioning installs the footer hooks into the managed
  # workspace.
  features = {'brog': creds.contains('brog'), 'commit-accounting': True}
  # the dev toolset duplicates the claude harness's built-in file/shell tools
  tools = [when(harness == 'bro', dev.mcp.spec()), when(feature('brog'), brog_mcp.spec())]
  data_sources = [references.dev_style]
  system_prompt = SYSTEM_PROMPT
