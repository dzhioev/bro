# webview: a browser worker a session drives through the ride channel

A design-review copy of the `## Design` of [#638](https://github.com/dzhioev/bro/issues/638), reviewed as a document, never merged;
the task page is the single source of truth, and this file is written back to it once the review approves.

## Design

Settled with the user on 2026-09-21 (trail `01m30k8f2f-xmn2pkav-9ztpe826`) over #691's shipped contract, then reviewed against the code the same day (trail `01m30r7vb7-ksh5skqa-hx3r5phy`);
what the review changed and why is in `## Design changelog`.
Supersedes `## Shape`.
At write-back the task's other sections are brought to this vocabulary in place, so the page contradicts itself nowhere:
`## Shape`'s `{text}` payloads and `quest ask` become JSON objects and `mission ask`, `## Follow-ups`' `quest_ask` becomes `mission ask`,
and `## Goal`'s "other bros never touch it" becomes the policy `### The type` states, enforced by #522.

### Vocabulary

A *webview* is a mission of the `webview` worker type:
opened by `launch {type: webview, …}` from the session that becomes its *owner*, undertaken by a worker container running Chromium and the `webview serve` daemon,
and ended by a close command, `mission cancel`, the owner's death, or root teardown.
A *command* is an owner question on the mission's chat carrying one JSON object;
a *reply* is the worker's reply to it, and a *say* is the worker's unsolicited event.
`vnc` is the one mode that puts a human at the screen.
The browser's vocabulary is Playwright MCP's, minus the tools the daemon withholds.

### The mission surface: `bro/mission.py`, with `quest` the bro-only view over it

The quest verbs were written for bro quests:
`quest say` and `quest ask` refuse a mission of another type (their liveness check reads the bro answer), `quest history` renders `{text}` payloads alone, and `quest watch` prints chat for bro quests only.
The webview needs a surface that is per mission rather than per type, with typed payloads.

`mission`, a new session command in core (`bro/mission.py`, registered beside `quest`), is that surface, one rule for every worker type:

- `mission check <id>` prints the mission's retained result, outcome and value or error, or that it still runs;
  a bro's answer is its value.
- `mission history <id> [--seq N]` prints the talk rights and the retained chat tail with open questions marked;
  `--seq` prints the one entry with its whole payload, read through the events read (the ride keeps 4096 events), so it reaches past the record's 32-entry tail.
- `mission say <id> '<json>'` and `mission ask <id> '<json>' [--reply-to ID] [--wait [SECONDS]]` take one positional, a JSON object that is the payload itself;
  a payload over `MAX_MESSAGE_BYTES` or a move the mission's talk forbids is refused before sending.
- `mission list [--type T]` prints every retained mission the session owns, live first.
- `mission watch [--type T]` streams every owned mission's lifecycle and chat, one line each:
  `mission <id> (<type>[ to <target>]) <transition>` for a lifecycle transition and `… says|asks|replies|refused <payload>` for chat, a `{text}` payload rendered as text and any other as compact JSON;
  a chat line over `WATCH_LINE_BYTES` (1 KiB) is cut and ends with `[<n> bytes, cut; seq <seq> in mission history <id>]`.
- `mission cancel <id> [--timeout S]` ends an owned mission.

`quest` becomes the bro-only view over the same module, on every verb:
`check`, `list`, `history`, `say`, `ask`, `watch`, and `cancel` take bro quests alone, with `{text}` payloads taken and printed as text, `check` printing the answer, and `watch` printing bro quests' lifecycle and chat and nothing else;
the `quest_*` service tools, `bro::quest_cancel` included, narrow the same way.
A flow that never launches another type never meets the word mission:
the verbs' exit codes, the `quest_*` tools, and the public functions importers use keep their names and shapes, so prompts, spells, holds, and docs stay as they are;
the functions are `check`, `history`, `say`, `ask`, `watch`, `cancel`, `live_missions`, and the constants.
A bro that launches another type is told about `mission` and declares it:
`cli('mission ask')` and `cli('mission history')` mount the parameterized verbs as tools with typed arguments derived from the CLI's declarations,
since a shell roster admits complete command lines by exact match and cannot carry a mission id or a payload,
and `shell('mission watch')` (or `shell('mission watch --type webview')`) admits the fixed watch command behind the shell block on both harnesses.
There are no `mission_*` service tools, and the automatic `quest watch` admission is unchanged.
Two watches run side by side:
each is an ordinary channel client with its own cursor over the non-destructive `events` read, multiplexed by the session's broxy.

The turn-end guards are the one shared place, and they speak both vocabularies without matching either:
a one-shot session's turn end is held while the session owns a live mission of any type (`live_missions()`, as #691 made it) and no background task runs,
since print mode holds the process on any pending task and the guard runs again at the next stop, which is the rule the native runner already applies to its jobs.
The Claude stop guard (`ride/ride/claude/stop_guard.py`) drops its exact `quest watch` match and its idle-watch notice for that one rule;
its notice lists the missions as `live_mission_line` words them (`quest <id> to <bro>`, `mission <id>: <type>`)
and names the way out per kind present, `quest watch` and `quest cancel` for quests, `mission watch` and `mission cancel` when any mission is not one.
The native runner's notice (`native/bro/native/runner.py`) gains the same per-kind wording.

### The type: `bro/webview/worker.py`

`WebviewType`, registered as `webview = bro.webview.worker:WebviewType` under `bro.worker_types` by the `bro-webview` distribution.
Declarations:
`name = 'webview'`; `permits = frozenset({'vnc'})`, spelled `:webview.vnc` on every surface; `default_timeout = None`; `widens_talk = False`; `manual = False`.
`talk(request)` is `{owner.question, worker.say}` for every launch.

`launch(request)` accepts `vnc` (bool, default false) and `allowed_origins` and `blocked_origins` (lists of non-empty strings without `;`, default empty), and denies any other field;
denies `vnc` when the owner's permits lack `webview.vnc` (`launch denied: the VNC view needs :webview.vnc; permits held: …`);
denies an owner that is itself a webview, since a webview is a leaf;
and denies a second live webview for one owner (`launch denied: this session already holds a live webview <id>; close or cancel it first`).
A journal subscriber the type returns from `subscribers()` keeps the live webviews by owner mission, adding on a webview record's `accepted` and dropping on its `ended`;
the dispatcher journals a launch's `accepted` inside the primitive the control calls, so the next request from the same owner sees it.
The run is:

```python
Container(
  WorkerContainer(
    files={'Dockerfile': <packaged>},
    command=('webview', 'serve'),
    env={'WEBVIEW_OPTIONS': <JSON of vnc and the origin lists>},
    published_ports=(6080,) if vnc else (),
    artifact_view='/workspace/artifacts',
  ),
  extension=WebviewFacts(vnc, allowed_origins, blocked_origins),
)
```

The packaged file is read through `importlib.resources` at launch, never at import, and the module imports only `bro.worker_types` and the standard library at module level, since the permit preflight loads it.
`audit_fields` renders the facts as `{vnc, allowed_origins, blocked_origins}`.
Any attributable owner may open a webview, and the type interprets no other permit.
`## Goal`'s rule that other bros reach the web only through the browser bro is policy here and stays policy for a root session, which holds every installed type under #522 as it holds every permit today;
for a summoned child #522 makes it enforced, since a child then opens a webview only with the bare `:webview`,
which it holds through its own bro's configured layers (a project's `[tool.bro.browser]` grant, or the host config) or a summoner's grant bounded by its own, and which other bros' layers do not grant.
A generic type names no persona, so nothing of this is enforced here.
The launch's `share` refs are linked into the worker's artifact view by the generic lowering;
there is no manual variant.

