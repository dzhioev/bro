# Development style

- Be concise.
  Surface short status updates between tool calls;
  skip the running commentary.

- Name identifiers with full English words, not truncations or abbreviations (`connection` not `conn`, `response` not `resp`);
  only standard tokens stay short
  — initialisms (`id`, `url`, `db`), word-clippings (`config`, `repo`, `info`), and a few fixed idioms (`msg`, `src`, `args`).

- Stay in scope, but speak up.
  If you spot something worth improving outside the task
  — a bug, a stale abstraction, a doc that's drifted, a redundant pattern
  — propose it and let the user decide, don't silently do it.

- Don't shrink the fix to shrink the diff.
  Scope is the task's goal, not the number of files touched:
  when the clean fix means changing a shared abstraction and all its consumers, or rewriting a doc section instead of patching one paragraph, that breadth is the fix.
  Before presenting a proposal, evaluate it against the fix you would design if diff size were no concern
  — a gap between the two means you're optimizing diff size over result quality:
  work out the clean version and lead with it, even when it touches more than you'd like.
  This widens how thoroughly you fix, not what you take on:
  improvements the task doesn't need still get proposed, not silently done.

- Update docs when your change makes them out of date;
  don't add new doc files speculatively.
  When you rename or remove a symbol, flag, or behavior, grep the docs for the old name and the rationale that leaned on it
  — stale references and now-false why-claims don't surface on their own.

- Write markdown prose one sentence per source line, splitting a long sentence further at its major clause boundaries
  — a semicolon, a colon that introduces a clause, the em-dash clause whose dash then opens the new line.
  Never strand a bare label or a lone reference on a line of its own:
  what a break leaves on either side has to read as a clause, so the em dash separating a list item's term from its gloss is punctuation rather than a boundary, and the term keeps the start of its gloss.
  A reword then shows up in review as the clause it touched.
  Line length is not the unit: a short sentence is a short line, and nothing is padded out to a column.
  Never break inside a code block or a table row, or anywhere adjacent to a `{{…}}` group, whose surrounding whitespace renders literally;
  keep a continuation line in its list item's content column, and repeat a `>` marker on every line of a quoted paragraph.
  `check-markdown` holds a reflow to whitespace and flags a paragraph left as one source line.

