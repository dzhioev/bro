# bro

> [!WARNING]
> **Early development.**
> Interfaces move without deprecation cycles and `master` carries breaking changes.
> Provided as-is, without warranty or support;
> use it at your own risk.

**Declare an agent once.
Run it on any harness[^harness] and any LLM[^llm].
Know exactly what it can reach.**

A bro is an agent persona declared in one place
— what it knows, which tools it holds, which secrets it may read, whom it may delegate to
— with nothing about it wired anywhere else.
The same declaration runs as a Claude Code session or under bro's own agent loop, one-shot or interactive, on whichever model you name at launch.
Wherever it runs, it runs inside a boundary that holds exactly what the declaration says:
a fresh container, a clone, the credentials it needs and no other, the bros it may summon and no other.
The rest of this page shows all of that on one made-up bro.

## Getting started

The way in is [`BOOTSTRAP.md`](BOOTSTRAP.md), an executable checklist for an agent rather than a page for you.
On a macOS or Ubuntu host with Git, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), Docker, and Claude Code, open an agent session in the repository you want to adopt bro in and point it at the file;
any harness that can read and edit the repository will do.
With Claude Code:

```console
cd ~/acme
claude 'fetch https://raw.githubusercontent.com/dzhioev/bro/master/BOOTSTRAP.md and follow its instructions'
```

The session pins the framework into the project from this repository at one commit, declares a first developer bro, wires the host's credentials, and commits the setup;
then it proves the setup with one isolated run and hands you a `dive-in` to continue from, showing every write before it makes it.

To work on the framework itself, clone this repository and run `./setup.sh`.

## Meet triage, an example

Everything from here on is one invented example:
`acme`, a project that does not exist, and `triage`, the bro it ships to work its issue queue.
`triage` reads a report against the code, labels the issue or closes it as a duplicate, files a task for a confirmed bug, and hands the bug on.
It holds no shell and patches nothing.
Here is all of it:

```python
# bros/triage/__init__.py
import bro.brog.mcp as brog_mcp
from acme import issues
from bro.base.condition import when
from bro.bro import feature
from bro.datasources.references import man
from bro.datasources.web_search import WebSearch
from bro.harness import claude
from bro.llm.llms import openai
from bro.mcp import creds, harness, mount, sh
from bros.bro import Bro
from bros.dev import mcp as dev_mcp

SYSTEM_PROMPT = """\
You triage the issues of this repository: read a report against the code,
label it or close it as a duplicate, and hand a confirmed bug on. You hold
no shell and patch nothing. Read issues through the `issues::` tools; search
the web when a report names a library you do not know.
{{when #features contains brog}}
A confirmed bug gets a task (`brog::create_task`) before it is handed on.
{{end}}{{when #may_summon contains analyst}}
Before you label a report, [[ask analyst whether a recorded run already worked on it]],
and read the run it names.
{{end}}{{when #may_summon contains fixer}}
Hand a confirmed bug to `fixer` with [[ask]]: one self-contained prompt with the
issue number, what you read, and the task.
{{end}}"""


class Triage(Bro):
  name = 'triage'
  description = 'reads and labels incoming issues, hands confirmed bugs on'
  llm_spec = openai.LLMSpec(model='gpt-5.6-sol', reasoning_effort='high')
  features = {'brog': creds.contains('brog')}
  may_summon = ('analyst',)
  tools = [
    mount(issues.toolset),
    when(feature('brog'), mount(brog_mcp.toolset, 'create_task', 'add_comment')),
    when(harness == 'bro', mount(dev_mcp.toolset, 'read_file', 'grep')),
    claude.block(*claude.SHELL, *claude.DELEGATION),
    sh('bro show', 'name'),
    sh('rewind show', 'trail_id', 'output_limit'),
  ]
  data_sources = [WebSearch(), man('environment')]
  system_prompt = SYSTEM_PROMPT
```

## Run it anywhere