### The artifact view of a worker container: `WorkerContainer.artifact_view`

`WorkerContainer` gains `artifact_view`, the absolute container path where the host mounts the worker's read-only artifact view, default `/var/ride/artifacts` (`CONTAINER_ARTIFACTS_ROOT`), validated as a normalized absolute POSIX path;
a launch fact rather than an image fact, so it stays out of the image hash.
The lowering mounts the view there;
the facts row records the path where it recorded a bool, `PeerDescription.artifact_view` and `WorkerFacts.artifact_view` becoming an optional path, `None` for a peer with no view;
and `artifact get` answers `<view>/<ref>` for that peer.
Every other boxed peer keeps `/var/ride/artifacts`.
The webview declares `/workspace/artifacts`, inside the browser's file root, so a shared ref is uploadable without a copy.

### The image

One packaged file, `bro/webview/container/Dockerfile`:
`ARG RUNTIME_IMAGE` and `FROM ${RUNTIME_IMAGE}`
(the image every session container runs: `python:<version>-slim` with Node 24, gh, uv, Claude Code, and the `ride` user, whose build steps run as root and whose entrypoint re-executes the command as `ride`),
then `xvfb`, `x11vnc`, `novnc`, and `websockify` from apt, `@playwright/mcp` pinned by version through `npm install -g` (its bin is `playwright-mcp`),
and Chromium with its system libraries through that package's own `playwright` dependency (`playwright install --with-deps chromium`) under `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`, readable by every user.
The `@playwright/mcp` pin is the one version fact:
it fixes the tool roster and the Chromium build (the current release, 0.0.82, ships 25 tools over a Playwright alpha), and bumping it is editing the file, which changes the image hash.
The host builds `bro/webview:<hash>` lazily at the first open on a runtime image and prunes superseded tags, as #691 lowered;
the cold build downloads Chromium and takes minutes, inside the host's 30-minute launch deadline, and repeats at the first open after any runtime image change (a claude-code or uv bump, an edit to the runtime Dockerfile) or pin bump.
`ride/ride/worker_container.py` serializes `ensure_worker_image` per tag, since two first opens in one ride would otherwise build the same image twice,
and captures the build's output, so a failed build ends the launch with the tail in its error rather than "returned non-zero exit status 1".

