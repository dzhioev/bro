# Framework checkout integration

Checkout-specific personas, policy tests, and the repository test gate.
The workspace member publishes `bro-local`, contributing `bro.local` plus the `bro-dev` and `bro-eyebro` persona declarations used to work on this repository.
Nothing here belongs in a consumer installation.

## Development

This member depends on core and the generic development distribution.
Formatting, linting, typing, tests, and packaging use the root repository gate.
Run `sync-scripts --project local` after adding or removing a CLI, and build the wheel with `uv build --package bro-local`.

## Components

- `bros/` — this checkout's developer and reviewer persona declarations.
- `bro/local/prompts.py` — framework-project context shared by those personas.
- `bro/local/run_tests.py` (`run-tests`) — the checkout-wide staged test gate and explicit test rosters.
- `bro/local/*_policy_test.py` — repository-wide policy assertions that have no consumer-neutral package owner.