`dive-in` is the everyday launcher:
run from inside a checkout, it starts an isolated session against that checkout with the project's default bro, and the launch flags pass through to `ride` underneath.

Triage tonight's queue as a Claude Code session in a container:

```console
dive-in 'triage the queue'
```

The same bro under bro's own loop, on an OpenAI model at high effort:

```console
dive-in --harness bro --llm openai:sol:high 'triage the queue'
```

Back on Claude Code, pinned to one model at maximum effort:

```console
dive-in --llm :fable5:max 'triage the queue'
```

Bare Claude, holding only the declared tools and none of Claude's own:

```console
dive-in --raw 'triage the queue'
```

Another bro of the project, on a host worktree with permission prompts kept:

```console
dive-in --bro reviewer --host 'review PR #131'
```

One reply on stdout, for a script:

```console
ask --repo ~/acme triage 'is #128 a duplicate of #97?'
```

And no boundary at all, in the calling process, when you just want to poke at it:

```console
bro run triage 'is #128 a duplicate of #97?'
```

## Hand the bug on

On its own authority `triage` may consult the analyst and nobody else;
the helpers that change code are granted at launch.
Lend it both for this session:

```console
dive-in --grant @fixer --grant @reviewer 'triage the queue'
```

The prompt's delegation paragraph is in now, and a confirmed bug is handed on in plain words, by the session or by you:

```
[[ask fixer to fix #128; the task has what I read]]
```

The fixer runs in a fresh container of its own, on the same commit, with its own scoped credentials and nothing inherited, and its answer comes back into the conversation.
When it reports a pull request:

```
[[ask reviewer to review PR #131]]
```

The phrase is the same under bro's own loop and in a Claude session, and a bro's own prompt uses it the way you do.

## What the declaration says

Read the class line by line and nothing is left to configure elsewhere:

- `mount(issues.toolset)` mounts the project's own toolset, and `mount(brog_mcp.toolset, 'create_task', 'add_comment')` two tools of another, the subset validated at import.
- `when(feature('brog'), …)` makes the tracker appear wherever a tracker credential resolves, and the prompt's `{{when #features contains brog}}` paragraph appears with it.
- `when(harness == 'bro', …)` gives the bro file reading only where the harness brings no file tools of its own.
- `claude.block(*claude.SHELL, *claude.DELEGATION)` withholds Claude's shell and subagents:
  triage holds no shell, and delegation goes through summons, inside the boundary.
- `sh('bro show', 'name')` and `sh('rewind show', 'trail_id', 'output_limit')` are the two commands it may run, each served as a tool:
  the card of a bro it is about to hand work to, and the run the analyst names.
  How a command becomes a tool is below.
- `data_sources` are read-only connectors whose summaries land in the prompt: web search and the reference manual.
- `llm_spec` is the model it runs on by default;
  any launch may name another.
- `may_summon = ('analyst',)` is whom it may summon on its own authority:
  the built-in analyst, which answers from the recorded runs.
  Every other delegate is a launch grant, and a `{{when #may_summon contains …}}` paragraph shows only in a session that may.
- `bros/triage/spells/triage.md`, beside the class, is its procedure, served as the `spell::triage` tool.

**A command is a tool.**
`sh('rewind show', 'trail_id', 'output_limit')` reads the arguments `rewind show` declares
— from its parser, not its help text
— and serves it as `sh::rewind_show` with the two named parameters, the rest withheld.
This is what the model sees:

```json
{
  "trail_id": {"type": "string", "description": "trail id (or a legacy claude session id)"},
  "output_limit": {"type": "integer", "description": "max rendered output lines (default with an offset: 100)"}
}
```

A call `sh::rewind_show(trail_id='01m1z954qq-q9frvz3q-scmg8x5h', output_limit=200)` runs one fixed argv, with no shell in between:

```console
rewind show --output-limit=200 -- 01m1z954qq-q9frvz3q-scmg8x5h
```

The program and its subcommands come from the declaration, never from the model, and a value that looks like an option still reaches the command as a value.
Any console script built on the framework's argument parser qualifies, a project's own included.

