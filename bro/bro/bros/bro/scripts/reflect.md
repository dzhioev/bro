---
name: reflect
description: This script should be used when the user asks to step back and turn lessons from experience into durable improvements — "@:reflect:@", "reflect on this session", "post-mortem this incident", "what should we learn from this", "how could this have gone better". Analyzes the current session history (or a given situation/incident), separates one-off slips from systemic friction, traces each systemic finding to the durable surface that owns it — a script, the bro's toolset, the bro's system prompt or shared prompt text, a doc — and drafts concrete edit proposals. After presenting the findings it suggests a delivery per proposal: file it as a task that lands through its own session, or fix it in place when the change is small.
parameters: {"incident?": "the situation or incident to reflect on instead of the whole current session"}
version: 1.0.1
---

# Reflect

Turn friction into durable improvements. Reflection mines what happened for lessons, traces each lesson to the surface that owns it — a script, the bro's toolset, its system prompt, a doc — and drafts the edits as proposals. Each accepted proposal is either filed as a task — landing through that task's own session — or, when the change is small, fixed in place.

## Scope the material

The default material is the whole current session, first user message to now. An `incident` argument (in the appended `# Arguments` section) shifts the focus to that situation instead — gather what this session already knows about it and ask the user for the parts it doesn't. When the incident lives in another bro's recorded run and this session can shell out, `rewind show <trail-id>` replays its trace.

## Find the friction

Walk the material and collect the moments where things went worse than they should have:

- corrections: the user restated a request, interrupted, rejected a tool call, or reversed something already done
- wrong turns: a misread request, a wrong tool or target picked, retries on the same step, work redone or thrown away
- gaps: information or a tool the session needed but didn't have — including every question that reached the user only because no prompt, script, or doc answered it
- policy misses: a rule a prompt or script states but the session ignored, or followed to a bad result because the rule is ambiguous or wrong
- drag: avoidable round trips, noisy output, steps a script should have ordered better

Include your own mistakes plainly — the material is evidence, not a verdict on anyone.

Then filter: separate one-off slips from systemic friction. An edit is warranted only where the same situation would plausibly recur and the existing text or toolset steers it wrong. A single misjudgment against an already-clear rule earns no edit — restating a rule louder is bloat, not a fix. Every added sentence taxes every future session that carries the surface, so the bar for new text is high: prefer fixing wording over adding wording, and deleting text over both when the text itself caused the problem.

## Trace each finding to its owning surface

For each systemic finding, name the one surface whose edit prevents the recurrence:

- **a script** — a step that misled, is missing, or is mis-ordered in a script that ran, or a workflow that recurs often enough to deserve a script that doesn't exist yet; scripts live in the owning bro package's `scripts/*.md`
- **the bro's toolset** — a missing, mis-scoped, or misdescribed tool: the `mcp_servers` / `data_sources` declarations on the bro's class, or the tool's description and behavior in the pack that owns it
- **the bro's system prompt** — the class-level `system_prompt` of the bro or of the ancestor that owns the rule; shared text under `prompts/shared/*.md` only when the lesson genuinely applies to every bro
- **a doc** — a reference doc or CLAUDE.md the session leaned on that was stale, silent, or misleading

Weigh the lesson's generality honestly — both directions fail: a local lesson hoisted into an ancestor or shared text taxes every session for one workflow's problem, while a general lesson patched into the one script where it happened to surface leaves the recurrence alive everywhere else. Aim for the narrowest surface that still covers everywhere the situation can recur.

## Draft the edits

For each finding, draft the concrete change, not a theme: the exact wording to add, change, or delete, and where it goes. With file access, read the current text first and write the edit against it; without, specify it precisely enough that a dev session can apply it unseen — surface, section, current wording, proposed wording. Keep each edit as small as the fix allows, and keep independent fixes as independent proposals — they are reviewed and landed separately.

## Deliver

Present the findings and proposed edits together: per finding, what happened, why it would recur, the target surface, the drafted edit. Then suggest a delivery per proposal: file it as a task, or fix it in place when the change is small — a task-routed edit gets its own review and session, so prefer the task for anything bigger.

To file: with task tools mounted, create one task per accepted proposal carrying the drafted edit in its body, plus the provenance a fixing session needs to revisit the material when it can be identified — the trail id of the reflected run, the session facts the `banner` tool reports (session name, shell command). Without task tools, hand the user the proposal text to file wherever their tracker lives. To fix in place the session must be able to edit the surface — apply the drafted edit and verify it like any other change.
