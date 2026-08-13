---
name: audit
description: Use this spell to run a thorough, read-only audit of a codebase — sweeping for development-policy and style violations, documentation drift, dead or outdated code, module/structure problems, test-coverage gaps, and aesthetic inconsistencies — and to produce a severity-tagged findings report. Trigger on "audit the codebase", "[[audit]]", "do a code audit", "find problems in this repo", or similar. The audit is read-only and makes no changes; what to do with the findings is left to the user.
version: 1.1.1
---

# Codebase audit

A systematic sweep of a repository for problems, judged against the repo's own development policies and documentation rather than invented rules. The audit is read-only — it finds and documents, it does not fix; what to do with the findings is the user's call.

## 1. Scope and orient

Settle the scope first: the whole repository, a single subsystem, the diff of a PR or branch, or just recently-changed files? Take it from the request, and let it drive which areas you emphasize and how deep you go — a targeted audit of one module is not the same sweep as a whole-repo pass.

Then, before judging anything, read the audited repo's own development policies, style guides, and documentation — whatever defines its conventions, architecture, commands, and testing setup. Those are the primary standard you audit against; internalize them so findings cite the repo's own rules, not your assumptions.

Note the environment: how the repo is built, run, and tested, and which gates it provides (formatter, linter, type-checker, test runner) and how each is invoked. Run whatever setup or environment probe the repo documents.

## 2. Cover these areas

Do the mechanical checks before the qualitative reads. Judge against three standards in descending authority: first the audited repo's own documented policies and conventions, which are authoritative for that repo; then, where the repo is silent, your own development policies — the standards you carry as a developer, which an arbitrary repo does not restate and which are not part of it unless it documents them itself; and finally, where both are silent, widely-accepted practice. Say which of the three any finding rests on. Keep the repo's policies and your own distinct — they are not the same standard, and where they conflict the repo's own convention wins, so hold the code to your own policies only where the repo has not spoken.

### Coding style / policy conformance
Run the repo's formatter, linter, and type-checker in check (non-mutating) mode and collect the output first — they catch mechanical violations cheaply. Then search for the semantic rules those tools can't enforce — the naming, typing, sentinel, message-style, and CLI conventions the repo's policies and style guides define — and check each documented rule against the code.

### Documentation accuracy
Cross-check every documentation file against the actual source: paths, module/class/function names, command names and flags, signatures, and behavioral claims. Flag drift (renamed / moved / removed / changed), over-specification (restating detail the source owns instead of pointing at it), duplication (the same fact in two places that will diverge), and missing docs (a subsystem complex enough to warrant an index by the repo's own bar but lacking one).

### Dead / outdated code
Stale TODO/FIXME/HACK markers (assess each as live or stale); symbols defined but never referenced outside their own module; patterns the repo has migrated away from; unreachable or structurally-invariant branches. Trust the linter and type-checker for what they already catch — hunt for what they miss.

### Module / file structure
Oversized files (note the natural split points within them); misplaced code (logic living in the wrong subsystem); top-level clutter (modules whose scope belongs inside a package); and inconsistency across parallel subsystems where it reads as oversight rather than intent.

### Test coverage
Modules with real logic and no tests; test files that exist but aren't wired into the repo's test registry or discovery, so they silently never run; and thin tests that assert nothing meaningful.

### General aesthetics
Copy-paste at module grain (three or more near-identical blocks or files is the threshold — flag the pattern, not each repetition); inconsistent naming across parallel things; awkward or mis-leveled log/error messages; and import-style inconsistencies.

### Beyond the baseline
The areas above cover maintainability, and the first covers any rule either standard pins down — the repo's policies, or your own where the repo is silent — in any category, not just style, so a stated fail-fast or secret-handling requirement is caught there. What a baseline misses is the risk classes neither standard spells out as a checkable rule: security and secret-handling surfaces, failure modes no rule mandates (timeouts, retries, partial failure, resource leaks), performance, dependency and supply-chain health, concurrency and resource safety, API/compatibility. Judge what the codebase's domain warrants and hunt those actively too — there is no rule to check against — and skip the ones it does not.

## 3. How to run it

Run the mechanical gates yourself first — that's ground truth and lets you catch false positives later.

If the runtime lets you fan work out to parallel sub-investigations, use it for the file-spanning, judgment-heavy checks (the documentation cross-check, the test-coverage map, the dead-symbol cross-reference): scope each to a slice and have it return findings WITH evidence, pointing at both the claim and the source reality. If parallel work isn't available, do the same checks sequentially with the same rigor — the breakdown into independent slices is what matters, not the concurrency.

Verify the load-bearing and high-severity findings yourself before reporting; watch for false positives such as generated or ignored files, harness artifacts, and tool quirks. If a reported finding turns out correct on inspection, drop it and say so.

Scale breadth to the request: a quick look warrants a light pass; "thorough" or "comprehensive" warrants a wider sweep plus an adversarial verification pass over the findings.

## 4. Report

Produce a Markdown report — one section per audit area, each a numbered list of findings. Give each finding a severity tag and a short title, then describe it as fully as it needs: what the problem is, why it matters, and every location it touches. A single problem may span multiple files and lines, so name all the affected locations rather than collapsing it to one, and let the description run however long the finding warrants.

Record what you actually checked, not just what you found: name the gates and checks you ran and which passed clean, so a reader can tell "nothing wrong here" apart from "didn't look here." Mark any finding you couldn't fully verify — one that needs runtime behavior or domain knowledge to confirm — as uncertain rather than asserting it.

Severities: BUG (incorrect behavior or contract violation), STYLE (violates a written development policy), DOC (stale / missing / over-specified / duplicated documentation), DEAD (dead or obsolete code), STRUCTURE (file/module organization), TEST (coverage gap), NIT (minor, low priority).

Close with a Summary covering overall codebase health and the highest-impact things to address — as thorough as the findings call for, not constrained to a fixed length.

The audit is read-only: make no changes. What to do with the findings — fix now, defer, or ignore — is the user's decision.