For a command outside the framework there is no parser to read, so a toolset tool runs the fixed argv itself, as `close_duplicate` above does.
These are the parameters of `issues::close_duplicate` as the model sees them:

```json
{
  "number": {"title": "Number", "type": "integer"},
  "duplicate_of": {"title": "Duplicate Of", "type": "integer"}
}
```

A call `issues::close_duplicate(number=128, duplicate_of=97)` runs:

```console
gh issue close 128 --comment 'duplicate of #97'
```

Inside the session `gh` is authenticated by the `github` credential's install hook, so the tool carries no token of its own.

**Credentials are never listed.**
The toolset says it reads `github`, `WebSearch` says `brave`, the tracker says `brog`;
the bro's manifest is the union, and `bro show` prints it beside the tools:

```console
$ bro show triage
…
## MCP tools

- `issues` — 3 tools
  - `list_issues` — list open issues carrying a label, newest first
  - `label_issue` — add a label to an issue
  - `close_duplicate` — close an issue as a duplicate of another
- `brog` — 2 tools
  …
- `dev` — 2 tools
  …
- `sh` — 2 tools
  - `bro_show` — print an info card for a bro
  - `rewind_show` — render one generalized conversation (the default command)

## Features

- **brog** — gated on `#creds contains brog`; on in this environment

## Secrets

- `brave`
- `brog`
- `github`
- `openai` — optional (used if present)
- `openai` — LLM key
- _session baselines (`trails`; `anthropic` for `--raw`) added per-surface_
…
```

The toolset is an ordinary module, one `Toolset` per namespace:

```python
# acme/issues.py
import subprocess

from bro.mcp import Toolset


class _Toolset(Toolset[None]):
  secrets = ('github',)


toolset = _Toolset('issues')


@toolset.tool('list open issues carrying a label, newest first')
def list_issues(label: str, limit: int = 20) -> str:
  ...


@toolset.tool('add a label to an issue')
def label_issue(number: int, label: str) -> str:
  ...


@toolset.tool('close an issue as a duplicate of another')
def close_duplicate(number: int, duplicate_of: int) -> str:
  argv = ['gh', 'issue', 'close', str(number), '--comment', f'duplicate of #{duplicate_of}']
  return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
```

Every tool has one canonical name, `namespace::tool`
— `issues::list_issues`, `brog::create_task`, `spell::triage`
— and that is the name prompts and spells use;
each harness spells its own wire form (`issues__list_issues`, `mcp__issues__list_issues`), and the model is told the rule once.

The two helpers derive from built-in personas and declare only what they add;
the whole of `dev` and `eyebro`
— tools, spells, prompt, provisioning
— comes along the class hierarchy:

```python
# bros/fixer/__init__.py
from bro.llm.llms import openai
from bros.dev import Dev


class Fixer(Dev):
  name = 'fixer'
  description = 'fixes a confirmed bug and opens the pull request'
  llm_spec = openai.LLMSpec(model='gpt-5.6-sol', reasoning_effort='high')
  features = {'brog': True}
  extra_secrets = ('github',)
  system_prompt = 'Work from the task you were handed, and open the pull request with [[run pr]].'


# bros/reviewer/__init__.py
from bros.eyebro import Eyebro


class Reviewer(Eyebro):
  name = 'reviewer'
  description = 'reviews a pull request against the repository standards'
  extra_secrets = ('github',)
```

Registration is three entry points and a default:

```toml
# pyproject.toml
[project.entry-points.bro]
triage = "bros.triage:Triage"
fixer = "bros.fixer:Fixer"
reviewer = "bros.reviewer:Reviewer"

