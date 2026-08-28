---
name: review-diff
description:

This spell should be used when the user asks for a code review of a revision, branch, or range
— "[[review diff]] on this branch", "review HEAD", "review abc123..def456", "what do you think of this change"
— judging the change against the repository's own standards and producing a list of questions and suggestions for its author.
The review is read-only and changes nothing.
For driving a GitHub pull request to a verdict, [[review pr]] wraps this judging in the PR conversation loop.

parameters: {"target?": "revision, branch, or revision range to review; default: the current branch against its merge base with the default branch"}
version: 1.0.0
---

# review-diff

Judge a change against the standards that bind it, and hand its author a findings list:
questions where intent is unclear, suggestions where the code falls short of the bar.
Read-only — the change stays the author's to edit.

## 1. Resolve the diff

The `target` argument names what to review:

- a revision range (`abc123..def456`) — exactly that;
- a single revision — that commit (`<rev>^..<rev>`);
- a branch — what it adds over the default branch:
  `$(git merge-base origin/master <branch>)..<branch>`;
- absent — the current branch the same way.

Read `git log` over the range before the diff:
the commits are part of the change
— their split and messages are reviewable against the repo's conventions too.

The object of review is the change, not the repository around it;
a whole-codebase sweep is a different job ([[audit]], where that spell is available).
Pre-existing problems the change merely sits next to are out of scope
— except where the change extends or copies them.

## 2. Ground yourself in the standards

Before judging anything, read what binds the change:

- the repository's own guides
  — the root development/contribution doc and the subsystem docs covering what the diff touches, style and policy documents, commit conventions;
- the development style policy — `dev-style-source::read`;
- the conventions the surrounding code already follows.

Authority runs in that order:
where the repo has spoken, its rule wins;
where it is silent, the development policy holds;
where both are silent, widely-accepted practice.
Every finding says which of the three it rests on.

## 3. Review

Read the full diff, then read past it until the change makes sense in context
— the callers and siblings of what changed, the docs that describe it, the tests that cover it:
a diff hides its blast radius, and most wrong findings come from judging lines without their surroundings.

Work through, per commit and for the change as a whole:

- **correctness** — the change does what its commits claim;
  edge cases, failure modes, and lifetimes hold up;
  nothing the diff touches is left half-migrated.
- **design** — the right mechanism at the right altitude and place;
  no consumer-specific leakage into generic layers;
  breadth matches the problem rather than the smallest possible diff.
- **standards conformance** — the specific rules gathered in step 2, checked against the diff.
- **tests** — new behavior is covered;
  assertions assert behavior rather than mirroring the source;
  nothing meaningful became silently untested.
- **docs** — documentation the change obsoletes is updated;
  no stale references to renamed or removed things remain.
- **quality** — naming, duplication, dead code, comment discipline, message and log wording.

Where a suspicion is mechanically checkable
— would this test fail, does the type checker flag it, does the repo's linter object
— run the check read-only in the workspace's checkout instead of asking the author or guessing.
Say what you ran.

## 4. Report

A numbered findings list, then a verdict.

Each finding:

- phrased as a **question** where the author must answer before it can be judged (unclear intent, a suspicious choice that may be deliberate),
  or as a **suggestion** where the improvement is concrete and actionable as written;
- marked **[blocking]** when you would withhold approval over it, **[minor]** otherwise;
- carrying every location it touches (`file:line`), what is wrong, and the standard it rests on
  — a cited repo rule, the development policy, or judgement.

Close with:

- the verdict — approvable as-is, approvable once the blocking findings are addressed, or needs discussion;
- what you checked mechanically and what came out clean, so silence is distinguishable from not looking.

The review is read-only:
make no changes, stage nothing, push nothing.
What to do with the findings is the author's and the user's call.
