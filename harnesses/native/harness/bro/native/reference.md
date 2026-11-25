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
A resume continues the newest session run log, without reading a trail store.
Before the run starts, the runner registers the lifetime's launch facts (`bro/reference/ride.md`, "The session executable") for the run log it names, from its step 0.

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

Every native run writes its own append-only JSON Lines run log, without consulting a trail store.
By default it creates a fresh file under `<runtime-root>/bro/runs/`;
`bro run` and `bro chat` accept `--run-log PATH` to name it explicitly.
A named path is created exclusively and holds one run.
`Runner.run_log` exposes the opened log's path, or None when a replacement `tracker=` records the run.

A managed session names its run log under `<session dir>/bro/runs/` and starts `bro.native.trail-recorder` before launching the run, waiting for its readiness byte.
The recorder stores its step lines as trail rows, publishes the session's trail pointer, reports recording health and emits the trail mark for one-shot or summoned sessions.
Each lifetime records a trail of its own.
Before starting a continued log's trail, the recorder settles its predecessor's entry in `<session dir>/recording-map.json` and completes its trail from the local log, including tool lists and its end.
A predecessor unknown to the map is recorded whole, oldest ancestor first, with its recorded run facts and the session's location but no historical launch line, git state or summon attribution.
The new trail's edge names the predecessor's trail and the continued step, with kind `resume`.
The recorder persists a minted trail id in the map before sending the start, so a lost reply retries the same start.
A line row uses the trail format's own `run_log` tag, independently of the log's format.
It stops after the run, making a final recording pass.
Nested native runs write their own logs under the runtime root and neither record trails nor change the pointer.
`--no-trails` disables the session recorder, not the run log.
A trail-store outage does not fail the run;
its log keeps the steps the recorder has not confirmed.

The native LLM loop writes its process's cumulative LLM usage to the session's usage file (`BRO_USAGE_FILE`, `bro.llm.usage`) after every API call.
`Runner.run` delivers a one-shot run's result through its broker channel;
the interactive surface delivers a summoned conversation's answer.

## Resume

`ride resume` continues the conversation from the session's own run-log chain at its latest consistent step, under the recorded resolved recipe and source.
It works with a record-only trails token, with `--no-trails`, and while the store is unreachable.
The session runner picks logs under `<session dir>/bro/runs/` newest by name, passing over files without a complete `open` and format-2 logs without step 0.
A workspace with no continuable log is refused as holding no session run log;
one whose newest non-debris log predates format 2 is refused without migration.
Before the run starts, the session runner removes the old trail pointer, even if the run never opens;
only the recorder publishes the new trail's pointer.
A kept summoned child's workspace resumes as an interactive `bro chat`, like any kept native session.
A session record predating resolved recipes is refused with an instruction to start a new session.

A format-2 run log names the log it continues in `open.forked_from`, as `{run, step}`.
Both logs must be in the same directory.
A missing ancestor file or bound step is refused, naming the files.
The conversation includes each ancestor through its bound, excluding their system-prompt steps.
The latest consistent step leaves no unanswered tool call;
a continued log's system prompt is itself a consistent point, carrying the ancestor conversation even if that lifetime wrote no input.
A root holding its prompt alone has no consistent continuation point.

Step 0's `session_at` is the character offset of the prompt's session part.
Continuation keeps the recorded prompt byte for byte when that part equals the session part composed for the new launch;
otherwise it composes the whole prompt again.
Persona changes alone do not replace the recorded prompt.
Every replayed call must use the selected recipe's wire, checked before the new log opens.
An unchanged prompt and provider/model can seed a server-chained wire at a completed call's response id;
other continuations restore the wire from the local records.

A resume's notice compares the launch facts with those of the newest log in the continued chain that the conversation takes a step from, its system-prompt step not counting;
when that log registered none, the notice compares nothing.
The runner hands it to `bro chat` through the suppressed `--resume-notice`, which requires `--fork`,
and the run sends it as a `notification` step ahead of its first exchange's input, starting no turn of its own.
An in-process `bro chat --fork` gives no notice and registers nothing.

## In-process surfaces

`bro run <bro> <input>` and `bro chat <bro> [what]` run in the calling process:
they use ambient credentials and create no workspace or scope.
They read the live installation's home-layer stack (`bro/reference/ride.md`, "Recipe selection") on the bro harness, and only its recipe.
`bro chat <bro> [what] --fork [RUN_LOG] [--at N]` continues a local run log and renders its bounded chain as history.
Omitting the path picks the bro's newest log by name among the default directory's 200 newest files, regardless of the surface that wrote it.
The new log is created beside the continued one;
`--run-log` naming another directory is refused.
The fork runs the log's recorded recipe unless `--llm` replaces it, with source `run log <id>` when using the record.
Its `open` must name the selected bro, and its recipe must be admitted on a compatible wire.

`bro chat` exits with `conversation saved to <path>` when the conversation opened a run log.
Only a managed session's recorder turns that log into a trail.