[tool.bro]
default = "triage"
```

The persona is the same on every harness:
its prompt, its spells, its credentials, its allow-list.
What differs is what the harness brings of its own, and the declaration decides what to do with it.
A Claude session comes with Claude's tools and skills, so the declaration withholds the ones it does not want and adds its namespaces as MCP servers beside the rest;
bro's own loop and a bare session serve exactly the declared roster.
The model is a launch decision as much as a declaration:
`--llm provider:model:effort[+fast]`, any part left empty to keep the declared one, or a preset the project names.

## A boundary you can read

Before a session exists, its reach is on paper.
The card above lists the tools and the secrets;
the scope names which stored instance backs each secret for this checkout and this bro, and whether it resolves:

```console
ride scope --repo ~/acme --bro triage
```

Which instance that is stays out of the repository, in the host's `~/.bro.json`:

```json
{
  "defaults": {"creds": ["github+me"]},
  "projects": {
    "https://github.com/acme/acme": {
      "creds": ["brog+github", "github+bot"],
      "bros": {"reviewer": {"creds": ["github+reviewer"]}}
    }
  }
}
```

Inside the container, the session finds:

- `/workspace`, a clone on a fresh branch based on the commit the launch names, never your working tree;
- the framework it was launched from, frozen and read-only, so the project need not install it;
- a credential store holding exactly the declared kinds, resolved on the host into memory and copied into the container's writable layer.
  A missing required kind fails on the host before the container exists;
  an undeclared kind resolves to `SecretNotFound`;
  the store dies with the container;
- its own `~/.claude` and git configuration, constructed by the launch.
  No host `~/.claude.json`, credentials file, `~/.gitconfig`, or Docker socket.

Lend it one more credential for one launch:

```console
dive-in --grant aws 'why did the deploy fail?'
```

Delegation is bounded the same way.
A child may summon only whom its parent could and hold only what its parent lends it;
the host authorizes every request and journals it, and the child's trail records who summoned it.

Afterwards, every run on every harness is a recorded trail:

```console
rewind show <trail-id>
```

Two boundaries are not drawn:
the container's network is unrestricted, and `--host` runs as the host user, with scoped credentials as a convenience rather than a security boundary.

## Also in the box

- **Spells.**
  A procedure is a markdown file beside the persona, mounted as a tool and callable by phrase:
  `[[triage #128]]` in any prompt names `spell::triage`, and its body renders per harness.
  Claude's own skills keep working next to them.
- **Holds.**
  A session is told how firmly a human holds it, from `unattended` to `guided`, and behaves accordingly;
  an unattended run can `raise` instead of guessing.
- **Trails.**
  `rewind` reads any run back, a conversation forks and continues, and `ride resume` picks a workspace up where it stopped.
- **Tasks.**
  `dive-in -t 77` opens a session on a tracker task, and the built-in `dev` persona carries the task → PR → land workflow as spells.
- **Personas.**
  `dev`, `eyebro`, `lead`, `terminal`, `analyst`, and `devoops` ship ready to derive from.
- **Artifacts.**
  Peers pass files by content-addressed reference, and reach follows the launch tree.
- **One conditioning model.**
  `when(harness == 'bro', …)` in code and `{{when #harness = bro}}` in text evaluate the same facts and fail fast on a typo.

## Status

Two harnesses and one native provider today, footnoted above;
installs pin a commit of this repository, with a package index still to come;
the native loop's third-party skill loader exists with nothing loaded yet.

## Read more

- [`DESIGN.md`](DESIGN.md) — the conceptual model
- [`AGENTS.md`](AGENTS.md) — the framework map, and how to add a bro, a data source, a toolset
- [`bro/reference/ride.md`](bro/reference/ride.md) — the runtime: workspaces, credentials, summons, recording
- [`bro/reference/conditions.md`](bro/reference/conditions.md) and [`bro/reference/template.md`](bro/reference/template.md) — conditioning in code and in text
- [`bro/setup/AGENTS.md`](bro/setup/AGENTS.md) — the credential store and the host config

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

[^harness]: Today a bro runs under Claude Code or under bro's own loop.
The harness seam is one class, and more harnesses are coming.

[^llm]: Today bro's own loop runs OpenAI models, and a Claude Code session runs Anthropic's.
A provider is a recipe module plus a client, and more are coming.
