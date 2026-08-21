---
name: report-usage
description:

This spell should be used when the user asks for a usage report over recorded runs
— "[[report usage]]", "write the usage report", "how much did we spend last month", "where did the tokens go", "usage report for the last two weeks", "usage for yesterday 12-14, focus on cache balance".
Folds every trail in a window into per-class token totals broken down by vendor, model, bro, and harness,
optionally emphasising an angle the caller names,
reconciles the fold against the trail headers' own aggregates,
and commits the report into the directory the operated repository declares for its analyses,
together with any change to the generator that produced it, in one commit,
leaving the push to whoever owns the branch.

parameters: {"window?": "period to report on, in whatever form the caller states it — \"last week\", \"this June\", \"a week till now\", \"yesterday 12-14\", \"since 2026-07-01\"; defaults to the last 30 days", "focus?": "an angle to emphasise beyond the standing aggregates, e.g. \"cache write/read balance\", \"where the reasoning tokens go\", \"which bros grew\""}
version: 3.1.0
---

# Usage report

A standing report on what the recorded runs actually cost. It is committed into the repository as a dated document beside the script that produced it, so every figure in it can be re-derived from code the same commit carries.

## 1. Orient

The generator is `bros/analyst/scripts/trails_usage.py`, and it ships with the analyst — it is there in any install, not just this checkout.

Where its reports go is the operated repository's decision, declared as `[tool.bro.analyst] reports` in that repo's pyproject. The generator resolves it and stops when the repo declares none, because a framework installed into site-packages has no directory of its own to commit into. On that failure: ask the user where this repo should keep its analyses and offer to add the setting, when the session can ask; raise with what is missing, when it cannot. Reports are one `<YYYY-MM-DD>–<slug>.md` per report there — the date it was generated, then what it is about.

Name it so a reader scanning the directory knows which report answers their question without opening any of them: `--slug` takes the window and the angle, as in `--slug "june-2026 cache balance"` → `2026-08-15–june-2026-cache-balance.md`. The date is when the figures were pulled, not the window they cover, so a backfill says so in its slug and a regenerated report sorts after the one it supersedes.

Read the most recent report and the generator before doing anything. The script is the accumulated form of every report before this one, and what it already computes is the shape those reports were read in; keeping that shape steady matters more than any one run's presentation.

If the directory does not exist yet, this is the first report: create it and write the generator.

## 2. Resolve the window

Take it from the `window` parameter, defaulting to the last 30 days ending now. It arrives in whatever form the caller thinks in — a name (`this June`), a span backwards from now (`last week`, `a week till now`), or a slice of one day (`yesterday 12-14`). **You resolve it, not the generator**, which takes ISO bounds and nothing else. Anchor "now" from the shell rather than assuming it, and pass the result as `--since` / `--until` with whatever precision the phrase carried; a range inside a single day is as valid as a month.

Resolve to explicit UTC bounds and use those everywhere — the generator invocation, the report's own header, the filename. A window stated as prose is not reproducible: it means something different every day it is read. A window stated as timestamps means the same thing forever.

Sessions that were still live when the window closed are inside it: their spend is real and already recorded. Say how many the report counted, so a later re-run over the same window landing on a larger figure is explainable rather than alarming.

## 3. Resolve the focus

The `focus` parameter names an angle the caller wants the report to press on — a balance, a trend, a suspicion. It is **additive**: the standing sections render exactly as they always do, and the focus earns extra cuts and a section of the reading on top of them. A caller asking about cache balance still wants to know what the window cost.

Decide what the focus needs in figures, then check whether the generator already produces them. Where it does not, that gap is the work of step 4 — settle it before writing a word of the reading.

A cut added for a focus becomes a standing section of every later report. That is the intent: a question worth computing once is usually worth tracking, and a section that appears and disappears between reports destroys the comparability the series exists for. Add it in the shape you would want it in a report nobody asked the question for.

## 4. Generate

Run the generator over the resolved window. It refuses to replace a report already on disk, because the reading is written into the file it produces and the script cannot reproduce it; `--force` is how you say the reading is expendable.

When the report needs a cut the script does not produce, **extend the script and re-run it** — never compute a figure by hand beside it, and never fork a copy for this window. One generator evolves; git history carries what each report was produced by. A number in the document that no committed code produces cannot be checked, which defeats the point of committing the script at all.

Settle the generator's output before writing the reading. Extending the script after the reading exists means regenerating over it, and `--force` will take the reading with it.

Read the figures as a reader, not as their author: a column rounded for display is not the value it stands for. Take the ratio a claim rests on from the underlying counts, not from the rendered cell — and where a rounded cell would mislead, fix the rendering rather than explaining it in prose.

## 5. Reconcile

The generator reconciles its own fold against the trail headers' aggregates and reports the drift. Read that output — do not assume it passed.

Exact agreement is the expected result, because both sides count the same recorded calls. Any drift at all is a finding: investigate it before writing the report, and if it survives investigation, report it in the document rather than suppressing it. A report that silently rounds over a discrepancy is worse than no report.

## 6. Write the report

The generator produces the figures; this step writes the reading of them into the file it just produced. The document carries both, and keeps them distinguishable — a table is evidence, a sentence about what it means is an interpretation.

Cover the window's totals per token class, the split by vendor and model, and the split by bro and by harness. Read this window on its own terms — what is large, what is disproportionate to the sessions behind it, what does not look like what it should. Reports are not differences of each other, and two of them over overlapping windows subtract to nothing meaningful.

Where a focus was named, give it its own section of the reading, after the standing one and titled for the question it answers. It is where the report says something rather than shows something, so it carries the load: what the figures mean, which of them are noise at this volume, and what would have to change before the answer changes.

Keep the four billed token classes apart, in the report as everywhere else. Shares are taken within a class.

Close with what the window's spend actually bought: what the sessions in it produced. That is the question a usage report exists to answer, and trail counts alone never answer it.

The `usage-report` CLI is the other half of that question. It sums the token footers of the commits in a git range, so it measures what landed, while this report measures what ran. Quote both: the gap between them is what the window spent on work that never reached master, which is a figure worth watching across reports even when neither side is alarming on its own.

## 7. Commit it

The report and every change to the generator go in **one commit** — the commit's own token footer then accounts for the analysis that produced both, so the record of what the report cost sits with the report.

Run the repository's own gates first where the generator changed. It is source in a repository that has standards, and a report produced by a script that does not pass them is a report nobody can trust twice.

Commit; do not push. Where the branch goes is not this spell's decision.

Then hand it over: say where the file is, and lead with what the report found rather than with the fact that it ran — the reader wants the answer, not a receipt. Name the generator changes separately from the findings, because they are a change to the instrument, and a reader deciding whether to believe a new number needs to know the instrument moved.
