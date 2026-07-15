from base.condition import when

# a self-reference (resolved through sys.modules mid-initialization) plus the
# submodule import that binds `mcp` on the package — so the declaration below
# spells its entry by qualified pack path, readable without this header
from bro.bros import dev
from bro.bros.bro import Bro
from bro.bros.dev import mcp
from bro.datasources import references
from llm.mcp import harness

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
"""


class Dev(Bro):
  name = 'dev'
  description = 'generic software developer with file + shell + search tools'
  # the dev toolset duplicates the claude harness's built-in file/shell tools
  mcp_servers = [when(harness == 'bro', dev.mcp)]
  data_sources = [references.dev_style]
  system_prompt = SYSTEM_PROMPT
