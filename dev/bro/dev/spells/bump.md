---
name: bump
description:

This spell should be used when the user asks to take a newer version of a dependency the repository pins
— "[[bump <dependency>]]", "[[bump bro]]", "bump the framework", "update <dependency>", "take the latest <dependency>", "move the <dependency> pin", "bump <dependency> to <version>".
Follows the dependency's playbook where the `man` manual carries one,
moves the pin and syncs,
reads the range the move takes whole,
cross-checks every change against how the repository reaches the dependency
— the call sites the type checker cannot see, and the defaults the repository runs under without calling anything —
and adapts or opts out of what no longer fits,
reads the range for abilities and fixes that replace the repository's workarounds and adopts them,
smokes the repository against the new version,
commits the bump with the range as its body,
and hands off to [[run pr]].

parameters: {"dependency": "what to bump: a package, a tool, an image, or anything else the repository pins", "to?": "version or ref to take; default: the newest the pin's channel offers"}
version: 1.0.0
---

# bump

Take a newer version of one dependency into the repository and leave the repository working on it.
The pin move is usually one command;
the work is reading what the range changed and holding the repository to it
— the surfaces a type checker cannot see and the defaults nothing calls are where a bump breaks silently
— and taking what the range makes possible.

## Arguments

Passed values appear in the `# Arguments` section appended by the spell tool:

- `dependency` — what to bump, as the user named it.
  Packages released together as one, such as a framework's distributions, move as one dependency.
- `to` — the version or ref to take.
  Default is the newest version the pin's channel offers:
  the head of the branch it follows, the release a tag stream or registry names latest.

Below, `<old>` is the version the repository pins before the move and `<new>` the one the move lands on.

## The playbook

A dependency with details of its own carries a playbook:
a `bump-<dependency>` topic of the `man` manual, whose roster `man-source::read` lists.
Match the dependency the user named against the dependency each page's summary names, not only its topic's spelling
— "the framework" names the `bump-bro` page.
Read the matched page before step 1, and follow it within each step it names:
where the pin lives and how it moves, where the range is read, the surfaces the repository reaches the dependency through, the smoke and the gate, the commit's form.
What a page leaves out, the step's text below covers;
with no matching topic, the steps below are the whole procedure.

## Step 1 — locate the pin and move it

Find every place the repository pins the dependency
— a lock and the source table resolving it, a version file, an image tag, a version in CI or setup scripts —
and the repository's own way of moving it, a bump script or a documented command, which is used rather than re-implemented.
Read `<old>` off the pin before touching it.
A dependency the repository does not pin, or pins at versions that disagree, goes back to the user with what was found;
the bump stops there.

Move the pin to `to`, or to the newest version its channel offers.
A pin that follows no channel, fixed at a version or commit, moves nowhere without `to`;
say so and ask for the version.

Read the pin again:
every place names `<new>`.
A place left at `<old>`, or places that disagree, is an error to fix before going on, not a state to work from.
`<new>` equal to `<old>` means the pin was already there;
report it and stop.
`<new>` has to come after `<old>` on the dependency's own line of releases, a later version or a descendant commit, or the range omits exactly the changes the move removes and step 4 reads a retired ability as a new one.
A downgrade or a ref off another line of history is a different procedure from this one;
stop there and report the two versions.

Then sync the repository the way its own setup does
— its `setup.sh` or docs name the groups and extras.

## Step 2 — read the range

The range is everything the dependency published between `<old>` and `<new>`:
its commits, changelog entries, or release notes, whichever records its changes most completely.
Fetch it into a directory outside the repository
— the session's scratch directory where the harness names one, `mktemp -d` otherwise —
and read it there rather than through an API's summary.

Read the range whole:
each entry says what changed and why.
A range of hundreds of entries is still read whole
— skimming a range is how a contract change gets taken unadapted.

## Step 3 — cross-check the range against how the repository reaches the dependency

The repository's type check and test suite cover what they import and execute, and nothing else.
A contract change on any other surface passes a green gate and fails in production, so this step is the gate for those surfaces:
for every change in the range, decide how the repository reaches what it changed, grep the repository for that reach, and where the grep hits, read the reached thing whole on both sides of the move and what the repository consumes of it.
A contract is everything a caller can observe
— arguments, output, exit status, the environment read, the files written, the side effects
— and a caller relying on any of them is as invisible to the gate as one passing arguments.

The reaches the gate does not see:

