# bro-dev

`bro-dev` contains repository-development tooling kept out of the `bro` runtime distribution: console-script metadata generation, token-usage reports, shell-policy checks, repository hook installation, and the `bro-dev` development persona.

Install it in a repository's development dependency group, run `uv sync`, then call `bro-dev.install` from the repository's `setup.sh` after activating the venv. The installer copies the packaged post-commit hook and registers the local `git golc` alias.
