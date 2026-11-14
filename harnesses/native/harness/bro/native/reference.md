# Bro harness

`bro-native` registers the `bro` harness, which drives the selected bro's native LLM loop.
The harness serves the providers whose wires have native clients;
the recipe's LLM key rides the session's scoped store, so its auth preflight checks nothing.
This page is the harness's side of a managed session and the in-process `bro run` and `bro chat` surfaces;
the runtime around a managed session
— workspaces, isolation, credential delivery, summons, watches, recording
— is `bro/reference/ride.md`.

## The session runner

The runner spawns `bro run|chat …` in the workspace and waits, forwarding SIGTERM to it.
It hands the bro and the prompt to `bro run|chat` past `--`, as the launcher hands them to `do-ride`, so a prompt that reads as a flag stays the prompt;
a solo session runs `bro run` with the ask display preset.
It passes `RIDE_RESOLVED_RECIPE` verbatim as `--resolved-recipe`, so the native loop runs exactly the recipe the launch admitted.
The loop's prompt composes the hold's level text through `bro/bro.py:BaseBro.system_prompt_for`, from the hold its launch surface picks (`bro/launch/AGENTS.md`, "Display and holds").
Boxed sessions run the same `do-ride` command summoned children get;
unboxed sessions provision the workspace clone and run the runtime snapshot's `do-ride` under the same broker-root supervision and scoped credential store.
A resume's existence check is the session's current-trail pointer, from which the runner resolves the trail to continue.

## Tool groups

The bro harness serves the `files` group with its own file and search tools under the `dev` namespace, only their read-only part for `files(write=False)`, and `brash(...)` with its job tools.
Each job and watch starts its line under the job supervisor:
in brash under a finite command list, with the brash policy the native runner writes from the bro's reach into a directory its run owns, and in bash under `brash(ANY)`.
Jobs apply the session's current credential-hook environment file as an overlay.
It leaves `web` and `delegation` unserved, which `bro show` marks.
The model has no native skill loader, so the harness's `Harness.own_tools` contributes `bro::skill`, whose empty result means the requested third-party skill is unavailable.

## Watches and turn ends

The native runner pumps each batch from the session's watch store into the LLM inbox as `watch_delivery` renders it.
A managed session's runtime-owned `mission watch` therefore reaches the model without a watch-mode job;
an in-process run keeps each `bro::watch` it starts in a temporary store its `Runner` owns, through the same path.
Watch lines enter the model after tool results or wake an idle turn.

At each one-shot turn end, the runner applies the shared end-of-turn rule over covered and uncovered missions, live and stalled model watches, pending waking watch lines, running jobs, and live session traffic.
A notice reaches the LLM through the inbox as a user-role item recorded in a `notification` step.
An end returns the last reply, while a wait idles on the inbox until job or watch news, a job's silence notice, or a stall wakes the next turn.
A foreground `job` wait ends on a silent job as a background one does, the job continuing in the background.
Ending the run still closes every job and orphans every in-flight mission, while a delivered `answer` or a `raise` ends it at once.
`bro chat` uses the same inbox to wake on watch lines while continuing to accept a human's message between turns.

## Recording and usage

The native runner records the session's trail in-process (`harnesses/native/harness/AGENTS.md`, "Recording") and publishes the session's trail pointer when its trail opens.
The native LLM loop writes its process's cumulative LLM usage to the session's usage file (`BRO_USAGE_FILE`, `bro.llm.usage`) after every API call.
A summoned one-shot child emits its run lifecycle from `bro.native.runner.Runner.run`;
a summoned interactive one, a manual child among them, emits its `trail` mark from `Runner.send`'s first turn.

## Resume

`ride resume` continues the session's trail at its latest consistent point under the recorded native recipe, producing a new trail with `forked_from`.
A session with trail recording disabled publishes no pointer and cannot be resumed.
A `--llm` replacement on another wire than the recorded one is refused before the session starts.
A kept summoned child's `broker-<channel>` workspace resumes as an interactive `bro chat` continuing the child's trail.
A native trail whose session record predates the resolved-recipe record continues with `bro chat <bro> --fork <trail>`.

## In-process surfaces

`bro run <bro> <input>` and `bro chat <bro> [what]` run in the calling process:
they use ambient credentials and create no workspace or scope.
They read the live installation's home-layer stack (`bro/reference/ride.md`, "Recipe selection") on the bro harness, and only its recipe.
`bro chat --fork [TRAIL_ID] [--at N]` forks recorded history into a new trail, an omitted trail id selecting the bro's newest recorded call.
The fork runs the trail's recorded recipe unless `--llm` replaces it, with source `trail <id>` when using the record;
it checks admission and requires a compatible recorded wire.
