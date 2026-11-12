# Development domain

Development tooling for repositories built on the bro framework, distributed separately from the core.
The distribution is `bro-dev`;
it contributes portions of the `bro` and `bros` namespaces and depends on `bro`.
Core never imports it.
Nothing here is specific to this checkout
— checkout-only policy and the `bro-dev` persona live in the `bro-local` member (`local/`).

## Development

This workspace member owns `pyproject.toml`
— its distribution metadata, dependencies, console scripts, persona entry points, and wheel roots.
Formatting, linting, typing, and the test roster are the repository root's (`AGENTS.md`, "Development"), which covers this directory too;
build the wheel with `uv build --package bro-dev`.

## Components

- `bro/dev/` — reusable repository-development utilities:
  - `sync_scripts.py` discovers CLIs and regenerates a distribution's scripts table plus committed `_entrypoints.py` bridge
  - `usage_report.py` aggregates token-accounting footers over a git range
  - `install.py` installs the commit-footer hooks and the repo-local `git golc` alias
  - `git_golc.py` backs that alias with per-commit output-token credits
  - `shell_policy.py`, `packaging_policy.py`, `markdown_policy.py`, and `sleep_policy.py` expose reusable repository checks over an explicit root;
    `markdown_policy.py` also backs `check-markdown`, which holds a bulk prose reflow to whitespace and nothing else
  - `affected_tests.py` reads a diff and the import graph over a repository's source roots, answering which of its modules a change can reach
  - `sharding.py` is the `--shard K/N` pytest plugin, dealing a collection into shards by test class or standalone function
  - `references.py` declares the `dev-style` and `rebase-conflicts` sources, the `guides` pages and the `delivery` page, all over files in `bro/prompts/dev/`
- `bro/workflow/` — development delivery mechanics:
  co-author and token-accounting commit metadata,
  the read of origin's default branch,
  base-ref resolution and branch folding,
  head checks and push or PR landing,
  and the packaged git hooks.
  Its public import paths remain `bro.workflow.*`;
  its CLIs are `commit-footer`, `default-branch`, `fold-branch`, `check-head`, `land-push`, and `land-pr`
- `bro/extra/github/` — `poll_pr.py` (the `poll-pr` review watcher), `pr_state.py` (the `pr-state` read of a PR's reviews and conversation/inline comments), and their shared account trust predicate in `trust.py`.
  Both commands require `--trust-file <path>`, naming trusted GitHub logins, one per line;
  repository owners and accounts with write access also qualify.
  Untrusted feedback carries only its account, kind and URL, never its text.
  `poll-pr` rereads the file each cycle and releases held feedback once trusted;
  an unreadable file ends it with `watch_failed`.
  Its `--replay` emits existing feedback oldest first with `replayed`, then `replay_complete` with the event count before live events.
  Comments carry `in_reply_to`, comments and reviews carry `user_type`, and reviews carry the `commit_id` they judged;
  `pr-state` also carries each trusted review's association and state.
  The GitHub client, App-auth source, and pull-request reads they consume remain in core at `bro.extra.github.api`, `.app`, and `.pulls`
- `bro/prompts/dev/style.md` — the development style policy, tool-served by the Dev and Eyebro personas through `dev-style-source::read`
- `bro/prompts/dev/{concurrency,testing,markdown}.md` — the guide pages the style policy points to, declared by the same personas into their `man` manual
- `bro/prompts/dev/delivery.md` — the delivery plan and the gates that clear its landing, declared by the Dev, Eyebro and Lead personas into their `man` manual
- `bro/prompts/dev/rebase_conflicts.md` — resolving a rebase that stops on a conflict, tool-served by the Dev persona through `rebase-conflicts-source::read`
- `bro/dev/spells/` — the spells more than one development persona offers, as `SpellFile` values in the `bro.dev.spells` package:
  audit, framework bump, commit, design, task, task creation (`open-task`), reflection (`reflect`), delivery (`run-pr`, including PR resume), PR following (`follow-pr`), and landing
- `bros/dev/` — generic developer with the `files`, unrestricted `brash`, `web`, and `delegation` tool groups, and those spells (the task ones gated on its `brog` feature).
  Its optional `brog` feature mounts tracker tooling;
  its provisioning declaration installs the commit hooks
- `bros/eyebro/` — code reviewer with the `review-diff` (revision/branch → findings list) and `review` (rounds to a head-bound verdict on `github`, `talk` or `local`) spells.
  It derives from `Bro`, not `Dev`, so the author-side spells stay out of its roster.
  Its optional `github` feature is the pull-request conversation's identity:
  `review` uses it on `github` and otherwise names what is missing and how to supply it.
  The `local` channel and standalone `review-diff` need no credential
- `bros/lead/` — coordinator persona and `orchestrate` spell
- `bros/terminal/` — standalone container developer, directly derived from `BaseBro`

The `bro`, `bro.extra`, `bro.prompts`, and `bros` package trees are shared with other distributions.
Declare every shipped leaf in `[tool.uv.build-backend].module-name`;
do not build one of those parent trees wholesale.

## Wheel contents

Every member builds through uv's own backend, which ships each declared module root whole, so a `[tool.uv.build-backend] wheel-exclude` glob is what keeps a file out of the wheel
— the test modules (`*_test.py`, `*_test_helper.py`, `conftest.py`, and a Claude Code plugin's `*.test.ts`) among them,
held against the built wheels by `dev/bro/dev/packaging_policy.py` and enforced repository-wide by its sibling `packaging_policy_test.py`.
That module derives the distributions from the root's `members`, so the projects shipped from outside the workspace are named to it explicitly;
`local/` holds their names, next to the gate's other checkout-specific facts
— the modules and file-type manuals a wheel must carry among them.

## Commit metadata

Agent commits carry the token-accounting footer automatically
— the installed `commit-msg` hook appends it whenever the environment carries an agent usage source, and human commits stay footerless (`bro.workflow.commit_footer`);
an interactive session's commits carry a `Co-Authored-By` trailer for the human it works for beneath it (`bro.workflow.co_author`).
Never hand-write or strip either.
