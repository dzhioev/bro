---
name: reflect
description:

This spell should be used when the user asks to improve what a session ran under from how its runs went
— "[[reflect]]", "reflect on this session", "[[reflect on the last few assistant sessions]]", "reflect on trail <id>", "post-mortem this run", "what should we learn from this", "how could this have gone better".
The improving half of a loop over a bro's definition:
a session runs under prompt texts (a spell, the bro's system prompt, the shared prompt text every bro gets, a tool description, a doc it leaned on) and under the bro's declaration with the scope its launch gave it (tools and data sources, spells, summon targets, credentials, permits);
reflect reads one run or several against the version of that definition in force during each
— step by step and rule by rule through the texts, and through what the run reached for and lacked —
keeps only the divergences the definition itself caused, and writes its next version as edits small enough for the next runs to test.
Each edit is fixed in place through this session's pull request, or filed as a task when its surface is out of reach.

parameters: {"material?": "the runs to reflect on instead of the current session: a trail id, or a description such as the last few sessions of a bro"}
version: 3.0.0
---

# Reflect

A session runs under a definition.
Its texts are a spell,
the bro's system prompt,
the shared prompt text every bro gets,
a tool description,
a doc it leaned on;
its declaration, with the scope the launch gave it, is the tools and data sources,
the spells,
the bros it may summon,
the credentials,
the permits.
Reflection is the improving half of the loop over that definition:
the runs are read against the definition that drove them,
the definition gets its next version,
and the next runs test it.
The material is one run or several
— this session, a recorded trail, the last few sessions of a bro
— and the output is the diff to the definition that drove them.
Any part of it can be the surface a run exposed.

## Scope the material

The default material is the current session, first user message to now;
the definition in force is then what this session itself ran under.
A `material` argument (in the appended `# Arguments` section) names other runs.
Where the session has a shell, `rewind show <trail-id>` replays a trail named by id;
a run named by description ("the last few assistant sessions") is located with `rewind list` and its filters (`--bro`, `--since`, `--limit`),
and when more than one trail plausibly matches and the choice changes the material, confirm the pick before reading.
Several runs under one definition are read together:
a long trail is rendered whole into a scratch file (`rewind show <trail-id> > <file>`) and read from there,
and `rewind grep <pattern> <trail-id>…` finds one step's traces across them.

Read each run against the version of the definition in force during it, not the current files.
The trail carries most of it:
a spell as the `spell::<name>` or `bro::cast` result,
the system prompt as a claude trail's `prompt_snapshot` notice or a bro trail's `system_prompt` step (`rewind steps`),
a doc as the tool served it,
the scope as the launch line and the banner the run read (`may_summon`, `permits`, the grants on the command).
What the trail does not carry is recoverable only for a surface the launched repository owns, from the commit the trail's launch line names (its `--into` ref, the git state at launch);
a bro or a text an installed package supplied has no version the trail names.
Where the version in force is unavailable, say so and skip findings on that surface:
a divergence seen there is reported without a cause, since the words the run saw are not in evidence.

## Read each run against its definition

Walk the texts, not the run:
step by step where they prescribe steps, rule by rule where they state rules.
For each, establish what the run did with it
— followed it as written,
improvised around it,
got corrected by the user on it,
skipped it,
or did it out of order.
Followed as written needs no note.
Then walk what the run reached for beyond its texts and did not have:
a summon denied,
a credential the scope lacked,
a tool it did without or improvised around,
a permit it did not hold.
Each shows in the trail as a denial, an error, a workaround, or a question that reached the user.
Where the run diverged, name what in the definition caused it:

- an ambiguity that made the session guess
- two places that disagree
- a rule and a step that cannot both hold
- a text that presumes what the declaration or the scope does not carry
- a specific where the general rule would do
- a step in the wrong order — one that reads what a later step still changes
- output the text asks for that nobody acted on
- silence where the procedure needed an instruction — a step it lacks, a case it does not cover

A divergence against a definition that was clear and complete on the point is the session's, not the definition's:
a single misjudgment against an already-clear rule earns no edit
— restating a rule louder is bloat, not a fix.
Across several runs, a divergence that recurs weighs more than one seen once, since a single run can diverge for reasons of its own;
a fault in the definition itself
— the disagreement, the pair that cannot both hold, the wrong order, the presumed capability
— counts on first sight.
When the material is this session, read your own run the same way;
the trail is evidence, not a verdict on anyone.

## Keep to what the next runs need

The measure of every edit is whether the next runs go better because of it.
Anything that is not the definition failing the run is out of scope:
a one-time ask from the user,
ad-hoc work the session did beside its texts,
an observation that something worked.
A correction the user made is a rule only when they say so, or when the material shows it recurring;
otherwise it stays what it was, a correction made in that run.
Every added sentence taxes every future run that carries the text, and every added grant widens what every future run may do, so the bar for both is high:
prefer fixing wording over adding wording, deleting text over both when the text itself caused the problem,
and widen scope by exactly what the run lacked — the one target, kind, or permit.

## Trace each finding to its owning surface

For each finding, name the one surface whose edit prevents the recurrence:

- **a spell** — a step that misled,
  is missing,
  or is mis-ordered in a spell that ran,
  or a procedure the run improvised that recurs often enough to deserve a spell that doesn't exist yet;
  a spell is a file under the owning bro package's `spells/` and an entry in the class's `spells` declaration
- **the bro's declaration** — the roster and the reach the run had:
  the `tools` / `data_sources` declarations on the bro's class, its `may_summon`, `extra_secrets`, and `features`, or a tool's description in the pack that owns it.
  A tool's behavior is code and outside this loop;
  a tool the run lacked is mounted when it exists, and the text that leaned on it is edited to name the gap when it does not
- **the launch scope** — what a launch adds to the declaration:
  the `--grant` / `--revoke` layer of the launch line or of the summon that started the run, the repository's `[tool.bro]` grant and revoke lists, and the host config's layers;
  a spell that composes a launch line or a summon for the user owns the grants it names
- **the bro's system prompt**
  — the class-level `system_prompt` of the bro or of the ancestor that owns the rule;
  shared text under `bro/prompts/shared/*.md` only when the lesson genuinely applies to every bro
- **a doc** — a reference doc or AGENTS.md the run leaned on that was stale,
  silent,
  or misleading

Weigh the finding's generality honestly
— both directions fail:
a local lesson hoisted into an ancestor or shared text taxes every session for one workflow's problem, while a general lesson patched into the one spell where it happened to surface leaves the recurrence alive everywhere else.
Aim for the narrowest surface that still covers everywhere the situation can recur:
for scope, the class declaration when every run of that bro needs it, the repository's `[tool.bro]` when every session of that repository does, and the launch line or the spell that composes it when one workflow does.

## Write the next version

For each finding, write the edit itself, not a theme:
the wording, the declaration entry, or the grant to add, change, or delete, and where it goes.
Read the current text first and write the edit against it
— the reading was against the version in force, and where the current file has moved on since the run, a finding it already covers is closed rather than fixed twice;
without file access, specify the edit precisely enough that a dev session can apply it unseen:
surface, section, current wording, proposed wording.
Keep each edit as small as the fix allows and independent fixes as independent proposals, reviewed and landed separately:
an iteration stays small enough for the next runs to test, and the definition converges instead of accumulating rules and grants.

## Deliver

Present the findings and the edits together, per finding:
the step, rule, or capability, what the run did with it, what in the definition caused it, the surface, the edit.
Whether the turn ends on the proposals before delivery is decided by the session's hold, not by this spell.
Delivery is per proposal:
fix it in place when this session can edit the surface
— apply the edit, verify it like any other change, and land it per [[run pr]];
file it as a task when the surface is beyond this session's reach, the fix is substantial design or code work, or the user wants it deferred;
hand the user the entry when the surface is the host's own config, which no repository carries.

To file:
with task tools mounted, create one task per proposal carrying the drafted edit in its body, plus the provenance a fixing session needs to revisit the material when it can be identified
— the trail ids of the reflected runs,
the session facts the `banner` tool reports (session name, shell command).
The task goes to the tracker of the repository owning the surface:
the mounted task tools reach the current project's tracker only,
so a proposal against another repository's surface files through that repository's own tracker (`gh issue create --repo <owner>/<name>`),
or falls back to handing the user the text.
Without task tools, hand the user the proposal text to file wherever their tracker lives.
