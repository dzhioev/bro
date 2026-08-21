# bro-dev

`bro-dev` contains the development domain kept out of the `bro` framework core:

- repository utilities and commit-accounting workflow (`bro.dev`, `bro.workflow`)
- the `poll-pr` GitHub review watcher
- the `dev`, `lead`, `terminal`, and `analyst` personas and their spells
- the development style reference mounted by Dev

Install it in a repository's development dependency group and run `uv sync`.
Call `bro.dev.install` from the repository's `setup.sh` after activating the venv to install the commit-footer hooks and local `git golc` alias.
The package depends on `bro`;
the GitHub API client and App authentication remain available from the core's `bro[github]` extra.