- **Names** — commands, flags, settings keys, environment variables, tool, hook and event names, and strings in declarations the type checker cannot see.
  Grep for each changed name in scripts, CI workflows, git hooks, configuration, and docs, and read each use for what it consumes.
- **Formats** — output the repository parses, files the dependency writes and the repository reads afterwards, and schemas the repository fills.
- **Copied files** — a file the repository holds as a copy of a pattern the dependency ships.
  Diff the copy against the pattern at `<new>`;
  what changed upstream is what the copy is missing.
- **Behavior the repository runs under** — a feature on by default, a default that changed, a new limit or timeout.
  No call site names it, so no grep finds it:
  the repository reaches it by running the dependency.
  Hold each against how the repository runs the dependency and decide whether that use wants it:
  keep it, opt out through the switch the dependency gives where the repository already configures the dependency, or adapt to it.
- **Everything else** — what the repository imports and executes is the gate's;
  nothing further to check by hand.

A change can reach the repository two ways and gets both reads.
Each mismatch is adapted in this bump, and each unwanted default opted out of:
the bump is what breaks the call site or brings the behavior in, so the answer belongs with the pin move, not to a later session.
Where questions reach the user, put the defaults you would opt out of to them, each with what it does to the repository and the switch you would set, and settle them with the adaptations;
unattended, opt out of a default the range's own text names a switch for and that works against the repository's use, and name the rest in the report.
Keep a list of what you adapted and opted out of, and the change behind each;
step 6 puts it in the commit body.

## Step 4 — adopt what the range makes possible

A repository accumulates work around what a dependency lacked:
a script re-implementing what the dependency did not ship, prompt text steering around a bug, a copied file patched where the upstream one fell short, a flag or a setting dodging a behavior since fixed.
A bump is the moment those become removable, and nothing else prompts it
— so look for them every time, and adopt wherever the replacement is clean.

Read the range for abilities and fixes, with the dependency's docs where the playbook names them.
Then read the repository for what it does around their absence:
comments and docs naming a limitation or bug of the dependency, prompt text instructing around one, scripts and helpers whose job the dependency now does, copied files whose upstream now covers the case, and settings set to avoid a behavior.
Pair them:
each ability or fix that replaces something the repository holds is a candidate, with what it replaces and roughly how much changes.

Then decide:

- where questions reach the user, put the candidates to them as the set you intend to adopt, each with what it replaces, and adopt what they confirm;
- unattended, adopt the one-for-one replacements
  — the workaround comes out and the dependency's own thing goes in, nothing else moves
  — and leave the larger ones as tasks{{iff #features contains brog}} (`brog::create_task`, one per candidate, naming the ability and what it replaces){{else}} named in the report{{end}}.

Each adoption is its own commit (step 6):
a reviewer judges it apart from the bump, and a wrong one reverts alone.

## Step 5 — smoke

Run the repository's formatter, then the smoke the playbook names
— without one, the cheapest run of the repository's own commands that loads it against `<new>` —
so an import, declaration, or startup error surfaces before the suite does.

The repository's own gate runs once, in [[run pr]]'s "Pre-review check" on the tree that ships, and is this bump's gate, with any stage the playbook adds for the dependency.
What neither the smoke nor the gate exercises, step 3 is the gate for.
A red smoke or gate is fixed in this bump:
a failure the pin move causes is the bump's to adapt
— after step 6, a fixup the fold groups into the bump commit, its contract change added to the bump's body —
and one it does not cause is reported to the user rather than worked around.

## Step 6 — commit and hand off

Two kinds of commit, each made through [[commit]]:

- **The bump**:
  the pin move together with every adaptation and opt-out step 3 forced, so the tree works at this commit.
  The body summarizes the range in the form the playbook names
  — without one, a line per change the repository reaches, by version —
  followed by each adaptation and opt-out and the change that forced it.
- **Each adoption** from step 4 on its own, its body naming the ability or fix and what it replaced.

The split is decided here, and [[run pr]]'s fold keeps it:
the bump and each adoption reach the base branch as the commits they are, so a reviewer judges each apart and a wrong adoption reverts alone.

Then [[run pr]]
— it owns the rest, and chains into [[land]] on approval.
Cast it in the response that closes the commits;
the hand-off is this flow's terminal step, not a decision to put back to the user.{{when #features contains brog}}

Where the session has a task, record the bump on it before the hand-off:
`brog::add_comment(<task-id>, topic='bump', body='<dependency> <old> → <new>, <n> changes; adapted: …; opted out: …; adopted: …')`.{{end}}