- Write comments and docs for a reader who has the final code and the whole repo but wasn't in the room while you wrote it:
  they never saw the alternatives you weighed, the audit/ticket you worked from, or what the code said before.
  Self-explanatory means recoverable by normal navigation:
  the reader's toolkit is identifier names, signatures and types, control flow, and one grep or jump-to-definition, and whatever that toolkit reconstructs needs no comment
  — however non-obvious the why felt while you decided it.
  Fresh out of a design discussion every decision reads as a non-obvious why;
  the navigation test, not that feeling, decides.
  Three kinds of "why" leak through anyway; the test kills all three:

  - cross-layer wiring — who consumes this symbol, env var, or file and what happens on the other side:
    one grep on the name lands the reader on the owning side, which carries the story;
    don't restate it at the other end
  - construct semantics — what `finally:` guarantees, what `asyncio.to_thread` moves off-loop, what a template directive means:
    the language and the reference docs own those
  - design-defense — why the design is right, what it protects against, how the pieces cooperate:
    that discussion belongs in the PR/task/spec;
    the code carries at most the one-sentence constraint the reader must not break

  What earns a comment is information that exists nowhere navigable
  — an external contract, a non-local invariant no single file shows, the reason behind a magic value, a deliberate omission that would otherwise read as a bug
  — stated in a sentence or two, never the discussion that produced it.
  Still cut the trajectory-anchored kinds:
  roads not taken (defending against an alternative the reader wouldn't reach for on their own),
  unresolvable refs (audit/ticket ids, "as discussed"),
  change-narration ("now" / "used to" / "is gone" — state the behavior, not the transition),
  and the one the act of editing breeds
  — a comment or doc framed around a symbol, file, or behavior this change just deleted or renamed, so re-read every comment near a removal or rename, not only the code.
  Likewise cut per-assertion narration in tests and help/doc text that narrates a use case instead of saying what the thing does.

  The final gate is the deletion test:
  remove the comment — if a competent reader would then edit the code wrongly or have to leave the repo to search, keep it;
  otherwise it was trivia.
  Default to none.
  Docs are in the same scope:
  an AGENTS.md entry faces the same tests, and one that restates wiring, construct semantics, or design rationale the source already shows is the same leak in doc form.

- Conversational emphasis is not artifact emphasis.
  A point you and the user dwelt on earns no extra prominence in the code.
  A single decided fact
  — a constant's value, a default, a rationale
  — lives in exactly ONE place:
  define it once and reference it;
  never restate the same specific detail across multiple files (source and docs alike).
  Repetition is noise, and it rots the moment the value changes.

- Respect the boundary you write behind.
  A standalone or lower-layer module must not name its callers or the reason it was commissioned:
  a generic component names no specific consumer in its identifiers, comments, or docs, and describes what it is
  — not what it isn't or whom it serves.
  "Built for X" is X's context, not the module's;
  leaking it inverts the separation that justified the split.

- Fail fast on violated assumptions
  — make it the default, not something to be asked for.
  When data is malformed, missing where it's required, or in a state that "shouldn't happen", raise and stop rather than coping
  — no fallback value, no swallowing try/except, no silent continue/skip, no permissive default for a value that must be present, no coercing an unexpected type.
  Reserve graceful handling for genuinely expected conditions (optional input, known-transient errors);
  recovering from an impossible case only hides the bug and moves the failure far from its cause.

- Teardown goes through a context manager, not an inline `try:`/`finally:`.
  When cleanup must pair with setup
  — close what was opened, restore what was patched, cancel what was started
  — scope it with `with`:
  use the stdlib managers (`closing`, `ExitStack`, …) or write a small custom one (`@contextlib.contextmanager` makes it a few lines), so the pairing is named, reusable, and impossible to forget at the next call site.
  `finally:` is reserved for the shapes a `with` cannot express:
  the inside of the context manager (or teardown helper) that owns the release;
  an epilogue that consumes values computed inside the block (`__exit__` sees the exception, not the block's results);
  and sequencing within teardown itself, where a later cleanup step must run even when an earlier one raises.

- A test asserts what the code does, not what the source says.
  A test's expected value copied from the implementation
  — a constant, a default, a registry entry, prompt text
  — makes the assertion a mirror:
  the only failure it can catch is an edit that forgot to update the copy, so it verifies nothing and taxes every legitimate change to the value.
  The gate is naming a failure the assertion would catch besides "someone changed the value";
  none means it is a second copy of a decided fact
  — cut it.
  Cover a value through the behavior it drives
  — the branch a default selects, the effect a config entry has
  — and cover checked-in data structurally (it parses, entries are well-formed, references resolve), never by restating entries;
  a data-only change then needs no new test.
  When the claim is that a declared value reaches a surface, compare the surface against the imported declaration (`assert timeout == DEFAULT_TIMEOUT`), not a retyped copy;
  when the claim is that assembly merges sources, assert one representative member per source, not the transcribed roster;
  when the claim is that a surface selected the right file or branch, one copied phrase is the marker
  — chosen to discriminate the fork, not a transcript of the content.
  A full roster or a copied literal stays right in two places:
  where real machinery between declaration and observation
  — entry points, a built wheel, a spawned process
  — can silently drop or admit members,
  and where the value is an external contract something outside the repository reads as-is
  — an API's required headers, a persisted disk layout, an env var another process consumes
  — so referencing the symbol would let a breaking rename slip through;
  a pinning test says which consumer it pins, in its name or in one comment.

- Diagnose before you patch.
  State the upstream cause of a failure before reaching for a workaround
  — a patch you can't trace to a root cause is a guess, and a workaround over one you do understand is debt to flag, not hide.

- In a managed ride session, treat bare commands as pinned session commands, not repository commands.
  Run repository tools through `uv run <command>` or an explicit `.venv/bin/<command>` path;
  do not activate the workspace venv over the session `PATH`.

- Run tests, type checkers, and formatters before declaring work done
  — if the repo has them.
