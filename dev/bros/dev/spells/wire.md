---
name: wire
description:

Use this spell to wire the host's credentials for the repository you are working in
— deciding which stored instance of each credential kind this project's sessions read, and recording it in the host's `~/.bro.json`.
Trigger on "[[wire credentials]]", "configure credentials for this repo", "which github token does this project use", "my sessions pick the wrong task tracker",
or a launch that failed on a missing or wrong credential.
It runs on the host (a container session cannot reach `~/.bro.json`), changes no repository file, and never invents a secret
— a kind with nothing behind it is reported for the user to provide.

version: 1.1.0
---

# Wire this project's credentials

A host serves several projects, and its store may hold several instances of one credential kind — two GitHub identities, a task tracker per backend. Which instance a project's sessions read is recorded per project in `~/.bro.json`; this spell decides that mapping with the user and writes it.

## 1. Check you can reach the host config

`~/.bro.json` is the host's file. Call `bro::banner`: a session reporting `kind: container` cannot see it, so stop there and tell the user to re-run on the host — in a `--host` session, or from their own terminal. `kind: worktree` is on the host and fine.

## 2. Learn what the project needs

Resolve the project root (`git rev-parse --git-common-dir`, then its parent — every linked worktree maps to its main checkout) and read the repo's `[tool.bro] default` bro from its `pyproject.toml`. That bro is what sessions launched here run as.

The mapping keys on the attachment a session names the project by, and a session may name it either way: the checkout path, or the git URL `ride ... --repo <url>` attaches. Ask the user which their sessions use — `ride list` shows what the existing ones were attached by — and record every identity in use; an attachment with no entry reads nothing this host scopes per project.

`bro show <bro>` lists the credential kinds it needs, its best-effort tier, and its features. `ride scope --repo <root>` states the same thing as an attached launch sees it: every kind a session from this project would hydrate, the instance each reads today, and whether it resolves.

## 3. See what the host holds

`credentials list --instance` prints the entries that resolve, variants included; the registry is `~/.bro/registry.json` plus the framework's built-ins (`bro/setup/AGENTS.md`, "Configuration"). For each kind the project needs, sort the host into one of three cases:

- **one instance, resolving** — nothing to decide; leave it alone.
- **several instances** — the project has a real choice. Show the user the candidates with whatever distinguishes them (a config's `backend`, an account or repo field — never the secret material itself) and ask which this project should read.
- **nothing behind the kind** — report it. Say what the kind is for and what its config looks like, and let the user create it; never write a secret yourself, and never silently pick a neighbouring instance.

## 4. Record the decision

Merge only this project's entries into `~/.bro.json`, leaving every other project's untouched, and show the user the change before writing it. One project attached both ways carries one entry per identity, each with the same selection. The file's schema — and what `kind+` with no instance after it means — is the module docstring of `bro/base/host_config.py` in the framework's own sources; read it before writing.

Kinds the project has no opinion about stay out of the entry.

## 5. Verify

Re-run `ride scope --repo <attachment>` for each identity you recorded: every kind the project selects should now name that instance and read `ok`. A kind reported `MISSING` is a selection pointing at an entry the store cannot resolve; one reported `REFUSED` is a kind this host reads per project that the attachment you passed has no entry for. Fix either before finishing rather than leaving the user to meet it at their next launch.

Close by telling the user what changed and what a session from this project now reads. A launch can still override any of it for one session with `--grant <kind>+<instance>`.
