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
   A `$cred` reference inside a loaded value resolves its target through the same picks, whether or not the session holds the target's kind.
4. **Known names.**
   Every name in a layer that applies to a launch must be registered, `defaults` included.
   Reading `~/.bro.json` checks only what holds for every installation, each name's spelling and each entry's shape, so an entry naming a kind this installation doesn't register fails only the launches it applies to.
5. **Loading.**
   Every held name must load, and any failure while loading fails the launch:
   an unreachable SSM parameter, a minting error, a malformed file.
   An absent name fails too, with one exception:
   an optional kind that no layer picked, whose empty instance isn't present, is skipped.
   A picked instance must be present even when its kind is optional.
   Grants make nothing required:
   a held kind is optional exactly when every source that needs it marks it optional.
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
skipped(x) = { (k, ε) | k an optional need, unpicked, (k, ε) not present }
loaded(x)  = held(x) − skipped(x)                          each loads, or the launch fails
```

Rollout:

Three contracts meet more than one framework version.

- `~/.bro.json`, which every installation on the host parses whole:
  this checkout's, the frozen runtime of every root still running, whose broker re-reads the file at each summon, and those of other repositories pinning an older framework.
  An older reader refuses an entry that names one kind in both `creds` and `grant`, and this change refuses a grant naming an instance.
- The resume record, which `ride resume` loads with whichever installation runs it.
  Its session spec gains the recorded `--cred` values, and a record loads only when its fields match exactly, so a workspace recorded before this change doesn't resume under it.
- The summon request, the pending manual record, the child's session spec and the scoped store, which never meet a mixed version:
  a party runs its root's frozen runtime for its whole life, and a manual child re-executes into its token's runtime.
  So a party whose root started before the upgrade keeps the old rules for every summon it makes, and a phase verifying this change launches from an upgraded installation.

The order:

1. Rewrite each config instance grant, `"grant": ["k+i"]`, into a spelling both versions read the same way:
   `"creds": ["k+i"]` alone where the bro already needs `k`;
   otherwise the pick and a bare `"grant": ["k"]` in two entries that both apply to the launch, such as the pick in the project entry and the grant in the bro entry.
2. End each running root that passes a credential in a summon request, such as a lead in its verify phase:
   once step 3 gives that credential to the summoned bro, the old rules refuse the root's own grant of it as a no-op.
   Finish any kept workspace that is to be resumed.
3. Upgrade every installation that reads the host config, and move each credential a summon request passed into the host config's entry for the summoned bro, in the spellings of step 1.
   Roots still running on the old runtime keep working on the rewritten file.
4. Once no older installation reads the file, a pick and its grant may share one entry.

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
