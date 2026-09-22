"""prompt text shared by this checkout's personas."""

FRAMEWORK_PROJECT = """\
## Bro framework project

You are operating inside the bro framework repository. Read the root and
relevant subsystem `AGENTS.md` files before working on the code, unless they
are already in your context; they carry the repository's non-obvious
development rules.

Text assets may carry `{{…}}` conditioning directives. Read the `template` man
page when their exact grammar or rendering semantics matter.
"""
