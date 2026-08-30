"""prompt text shared by this checkout's personas."""

FRAMEWORK_PROJECT = """\
## Bro framework project

You are operating inside the bro framework repository. Read the root and
relevant subsystem `AGENTS.md` files before working on the code; they carry
the repository's non-obvious development rules.

The framework is in early beta: much of what you find is a first approximation —
drafts, experiments, provisional structure — and none of it is fixed in stone.
There are no external consumers to protect, so backward compatibility is not a
design constraint. When the right design means breaking an interface, moving a
mechanism between distributions, or deleting a half-finished idea, propose
exactly that. Never trim a solution to avoid disturbing what is already there,
and never offer "nothing existing changes" as a merit of a proposal — the merit
is the structure left behind.

Text assets may carry `{{…}}` conditioning directives. Read the `template` man
page when their exact grammar or rendering semantics matter.

Never stage credential stores or synthesized secret directories. Framework code
must stay consumer-neutral: extension packages contribute personas, credentials,
task backends, and toolsets through the documented entry-point groups.
"""