### The daemon: `webview serve` (`bro/webview/serve.py`)

The container's command, run under `broxy run`.
In order, each step under a context manager that tears it down:
it decodes `WEBVIEW_OPTIONS`;
checks that its artifact view is mounted where the type declared it, a mount point at `/workspace/artifacts`, failing otherwise;
starts Xvfb on a fixed screen, waiting on `-displayfd` for the display;
in `vnc` mode starts x11vnc on the container's loopback and websockify with the noVNC pages on port 6080, waiting for each to accept a connection on its port,
and reads the published host port from `RIDE_PUBLISHED_PORTS` (a missing mapping fails the daemon);
starts Playwright MCP (`playwright-mcp`) as a stdio subprocess driven through the `mcp` SDK's client session,
with cwd `/workspace`, a minimal environment (`PATH`, `HOME`, `DISPLAY`, `PLAYWRIGHT_BROWSERS_PATH`), Chromium headed on the Xvfb display, `--isolated` (an in-memory profile),
`--no-sandbox` (Docker's default seccomp profile denies the user namespaces Chromium's sandbox needs; the container is the boundary),
the origin lists as `--allowed-origins` and `--blocked-origins`, `--output-dir /workspace/output`, `--image-responses omit`, `--file-paths absolute`, and the default capability set (`pdf`, `vision`, and `devtools` off),
and a `--config` file the daemon writes outside the workspace setting `browser.contextOptions.acceptDownloads` to false, so the browser refuses every download a page triggers;
the server's own file rule then confines uploads, drops, and named outputs to `/workspace` and refuses `file://` navigation;
warms the browser with `browser_navigate` to `about:blank`, so a Chromium that cannot start fails the launch rather than the first command;
opens `Client.from_env()`, emits `listening`, and says `{"event": "ready", "vnc": <url or null>}`.

Then it serves commands in arrival order from `receive`:
every owner question is decoded, run, and answered with `reply_to`, and a malformed one gets an error reply.
Each command is bounded by `COMMAND_DEADLINE` (120 s, above Playwright MCP's own 60 s navigation and 5 s action timeouts);
a command that outlives it ends the daemon non-zero, `failed:exit` with the log tail naming the command, because a wedged server cannot be trusted with the next one, and the owner opens a new webview.
The daemon owns every subprocess it started, Xvfb, x11vnc, websockify, and Playwright MCP, and waits on each in a thread of its own:
an exit at any time, idle or mid-command, ends the daemon the same way, the process name and status in the tail, with the receive loop unblocked by closing the channel client.
A tool's own error is the page's business and is relayed.
The daemon withholds the tools that leave the browser, `WITHHELD_TOOLS = {'browser_run_code_unsafe'}` (it runs arbitrary Node in the server process):
such a command gets an error reply without a call, and the roster omits them.
On `{"webview": "close"}` it replies `{"closed": true}`, sends `result {outcome: ok, value: {commands: <count>}}`, and exits 0, so the throwaway workspace is removed.

### The chat

Payloads are JSON objects in both directions, the mission chat's shape, bounded by `MAX_MESSAGE_BYTES` (16 KiB);
`mission ask` refuses an over-long command before sending.
A *command* is `{"tool": "<name>", "arguments": {…}}` with `arguments` optional, a Playwright MCP tool name passed through unchanged,
or one of the daemon's own, `{"webview": "tools"}` (the live roster with descriptions and input schemas, minus the withheld tools, as the reply's text) and `{"webview": "close"}`.
A *reply* is `{"text": "<the tool's text result verbatim, Playwright MCP's own markdown, snapshot included>", "files": [{"name": "<file>", "ref": "sha256:…"}, …]}`;
a tool error is `{"error": "<text>"}`, and a withheld or malformed command likewise.

