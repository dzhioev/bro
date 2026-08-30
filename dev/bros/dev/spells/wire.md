---
name: wire
description:

Use this spell to wire the host's credentials for the repository you are working in
— deciding which stored instance of each credential kind this project's sessions and bros read, and recording it in the host's `~/.bro.json`.
Trigger on "[[wire credentials]]", "configure credentials for this repo", "which github token does this project use", "my sessions pick the wrong task tracker",
or a launch or host CLI that failed on a missing or wrong credential.
It runs on the host (a container session cannot reach `~/.bro.json`), changes no repository file, and never invents a secret
— a kind with nothing behind it is reported for the user to provide.

version: 2.0.0
---

# Wire credentials

A host store may hold several instances of one credential kind:
two GitHub identities,
or a task tracker per backend.
`~/.bro.json` selects among them through host-wide defaults, the user's own command layers, and project and per-bro layers.
This spell decides the relevant mapping with the user and writes it.

## 1. Check you can reach the host config

`~/.bro.json` is the host's file.
Call `bro::banner`.
A session reporting `kind: container` cannot see it, so stop there and tell the user to re-run on the host
— in a `--host` session,
or from their own terminal.
`kind: worktree` is on the host and can continue.

## 2. Identify the consumer

Determine whether the user is wiring a managed launch or a host CLI.
For a managed launch, run `git rev-parse --git-common-dir` and take its parent as the project root.
Every linked worktree maps to its main checkout through the common directory.
Read the repository's `[tool.bro] default` bro from `pyproject.toml`.
That bro is what an ordinary session launched here runs as.
Include another exact bro name when the user is wiring a distinct identity for it, such as a reviewer.

A project mapping keys on the attachment a session names the repository by.
A session may use the checkout path or attach the git URL with `ride ... --repo <url>`.
Ask which form the user's sessions use
— `ride list` shows existing sessions
— and record every identity in use.
An attachment with no project entry receives defaults alone.

For a command the user runs themselves, name it by its canonical console-script name
— its import path with the underscores dashed, such as `bro.trails.rewind`.
The owning distribution's `[project.scripts]` lists it beside the bare alias the command is usually typed as (`rewind`), which is rejected as a key.
Its `user.tools.<command>` layer never affects managed launches.

## 3. Learn what credentials are needed and stored

For a managed launch, `bro show <bro>` lists the required and best-effort credential kinds.
`ride scope --repo <root> --bro <bro>` shows what the attached launch currently resolves.
For a host CLI, inspect the command's credential reads in its module or documentation rather than guessing from neighbouring tools.

`credentials list` prints the code registry's kinds and descriptions.
`credentials list --instance` enumerates stored names.
Read the ambient store's `creds/` directory (`~/.bro/creds/` when `BRO_STORE` is unset) and `creds.json` annotations to distinguish candidates;
do not look for or edit the retired `~/.bro/registry.json`.
Never print secret material.
Use safe metadata already present in a configuration value
— a backend, account, repository, application id, or parameter name
— only where it distinguishes instances without disclosing the credential.

For each needed kind, sort the host into one of three cases:

- **one suitable instance** — leave the more-specific layer empty when defaults or the kind's empty instance already selects it correctly.
- **several instances** — show the user the non-secret distinctions and ask which this consumer should read.
- **nothing behind the kind** — report it.
  Say what the registry description says the kind is for and what store name is expected.
  Let the user provide the material;
  never write a secret or silently pick a neighbouring instance.

## 4. Choose the owning layer

Read `bro/base/host_config.py`'s module docstring before editing the file.
Its precedence is launch flag, project-bro, project, tool for a host CLI, then defaults;
a kind no layer selects reads its empty instance.
Every list is named `creds` and carries `kind+instance`, the instance left empty (`kind+`) for the kind's own `creds/<kind>.cred`.

Put a host-wide choice in `defaults.creds` only when both the user's own commands and unrelated projects should read it.
Put what the user's own commands read in `user.creds`, and one command's own choice in `user.tools.<command>.creds`.
Put a repository-wide choice in `projects.<attachment>.creds`.
Put an identity specific to one bro in `projects.<attachment>.bros.<bro>.creds`.
Kinds the consumer has no opinion about stay out of its layer.

## 5. Record the decision

Merge only the chosen entries into `~/.bro.json`.
Leave every unrelated default, user, command, project, bro, and `llm` entry untouched.
A project attached by path and URL carries one project entry per identity, with matching project and per-bro selections.
Show the user the proposed change before writing it.

## 6. Verify

For project wiring, re-run `ride scope --repo <attachment> --bro <bro>` for every attachment and bro you changed.
Each selected kind should name the intended instance and report `ok`.
A `MISSING` kind points at material the store cannot resolve;
fix it before finishing.

For command wiring, invoke the CLI through a real console script on the cheapest path that reads the credential.
Do not set `BRO_STORE` for that check:
an explicit store deliberately bypasses `~/.bro.json`.

Close by telling the user which layers changed and what each consumer now reads.
A managed launch can still override its computed choice once with `--grant <kind>+<instance>`.
