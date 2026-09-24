# Design review: credentials — simplify the scope rules (#767)

This file is a design review only and is never merged.
It carries the page of [#767](https://github.com/dzhioev/bro/issues/767) as this review found it:
`## Design` is what the review settles, and it lands back on the page once approved;
`## Goal`, `## Problem`, and `## What changes in practice` are the page's own, carried for context.
The page's `## Implementation sketch` is settled in the plan phase that follows this review, so it is left out.

## Goal

A session's credentials follow a few rules that each do one thing:
`creds` / `--cred` pick an instance, `grant` / `revoke` add or remove a kind, every layer applies them the same forgiving way, whether a credential loads is decided by its name, and a summon request carries no credentials.

## Problem

- `grant` has two jobs.
  `grant k` adds a kind;
  `grant k+i` also picks its instance, in config and on the command line, and a "`creds` or `grant`, not both" rule polices the overlap.
- Config layers apply grants and revokes forgivingly, while launch flags, resume flags and summon requests are strict, with uneven no-op checks:
  a bare grant fails if the kind is in either tier, an instance grant only if the kind is already required at that instance, a revoke if the kind is absent.
  Resume has merge rules of its own, and a redundant grant in a summon request is accepted and then fails at the child's launch.
- A summon request can grant credentials under three different bounds
  — a bare kind needs the summoner to hold the kind while the child reads its own instance;
  `k+i` needs the summoner to hold that instance;
  a harness or model choice needs the summoner to hold the kinds it adds
  — checked against a scope the host recomputes for summoned sessions and cannot know for manual ones.
- An optional kind is skipped whenever it fails to resolve, so a typo in `creds: ["trails+wirte"]` silently sends recording to local storage;
  an unknown kind in a config `creds` entry or a config revoke is ignored silently.
- A grant turns an optional kind into a required one.
- `trails` is a special case the launch recipe adds.

## Design

Settled with the user on 2026-09-24 in the #748 design session (trail `01m365awd9-3ahkep1e-ae0ccgy2`), then reviewed against the code in #767's plan phase.

Terms:

- a *kind* is a registered credential type (`github`);
- an *instance* is one stored copy of a kind, `kind+instance`;
  the empty instance is stored as `kind` and written `kind+`;
- a name is *present* when the store the launch reads (the host's `~/.bro`, or the directory `BRO_STORE` names) has a `creds/<name>.cred` file or a `creds.json` entry for it, judged by the name alone, never by content.

Rules:

1. **Needs.**
   A session's needs come from its bro, its harness (the claude login), its model (the API key on the bro harness), and recording (`trails`, unless `--no-trails`).
   Each source marks which of its needs are optional.
2. **Kinds.**
   The held kinds are the needs, plus grants, minus revokes, applied layer by layer:
   the repository's `[tool.bro]`, host `defaults`, the host project entries (URL, then path), the host bro entries (URL, then path), then the launch flags.
   Every layer applies the same way:
   the last word wins, repeating a grant or revoke is harmless, and revoking an absent kind does nothing.
   Only malformed input fails:
   an unknown name, a grant or revoke naming an instance, two picks for one kind in one layer, granting and revoking one kind in one layer.
3. **Instances.**
   `creds` in config and `--cred kind+instance` on the command line pick instances and never add a kind;
   `grant` and `revoke` take kinds only, everywhere.
   Each held kind uses the most specific pick, else its empty instance.
   The repository cannot pick.
   A bro-level `creds` entry for a kind the bro's configured scope doesn't hold fails, naming `grant`:
   the configured scope is its needs under the configuration layers, recording's need counted even under `--no-trails`, so a launch's own `--revoke` or `--no-trails` never fails a bro entry.
   A `--cred` for a kind the launch doesn't hold after its own flags fails the same way.
   `defaults` and project `creds` may name kinds a given bro doesn't use.
   A `$cred` reference to a kind resolves through the same picks, whether or not the session holds that kind, so an unheld target's pick comes from `defaults` or a project entry;
   a reference to an instance reads that instance directly.
4. **Known names.**
   Every name in a layer that applies to a launch must be registered, `defaults` included.
   Reading `~/.bro.json` checks only what holds for every installation, each name's spelling and each entry's shape, so an entry naming a kind this installation doesn't register fails only the launches it applies to.
5. **Loading.**
   Every held name must load, and any failure while loading fails the launch:
   an unreachable SSM parameter, a minting error, a malformed file.
   An absent name fails too, with one exception:
   an optional kind that no layer picked, whose empty instance isn't present, is skipped.
   A picked instance must be present even when its kind is optional.
   A held kind is optional exactly when some source needs it and every source that needs it marks it optional, so granting an optional need leaves it optional;
   any other held kind is required, a kind only a grant holds included.
   The store's own shape is checked whole, as today:
   a malformed `creds.json` or a material file under a noncanonical name fails every launch that reads the store, whichever kinds it holds.
6. **Summons.**
   A summon request carries no credential values — no grant, revoke or cred;
   its `@bro` and `:permit` values stay, for #768 to rework.
   A child's credentials come only from its own bro, harness, model and configuration, as for a root launch of that bro on that harness and model with no credential flags.
   A manual child's user may add launch flags, on their own authority.
   The check that a request's harness or model choice adds only kinds the summoner holds goes too;
   a permit for harness choice can come later if a need appears.
7. **Resume.**
   A resume's `--grant` or `--revoke` replaces the recorded one for the same kind, and its `--cred` replaces the recorded pick;
   restating one is harmless.
   A resume's `--revoke` also drops the recorded pick of that kind, which would otherwise fail as a pick of a kind the session doesn't hold.
   `@bro` and `:permit` values keep today's merge until #768 folds them the same way.

In set terms:

```
kinds(x)   = needs(x) ∪ grants − revokes                  layer by layer, last word wins
pick(k)    = the most specific creds / --cred for k, else ε
held(x)    = { (k, pick(k)) | k ∈ kinds(x) }
skipped(x) = { (k, ε) | k optional, unpicked, (k, ε) not present }
loaded(x)  = held(x) − skipped(x)                          each loads, or the launch fails
shipped(x) = loaded(x) ∪ the names their kept references reach, transitively
```

The session's store holds `shipped(x)`, each name under its kind.
A value whose `$cred` chain reaches a minting source ships with its references kept, and their targets ship with it;
a kept reference must name a kind, since the session's store is kind-addressed.
Any other reference is expanded into the value that holds it.
Install hooks apply to the kinds of `loaded(x)` alone, never to a kind only a kept reference ships.

Rollout:

The change ships as a flag day, every installation at once:
mixed versions aren't kept working.
Two formats are read by versions other than the one that wrote them, and neither stays readable across the change, in either direction:
`~/.bro.json`, which every installation on the host reads whole, and the resume record, which `ride resume` loads with whichever installation runs it
— unless the record names a materialized runtime, which `ride resume` re-executes into before loading it.
The summon request, the pending manual record, the child's session spec and the scoped store stay inside one party, which runs its root's frozen runtime for its whole life;
so a root started before the upgrade keeps the old rules for every summon it makes.

1. End every running session, finishing first any kept workspace that is to be resumed.
2. Upgrade every installation that reads `~/.bro.json`.
3. Fix `~/.bro.json` where the upgraded launcher's errors point.
   Each error names the entry and what replaces it:
   - `"grant": ["k+i"]` becomes `"creds": ["k+i"]`, plus `"grant": ["k"]` where the bro doesn't already need `k`;
   - a name some installation doesn't register moves out of `defaults` into the project entries that use it;
   - a credential a summon request used to pass moves into the host config's entry for the summoned bro.

A phase verifying this change launches from a root started after the upgrade.

## What changes in practice

- `--grant github+proj-a` becomes `--cred github+proj-a`, plus `--grant github` when the bro doesn't already need github.
  `"grant": ["github+reviewer"]` in `~/.bro.json` becomes `"creds": ["github+reviewer"], "grant": ["github"]`.
- A redundant grant or an absent revoke no longer fails a launch or a resume.
- A mistyped pick, or an unknown name in any layer that applies, fails the launch instead of being ignored.
  A `defaults` entry for a kind only some installations register moves to the project entries that use it.
- An explicitly picked optional credential that is missing, unreachable or broken fails the launch.
- `summon … --grant <credential>` and `--revoke <credential>` are refused.
  Until lending (#748) lands, a phase that needs an extra credential — the lead's verify phase — gets it from the host config's entry for its bro, which gives it to every run of that bro on the project.
- A summon's `--harness` or `--llm` no longer requires the summoner to hold the kinds that choice adds.