The files are collected by diffing the workspace around the call:
before it the daemon records every file under `/workspace` (the `artifacts` mount excluded),
and after it every file new or changed is minted as an artifact (`bro.artifact.mint_artifact` from the daemon's workspace, through the shared attach), listed in `files`, and removed from the workspace.
So a file is attributed to the command that wrote it wherever the server put it (`--output-dir` takes the automatically named ones; a `filename` argument resolves against the workspace root), and the next command starts clean.
The collector never meets an open file:
every tool writes its files inside its handler before its result returns, and the one writer without a completion signal, a page-triggered download, which the server saves asynchronously after the call, is refused at the browser context;
the server routes the refused save into the rejection listener its live context installs, so the process survives,
and a file a page offers for download reaches the owner through `browser_network_request` with `part: response-body` and a `filename`, an owner command bounded like any other.
A mint the store's byte cap refuses is listed as `{"name": "<file>", "error": "<reason>"}` and the file is removed all the same;
a command whose files together exceed `OUTPUT_LIMIT_BYTES` (256 MiB) has none of them minted, all of them removed, and its reply is `{"error"}` naming the size.

The reply is measured whole against the bound.
Over it, the `text` is minted first and `{"spilled": "text", "ref": "sha256:…", "bytes": <n>, "files": […]}` stands in;
when that still does not fit (a long `files` list, the roster), the whole reply object is minted and `{"spilled": "reply", "ref": "sha256:…", "bytes": <n>}` is sent;
and a spill whose own mint fails (the store cap, an ingest error) is answered `{"error": "<reason>", "dropped": <bytes>}` with the reason cut at 1 KiB, an object that fits by construction, so no reply is ever refused by the dispatcher.
A mint reaches the owner and its ancestors and is linked into each one's view at once,
so a boxed owner reads a spilled snapshot or a screenshot at `/var/ride/artifacts/<ref>` as soon as the reply names it, and an unboxed owner through `artifact get`;
a snapshot of a real page usually crosses the bound, so that is the common step.
A ref the owner shared at open appears at `/workspace/artifacts/<ref>` inside the webview, the path to give `browser_file_upload`.
The daemon's says are JSON objects (`ready` is the only one today).
Every command and reply is journaled with its full payload and written to the launch audit, which grows by up to the bound per message.

### The owner side: the `webview` session command (`bro/webview/cli.py`)

Verbs `open`, `close`, and the container-side `serve`.
`webview open [--vnc] [--allow ORIGIN …] [--block ORIGIN …] [--share REF …] [--timeout SECONDS]` sends the launch,
rides its own connection through `accepted` and `started` to the `ready` say, then prints `{"mission": <id>, "vnc": <url or null>}`;
a denial or a failed launch (an image build error, say) exits non-zero with the reason,
and when its bound (`OPEN_TIMEOUT`, 1200 s, re-armed by each mark and below the host's 1800 s launch deadline) expires it cancels the mission it launched, so no webview outlives an open that gave up.
`webview close [MISSION]` sends `{"webview": "close"}`, waits for the reply and then for the record to end, and prints the outcome;
with one live webview per owner, `MISSION` defaults to the session's live webview, found in the caller's listing by type.
Everything between open and close is `mission ask <id> '<command>' --wait` (or `cli('mission ask')` from a bro that mounted it), and `mission history <id>` shows the conversation.
`mission watch` prints a webview's lifecycle and chat;
`quest` stays about bros on every verb.
A `ride exec` shell has no channel of its own, so driving a webview by hand happens from a session process, or on the human's side through `vnc`.

### Isolation and the human

A webview publishes no port by default;
its surfaces are the ride channel and the outbound network every container has.
Only its owner can command it:
the dispatcher delivers chat from a mission's two ends alone.
The container is the security boundary, Docker's, under the launch default `--memory=8g`, with Chromium unsandboxed inside it;
it gets the runtime volume read-only, an empty credential store, its artifact view, and no session, party, or trails state.
Inside it two measures are defense in depth against the owner itself, an LLM reading untrusted pages, and not a boundary, as Playwright's own contract says of its file rule:
the daemon withholds the tool that runs code in the server process, and the server's file rule keeps uploads, drops, and named outputs under `/workspace` and refuses `file://` navigation,
so a page that talks the owner into reading or uploading a file has no ordinary command that reaches the runtime volume or a process environment, which carries the channel's attach token.
What a compromise of the unsandboxed Chromium or of the server process reaches is the container itself:
the read-only runtime volume, the empty store, the owner's own refs, and that token, whose use needs presence on the launcher's docker bridge and yields impersonating the webview toward its owner.
The follow-up's profile file stays outside `/workspace` for the same reason, out of the ordinary commands' reach.
`vnc` is the one mode with a human at the screen:
noVNC on the launcher's loopback without a password, so while it is on any local process on the launcher can watch and drive the browser;
hence the permit, granted at a root launch (`--grant :webview.vnc`, or the host config) and passed down only by a summoner that holds it, never turned on by a bro alone.
The origin lists are advisory, as Playwright documents:
they do not affect redirects and are not a security boundary;
the boundary is what the run is lent and the human's answers up the channel.
The journal keeps every command and reply (the record's 32-message tail, full payloads) and the launch audit keeps them durably, so a run is reviewable action by action.

### Lifecycle and bounds

A webview lives until its owner closes it, `mission cancel` (`failed:cancelled`; the workspace is kept like any killed worker's until `ride clean` reclaims the empty tree),
the owner's death (`failed:orphaned` through the cascade), root teardown (`killed`), or a `timeout` the open request named (none by default).
A live one holds a one-shot session's turn end and shows in the status line, as #691 made every mission do, which is the nudge to close it.
A ride on a supplied runtime (`--runtime-bundle`) cannot open one, having no runtime volume, as with any boxed launch;
an unboxed ride can, given Docker.
A command sent before `ready` is dropped by the worker's broxy and stays pending in the journal;
`webview open` returns at `ready`, so an owner that waits for it never sends one.
The workspace has no quota, so what bounds its transient use is that every write there is an owner's tool call, closed before the call returns and removed after minting:
a page cannot land a file of its own choosing, since downloads are refused.

| bound | value | on overflow |
|---|---|---|
| command | `MAX_MESSAGE_BYTES` (16 KiB) | `mission ask` refuses it before sending |
| reply | `MAX_MESSAGE_BYTES` | the text is minted, then the whole reply; `{"spilled", "ref", "bytes"}` stands in, or a bounded `{"error"}` when the spill itself fails |
| one command's files | `OUTPUT_LIMIT_BYTES` (256 MiB) | nothing minted, all removed, an error reply naming the size |
| files kept | the store's `MAX_STORE_BYTES` | the refused mint is listed per file as an error |
| one command | `COMMAND_DEADLINE` (120 s) | the webview ends `failed:exit`, the tail naming the command |
| open | `OPEN_TIMEOUT` (1200 s, re-armed by marks); host `LAUNCH_TIMEOUT` (1800 s) | the CLI cancels its launch; the host ends it `timeout` |
| webview life | none; `timeout` at open | the mission ends `timeout` |
| live webviews per owner | 1 | the second open is denied naming the first |
| container memory | the docker launch default (`--memory=8g`) | Chromium dies inside; the next command errors |
| a `mission watch` chat line | `WATCH_LINE_BYTES` (1 KiB) | cut, with the marker naming its `seq` |

### Packaging, surfaces, and docs

Core:
`bro/mission.py` (`mission`, a session command registered like `quest`), `bro/quest.py` narrowed to the bro-only view over it,
`bro/worker_types.py` (`WorkerContainer.artifact_view`, `PeerDescription.artifact_view` as a path), and `native/bro/native/runner.py` (the per-kind notice).
Ride:
`ride/ride/claude/stop_guard.py` (the one rule), `ride/ride/worker_container.py` (the view mount path, the per-tag build lock, the captured build output),
`ride/ride/peer_facts.py` and `ride/ride/artifacts.py` (the view path on the facts row and in `materialize`), and the callers that pass it (`broker_root.py`, `bro_worker.py`).
A new workspace member `webview/` publishes `bro-webview` (package `bro.webview`, a portion of the `bro` namespace like `bro.bench`), depending on `bro` and `mcp`;
modules `worker.py`, `serve.py`, `cli.py` (`__cli_name__ = 'webview'`), `container/Dockerfile`, the generated `_entrypoints.py`, and `webview/AGENTS.md`;
entry points `bro.worker_types` (`webview`) and `bro.session_commands` (`webview`), scripts through `sync-scripts --project webview`.
The root `pyproject.toml` gains the member in `members`, pytest `pythonpath`, pyright `include` and `extraPaths`, and ruff `src`;
`local/bro/local/run_tests.py` gains its distribution entry and its test modules, and its `broker_e2e` stage runs the webview's e2e module beside ride's.
Docs:
root `AGENTS.md` (the members list, the worker-type registration sentence, `mission` beside `quest`),
`bro/reference/ride.md` (the worker-containers section names the webview as the shipped container type and the view mount path a container declares; the permit paragraphs gain `:webview.vnc`; the artifacts section names the declared view path),
`bro/setup/AGENTS.md` and `bro/prompts/environment.md` (the permit sentences),
and `webview/AGENTS.md` (the wire, the reply format, the file collection, the withheld tools, the pin and how to bump it).
`bro/broker/AGENTS.md` is untouched, since nothing here reaches the substrate.

### Rollout

No wire change:
no envelope, kind, or `PROTOCOL_REVISION` moves.
The framework's distributions are one repository at one revision:
a consumer installs `bro`, `bro-ride`, and `bro-webview` from one pin (`bump-bro` moves every member's pin at once), and the runtime bundle freezes that installation for the ride's whole life,
so no supported installation mixes framework revisions and no ride mixes bundles.
Skew still fails loudly rather than quietly:
an older `bro-ride` beside a newer `bro` and `bro-webview` mounts the view at `/var/ride/artifacts`, and the daemon's mount check ends the launch naming the missing `/workspace/artifacts`;
an older `bro` beside a newer `bro-ride` or `bro-webview` fails the first open on the field they read and it lacks.
Rides already running keep their bundle and are untouched;
the next ride from a launcher installed from this release carries the type, the `mission` command, and the view mount path.
Order for a launcher whose configuration will grant `:webview.vnc`:
install the three distributions from one revision first, then add the grant to `~/.bro.json` or a project's `[tool.bro]`,
because the permit preflight loads the named type, and a root from an older bundle denies the leaf as an unknown type until relaunched.
The image builds at the first open per runtime image and needs network for Chromium;
a consumer pinning the framework adds `bro-webview` when it wants webviews.

### Rejected

- An owner-side toolset or client library now:
  `mission ask` already carries a question to any live mission the session owns and returns the reply, and `mission history` recovers a lost one;
  a typed toolset can format the same payloads later.
  Settled with the user.
- Renaming `quest` to `mission` throughout:
  88 files and the bro vocabulary in every prompt and spell;
  the narrowed `quest` keeps the bro workflows as they are and gives non-bro missions the universal surface.
  Settled with the user.
- A `quest` that keeps every mission's lifecycle lines and its universal `cancel`:
  a bro-only flow would meet missions on its watch, and the guards would still need to know which watch covers what;
  the one-rule guard covers the mixed set instead.
  Settled with the user.
- A stop guard matching the watch command and covering missions per kind:
  print mode holds the process on any pending task and the guard runs again at the next stop, so "missions in flight and no background task" is the whole rule, and the idle-watch notice goes with it.
  Settled with the user.
- Enforcing browser-bro-only ownership in the type:
  a generic component naming a persona;
  #522 enforces it for summoned children through the permit their bro's layers grant, and a root session's use stays policy.
  Settled with the user.
- `mission_*` service tools beside the `quest_*` ones:
  six more tools in every bro's roster for a surface few bros drive;
  a shell or CLI declaration serves those that do.
- Chat lines on `mission watch` printed whole:
  a webview reply is up to 16 KiB in an owner's Monitor feed;
  the bounded line points at the entry.
- JSON commands inside `{text}` payloads:
  they needed no client, but every reader would parse text twice;
  the wire carries any JSON object and `mission` renders it.
- Image content blocks beside output files:
  Playwright MCP writes every screenshot into the workspace anyway, so files are the one carrier.
  Settled with the user.
- Withholding or rewriting `filename` arguments so every output lands in `--output-dir`:
  a vocabulary change for what a workspace diff collects exactly.
- Accepting downloads:
  the one writer without a completion signal, saved by the server after the call returns, with a size the page chooses on an ordinary click and no quota on the workspace;
  refused at the context, with the response-body route for a file the owner wants.
- Declaring `mission` for a bro with `shell('mission ask', …)`:
  a shell roster admits complete command lines by exact match, so the parameterized verbs go through `cli(…)` mounts and only the fixed watch fits the roster.
- `--allow-unrestricted-file-access`, alone or with a daemon-side path rule:
  uploads and `file://` would then reach the process environments and their channel tokens through an owner a page talked into it.
  Settled with the user.
- Copying shared refs into the workspace, or a loopback HTTP server rendering them:
  the view mount path makes the copy unnecessary, and rendering a local file is out of the vocabulary (a small page goes as a `data:` URL).
  Settled with the user.
- Any base but the runtime image, the official Playwright image among them:
  the runtime volume's venv is built for the runtime image's interpreter, and the daemon lives in that venv.
- Headless Chromium with a headed one only under `vnc`:
  two browsers, and sites fingerprint headless.
- Inline base64 images or whole snapshots in replies:
  the 16 KiB message bound.
- A direct MCP or CDP link from the owner to the container:
  a channel beside the broker, unaudited (#541).
- A Node daemon over Playwright MCP's library:
  the broker and artifact clients are Python in the runtime venv, and a Node daemon would re-implement the wire.
- The Python `playwright` package instead of Playwright MCP:
  the snapshot-with-refs vocabulary is Playwright MCP's, reused as-is and versioned by the pin (#541).
- Publishing the VNC port always, or a launch flag instead of a permit:
  isolation by default, and a permit is what only the human's launch confers and a holder passes down.
- An `open` permit leaf now:
  #522 owns "may launch a type" as the bare `:webview`.
- Unbounded webviews per owner:
  tabs cover parallelism, a runaway loop must not fan out containers, and one live webview lets `webview close` name none.
- A default deadline:
  the browser lives as long as its owner wants;
  the owner's death, cancel, and teardown bound it, and a live one holds a one-shot session's turn end.
- Prebuilding the image at ride start:
  a seam the contract lacks, and the 30-minute launch deadline covers a cold build.
- Ending the daemon on a tool error, or keeping it alive past a stuck command:
  a tool error is the page's business and is relayed;
  a stuck server is a violated assumption.
- Detecting a dead Playwright MCP only at the next command:
  the daemon owns the process and can wait on it, and a dead VNC stack would otherwise stay reported ready.
- Draining pending questions from the journal at `listening`:
  a command before `ready` takes an owner that ignored `webview open`;
  not worth a second inbox path.
- Writing the profile back at close (for the follow-up):
  credentials flow down only, concurrent runs on one profile would race, and a bro-driven browser would write a stored credential.

### Verification the stages owe

- `bro/mission_test.py`:
  every verb over a fake journal read, `check` on a bro and on a non-bro result, `say` and `ask` with JSON payloads and the talk refusal,
  `history --seq` through the events read, the watch lines for both types with the cut line and its marker, `list --type`.
  `bro/quest_test.py`:
  every verb refusing or omitting a non-bro mission (`check`, `history`, `say`, `ask`, `cancel`, absent from `list` and from `watch`), beside everything it keeps.
  `ride/ride/claude/stop_guard_test.py` and `native/bro/native/runner_test.py`:
  the one rule, a live mission of either kind held with no task running and let stand under any running task, and the notice's per-kind wording.
- `bro/worker_types_test.py`:
  `artifact_view` validation and its default.
  `ride/ride/worker_container_test.py`:
  the mount at the declared path, the per-tag build lock, a failed build's tail in the error.
  `ride/ride/peer_facts_test.py` and `ride/ride/artifacts_test.py`:
  the view path on the facts row and in `get`.
- `webview/bro/webview/worker_test.py`:
  argument validation and each denial, the `vnc` permit, the leaf rule, the options env and the ports only with `vnc`, the view path, the live-webview rule through journal events, `audit_fields`,
  the packaged Dockerfile validating as a `WorkerContainer` with a stable hash, and the module importing no broker or MCP machinery at module level.
- `serve_test.py`, over an in-process broker and a fake stdio MCP server (a small `mcp` server script):
  the mount check, `listening` then `ready` after the warm-up, a command's reply verbatim,
  files collected by the diff wherever the fake wrote them, minted, listed, and removed, a reply over the bound spilling its text and then the whole reply, a spill whose mint is refused answered as a bounded error, the per-command file limit,
  the config file refusing downloads,
  `tools` and `close` with the ok result, a malformed command's and a withheld tool's error reply, the server's cwd and minimal environment,
  a server dying while idle and mid-command and a stuck command each ending the daemon non-zero naming the cause;
  the display and VNC processes behind fake `Xvfb`, `x11vnc`, and `websockify` executables on PATH, readiness probed and a dead one ending the daemon.
- `cli_test.py`:
  `open` riding to `ready`, failing on a denial, and cancelling on its own timeout;
  `close` waiting for the end and defaulting to the live webview.
- `webview/bro/webview/e2e_test.py` (`broker_e2e`, host-only, over `ride.e2e_test`'s fixtures), a real webview from `run_root_via_broker` with the installed runtime:
  open, `browser_navigate` to a `data:` URL, `browser_snapshot`, `browser_take_screenshot` with and without a `filename`, each yielding a file artifact the owner reads from its view and gone from the workspace,
  `browser_file_upload` of a shared ref from `/workspace/artifacts`, `browser_run_code_unsafe` refused by the daemon, `file://` navigation refused by the server,
  a page-triggered download refused with nothing written and the next command answered, a response body saved through `browser_network_request` and minted, close ending `ok`, the container and workspace gone;
  a `--vnc` open answering on the published loopback port and denied without the permit;
  cleanup asserted against the live runtime root rather than the fixture's, so the scenario does not inherit #708.
- `local/`:
  the new member picked up by the packaging and markdown policies.

## Design changelog

Review-and-plan, 2026-09-21 (trail `01m30r7vb7-ksh5skqa-hx3r5phy`), against master at `64b6004e` and the pinned Playwright MCP (0.0.82) run in the session:

- **The chat rides a universal `mission` surface, and `quest` is narrowed to its bro-shaped verbs.**
  `quest say` and `quest ask` refused every non-bro mission (their liveness check read the bro answer) and `quest watch` rendered chat for bro quests alone, so the design's wire did not work as written.
  `mission` (`bro/mission.py`) serves every worker type with JSON payloads, a `--seq` read, a type filter, and bounded watch lines;
  `quest` keeps its verbs, tools, and functions over it, so no prompt, spell, or hold changes.
  Settled with the user over renaming `quest` to `mission` throughout (88 files) and over `mission_*` service tools.
- **Payloads are typed JSON objects, not JSON inside `{text}`.**
  The wire already carried any object up to the bound;
  only the quest surfaces assumed `{text}`.
  A command is `{tool, arguments}` or `{webview: …}`, a reply `{text, files}` or `{error}`, the ready say `{event, vnc}`.
  Settled with the user.
- **Files are the one carrier, image content blocks are off.**
  Playwright MCP writes every screenshot into the workspace anyway.
  Settled with the user.
- **The browser's file access is restricted, the tool that leaves the browser is withheld, and the artifact view mounts inside the root.**
  Playwright MCP 0.0.82 confines uploads, drops, and named outputs to its cwd and output directory and blocks `file://` outright unless `--allow-unrestricted-file-access`;
  its default roster includes `browser_run_code_unsafe`, which runs Node in the server process, and no setting excludes one tool.
  The daemon withholds it and starts the server with cwd `/workspace` and a minimal environment;
  `WorkerContainer.artifact_view` lets the webview mount its view at `/workspace/artifacts`, so a shared ref is uploadable without a copy;
  rendering a local HTML file is out of the vocabulary.
  Settled with the user over the unrestricted flag with a daemon-side path rule, over copying shared refs into the workspace, and over a loopback HTTP server.
- **Playwright facts corrected:**
  the bin is `playwright-mcp`;
  `--disable-dev-shm-usage` is Playwright's own default switch;
  `--no-sandbox` is passed because Docker's default seccomp profile denies the user namespaces the sandbox needs;
  `pdf`, `vision`, and `devtools` are opt-in capabilities;
  a headed browser never idle-closes.
- **A boxed owner reads a mint from its view at once**, since a mint links into every ancestor's view;
  `artifact get` is the unboxed owner's step.
- **Rollout gained its order** (install the release before granting `:webview.vnc`) and the mixed-version statement (one bundle per ride).
- **Smaller:**
  the subscriber drops on `ended` only;
  a webview cannot own a webview;
  `ensure_worker_image` captures the build output;
  the packaging list names every root-level list a member joins;
  the e2e scenario lives in the webview member over ride's fixtures;
  the bare-wire `quest_ask` never waits, which the browser bro answers with the `mission ask --wait` CLI;
  a command before `ready` is dropped, stated as a bound.

Design review round 1 (bro-eyebro on PR #709, 2026-09-21):

- **`quest` keeps every mission's lifecycle lines and its universal `cancel`.**
  The stop guard, the native runner, the automatic admission, and the turn-end notices rely on both, so a strictly bro-typed `quest` would leave a webview holding the turn end unreported;
  `quest` narrows only `check`, `list`, `history`, `say`, `ask`, and the chat lines on `watch`, `mission watch` is optional, and a bro declares `mission` for both harnesses with `shell(…)` or `cli(…)`.
  Settled with the user.
- **Ownership is policy until #522.**
  `## Goal`'s "other bros never touch it" is enforced once the bare `:webview` permit gates opening, held by the browser bro and lacked by others, with the root's direct route kept;
  the type names no persona, and `## Goal` says so at write-back.
  Settled with the user.
- **Files are collected by a workspace diff, minted, and removed.**
  The pinned server puts only automatically named files in `--output-dir` and resolves a `filename` against the workspace root, so scanning one directory missed outputs;
  the diff attributes every file to its command, and the removal keeps the workspace clean and the disk bounded.
- **The whole reply is bounded, and so are a command's files.**
  The text spills first, then the whole reply, so the dispatcher never refuses one;
  `OUTPUT_LIMIT_BYTES` bounds one command's files, the store cap bounds what is kept per file, and the transient write is named as the one unbounded quantity, the browse spell's rule.
- **Every subprocess is supervised while idle.**
  A wait per owned process ends the daemon on any exit, the VNC endpoints are probed before `ready`, and the daemon checks its view mount at start.
- **Docker is the sole stated security boundary.**
  The file rule and the withheld tool are defense in depth, as Playwright's own contract says of the rule, and the residual reach of a browser or server compromise is stated.
- **Rollout states the package-version contract:**
  one repository, one revision, one pin for every distribution, and a loud failure on skew from the daemon's mount check and the missing field.
- **The task page is reconciled at write-back:**
  `## Goal`, `## Shape`, and `## Follow-ups` are brought to this vocabulary in place.

Design review round 2 (bro-eyebro on PR #709, 2026-09-21):

- **`quest` is bro-only on every verb, and the turn-end guard has one rule.**
  Round 1's hybrid left `quest watch` printing a webview's lifecycle but not its chat, and the guards would have needed to know which watch covers which mission;
  now a bro-only flow never meets the word mission, a bro that launches another type declares `mission`, and a turn end is held while a live mission of any type has no background task running, the rule the native runner already applies.
  Settled with the user, reversing round 1's first entry.
- **Ownership is enforced by #522 for summoned children only.**
  #522 gives the root every installed type and a child none unless granted, so a root bro's opening stays policy;
  a child's `:webview` comes through its own bro's configured layers or a summoner's bounded grant, which is where "only the browser bro" is written.
- **Downloads are refused at the browser context, and the collector never meets an open file.**
  The pinned server saves a page-triggered download asynchronously after the tool call returns, the one writer without a completion signal, with a size the page chooses;
  `acceptDownloads: false` in a config file refuses it in the browser, the server's live context routes the refused save into its own rejection listener,
  every other file is closed before its tool returns, and the response-body route covers a file the owner wants.
- **A spill whose own mint fails is answered as a bounded error**, so no reply is ever refused by the dispatcher.
- **The parameterized `mission` verbs are mounted with `cli(…)`**, since a shell roster admits complete command lines by exact match;
  only the fixed watch command fits the roster.
