# Design review: permissions — one per-type permission document (#768)

This file is a design review only and is never merged.
It carries the page of [#768](https://github.com/dzhioev/bro/issues/768) as this review found it:
`## Design` is what the review settles, and it lands back on the page once approved;
`## Goal`, `## Problem`, and `## What changes in practice` are the page's own, carried for context.
The page's `## Implementation sketch` is settled in the plan phase that follows this review, so it is left out.

## Goal

A session's permissions are one document of named sections.
`launch` is keyed by mission type:
holding a type's key lets the session launch missions of that type, and the key's payload
— a small JSON object only that type interprets
— says what those launches may be.
`creds.use` names, per credential kind, the set of instances the session's store holds.
Which instance a kind reads by default is a pick kept in the store itself, on the host and in a session alike.

## Problem

- Launch authority is split between the summon allow-list (`@bro`, published as `RIDE_MAY_SUMMON`) and flat permit strings (`:bro.party.start.boxed`, `:webview.vnc`).
- Nothing gates starting a mission of a type in general:
  any session may open webviews, while the benchmark type hard-codes "only the session root may start benchmark jobs" (#522).
- The strict no-op rule denies a summon whose `@bro` grant the child already carries (#681).
- A name like `:bro.party.boxed` reads as a property of the bro holding it.

## Design

Settled with the user on 2026-09-24 in the #748 design session (trail `01m365awd9-3ahkep1e-ae0ccgy2`);
revised with the user on 2026-09-26 (trail `01m3dfv5a6-0exeeh6n-qkbewnck`) into sections, with credentials under `creds`;
then reviewed against the code and revised with the user in #768's plan phase (trail `01m3dmmncy-8n8jv5sv-1dqp4j30`).

1. **The document.**
   A session holds `D`, a map of sections:

   ```json
   {
     "launch": {
       "bro":       {"bros": ["bro-dev", "researcher", "bro-eyebro"], "party": ["boxed", "join"]},
       "webview":   {"vnc": true},
       "benchmark": {}
     },
     "creds": {
       "use": {"github": ["dev", "bot"], "trails": [""]}
     }
   }
   ```

   Both sections are always present, and `creds` always carries `use`;
   an unknown section or key fails, wherever it's named.
   `launch` maps a mission type to its payload.
   `creds.use` maps a credential kind to the set of its instances the session's store holds, the empty instance spelled `""`.
2. **The launch check.**
   The host lets a session launch a mission of type T only when T is a key of `D.launch`;
   type T's own check then decides with `D.launch[T]`.
   The host never interprets a payload.
3. **Schemas.**
   Each worker type declares its payload's fields, each of one kind:
   - a set of strings, whose values the type validates:
     grant is union, revoke is difference, and "the summoner has it" is subset;
   - a flag:
     grant sets it, revoke clears it, and "the summoner has it" is implication.

   The framework fixes the shape of `creds.use`:
   registered kinds, each a non-empty set of instances.
   No field of the document is ordered or single-valued.
   An unknown type, field or value fails every launch whose layers name it.
   Reading `~/.bro.json` checks only what holds for every installation, as #767 does for credential names (its rule 4):
   each name's grammar and section, and the retired permits;
   so a type one installation doesn't install fails only the launches that name it.
4. **Names.**
   Config lists, launch flags and summon requests grant and revoke `launch` by name, each name a path:
   `:launch.<type>` is a key (may launch that type), `:launch.<type>.<field>.<value>` a set member, `:launch.<type>.<flag>` a flag.
   Each name stands alone:
   granting a member or a flag doesn't grant its key, and revoking a key leaves the names beneath it in place.
   `D.launch` holds a type while its key is held, with the payload the names beneath the key make.
   A bare section, `:launch` or `:creds`, is malformed.
   Two shorthands stand for names:
   `@<bro>` is `:launch.bro.bros.<bro>`, one name under either spelling, and a persona's `may_summon` declares the same members;
   a bare credential kind in `grant` or `revoke` stands for its `creds.use` entry, folded by #767's rules.
   `:creds.…` is refused, naming `creds` / `--cred` and `grant <kind>`:
   credentials keep their spelling, and the path grammar cannot yet spell a kind with `_` (`claude_code`) or the empty instance.
   The issue that makes credentials spellable by path, such as lending (#748), extends the grammar for both.
   A retired permit fails wherever it's named, and its error names the replacement:
   `:bro.party.start.boxed`, `:bro.party.start.unboxed`, `:bro.party.join` and `:webview.vnc` become `:launch.bro.party.boxed`, `:launch.bro.party.unboxed`, `:launch.bro.party.join` and `:launch.webview.vnc`.
5. **The types.**
   - bro:
     `bros`, a set of registered bros, and `party`, a subset of `boxed`, `unboxed` and `join`.
     A summon needs its target in `bros` and its placement in `party`;
     an unmarked request starts boxed when permitted, else unboxed;
     a manual summon needs `boxed` or `unboxed`.
   - webview:
     `vnc`, a flag an open with the VNC view needs.
     A webview's own `launch` is empty, so the explicit "a webview cannot open another webview" rule goes.
   - benchmark:
     no fields;
     its hard-coded root-only rule goes, because the key is the permission.
6. **Defaults.**
   No session holds anything in `launch` by default, roots included, except the framework's seed, `:launch.bro` and `:launch.bro.party.boxed`, and the `bros` members its persona's `may_summon` declares.
   A persona declares nothing else in `launch`;
   any other name reaches a session through configuration, launch flags and summon requests alone.
   `creds.use` starts from the session's needs (#767, rule 1).
7. **Layers and delegation of `launch`.**
   The seed and the persona's `may_summon`, then the repository's `[tool.bro]`, host `defaults`, the host project and bro entries, the launch flags, and last the summon request, fold name by name the way #767 folds credential kinds:
   the last word wins, repeating a grant or revoke is harmless, revoking an absent name does nothing, and one layer granting and revoking the same name fails;
   an unknown name fails.
   A resume's `--grant` or `--revoke` replaces the recorded one for the same name, and restating one is harmless.
   A summon request may grant only what the summoner's own `D.launch` holds:
   a key the summoner holds, or a member or flag it holds under a key it holds;
   its revokes are unchecked.
   A manual child's `launch` is fixed when the host accepts the request, and its own launch refuses `@` and `:launch` overrides.
8. **`creds.use`.**
   The launch computes it by #767's rules and hydrates exactly it:
   it is #767's `shipped(x)` grouped by kind
   — each loaded kind at its pick, plus every name their kept `$cred` references reach, transitively.
   Every name in it must be present in the store the launch reads and load, or the launch fails.
   A kept reference may name an instance;
   its target ships under its own name.
   A summon request carries no credentials (#767, rule 6), so a child's `creds.use` is computed as for a root launch of its bro, not bounded by the summoner's.
9. **Default picks.**
   A kind's default instance is a pick, never a permission, and lives in the store it applies to.
   A store's `creds.json` carries its picks and its typed sources:

   ```json
   {
     "defaults": ["github+dev", "trails+write"],
     "sources": {
       "github+reviewer": {"type": "github_app"},
       "openai": {"type": "ssm", "parameter": "/bro/openai", "region": "eu-west-1"}
     }
   }
   ```

   `defaults` takes the `creds` list grammar, one entry per kind, every kind registered;
   an unregistered one fails every read by that installation.
   `sources` keeps today's per-name annotations, skipping entries of kinds the installation doesn't register.
   Both keys are optional.
   A `Store` layers its caller's picks over its own `defaults`, so a reader that picks nothing reads the store's defaults
   — the benchmark's trial store takes the host's pick of its LLM kind rather than the kind's empty instance.
   On the host, the store's `defaults` replaces `~/.bro.json`'s `defaults.creds` as the base every pick layers over:
   the project URL, project path, bro URL and bro path entries, then `--cred`, for a launch;
   `user`, then `user.tools.<command>`, for the operator's own commands.
   An ambient read in a process `BRO_STORE` directs reads only that store's `creds.json`, never `~/.bro.json`;
   a launch reads `~/.bro.json`'s project and bro entries whichever store it reads.
10. **The session store.**
    It holds each name of `creds.use` under its own stored name, `creds/<kind>+<instance>.cred`, the empty instance as `creds/<kind>.cred`.
    Its `creds.json` `sources` annotates each minting name, and its `defaults` records the launch's pick of every loaded kind and of every kind a kept reference names by kind, each one of that kind's `creds.use` instances.
    Install hooks run for the loaded kinds alone (`BRO_INSTALL_KINDS`), each wired to its default;
    a kind-addressed read in the session therefore reads the instance it reads today.
11. **Where the sections go.**
    The host computes `D` once per launch.
    `launch` goes to the broker, which checks launches and summon grants against it, and to the session's environment:
    `RIDE_PERMITS` carries it as compact JSON, and `RIDE_MAY_SUMMON` goes, since `summon.may_summon()` and the `#may_summon` fact read `launch.bro.bros` from it.
    The banner's `permits` row renders `launch` as names in the grant spelling, leaves only:
    set members, flags, and each key whose payload is empty.
    The `bro.bros` members stay in its `may_summon` row, as today, rather than under `permits`.
    `ride scope` prints the `launch` section a launch would hold, rendered the banner's way.
    `creds` reaches the session only as its store, and the banner doesn't show it.
12. **Reading a permit.**
    Its first segment names the section.
    Under `launch`, the second is the type of mission it lets the holder launch, and what follows describes those missions, never the holder.

In set terms, over #767's:

```
names(x)    = the seed ∪ may_summon(x), then each layer's grants and revokes, name by name
launch(x)   = { T: the names of names(x) beneath :launch.T | :launch.T ∈ names(x) }
use(x)      = loaded(x) ∪ the names their kept references reach   (#767's shipped(x))
defaults(x) = { (k, pick(k)) | k a kind of loaded(x), or named by a kept kind reference }
hooks(x)    = the kinds of loaded(x)
```

Rollout:

The change ships as a flag day like #767's, every installation at once:
mixed versions aren't kept working across it, and the order below stands in for them.
What a version other than the writer's reads:

- `~/.bro/creds.json` and `~/.bro.json`, read whole by every installation on the host:
  each checkout of this repository, each repository that pins the framework (ppp and kap), and the frozen runtime of every live root.
  Neither shape stays readable across the change, in either direction:
  an older installation fails on the migrated store, whose `defaults` and `sources` it reads as malformed annotations;
  a newer one fails on either old shape, naming the new place.
  One pairing fails silently:
  an older installation reading a migrated `~/.bro.json` beside a store it can still read loses its `defaults.creds` picks without an error, so every machine sharing one `~/.bro.json` through dotfiles takes the flag day together.
- The resume record, which `ride resume` loads with whichever installation runs it:
  a record granting a retired permit doesn't resume under the newer version, and one granting a `:launch.…` name doesn't resume under the older one.
- Nothing else crosses versions.
  The summon request, the pending manual record, the peer facts, `RIDE_PERMITS` and the session store stay inside one party, which runs its root's frozen runtime for its whole life;
  so a root started before the upgrade keeps the old rules for every summon it makes, until the host files migrate and its summons fail on them.
  The trails server reads a store baked into its image beside the code that reads it:
  `oops/trails/server/creds.json` moves to `sources` in the same change.

The order:

1. Once the landing merges, deploy the trails server from the landed commit, recording its running task definition and `latest` image first;
   `verify.sh` passing is the check, since the server reads both its credentials through the migrated store at startup, and no CI check reads that file.
   Its rollback points the service at the recorded task definition and retags `latest` to the previous image (#796).
2. Move ppp's and kap's pins past the landing with `[[bump bro]]`, adapting any retired permit they name;
   their checkouts sync in step 4, with every other installation.
3. End every running session, finishing first any kept workspace that is to be resumed.
4. Upgrade every installation that reads `~/.bro.json` or `~/.bro`, keeping a copy of both files.
5. Migrate both files where the upgraded launcher's errors point:
   the store's annotations move under `sources`, `~/.bro.json`'s `defaults.creds` becomes the store's `defaults`, and retired permits take their `:launch.…` names;
   `:launch.webview` and `:launch.benchmark` are granted where sessions open webviews or start benchmarks, since no session holds either by default.

Rolling back is the same flag day in reverse:
end every session, downgrade every installation, ppp's and kap's pins included, and restore both copies;
a workspace recorded after the upgrade doesn't resume under the older version.
The trails server rolls back on its own, as step 1 says.
A phase verifying this change launches from a root started after the upgrade.

An expand-and-contract rollout was rejected:
a first landing reading both store shapes and a second refusing the old one would keep older installations working through a transition, at the cost of code reading both shapes and a gate over the same installations the flag day covers.

## What changes in practice

- Every permit gains its section:
  `:bro.party.start.boxed` and `:bro.party.start.unboxed` become `:launch.bro.party.boxed` and `:launch.bro.party.unboxed`;
  `:bro.party.join` becomes `:launch.bro.party.join`, and `:webview.vnc` becomes `:launch.webview.vnc`.
  Host config, `[tool.bro]` entries and recorded resume specs using the old names fail as unknown.
- Opening a webview needs `:launch.webview`, and starting a benchmark needs `:launch.benchmark`;
  no session holds either by default, roots included.
- A redundant `@bro` grant stops failing (#681).
- A session can pass a child the right to open webviews or start benchmarks, `summon … --grant :launch.webview`, bounded like `@bro` (#522).
- A webview's own `launch` is empty, so it can launch nothing, and the explicit "a webview cannot open another webview" rule goes.
- `defaults.creds` moves from `~/.bro.json` to `defaults` in `~/.bro/creds.json`, and that file's typed-source entries move under `sources`;
  either old shape fails, naming its new place.
  Every other credential key and flag keeps its spelling and meaning:
  `creds` in project and bro entries, `user`, `user.tools`, `grant`, `revoke`, `--cred`.
- A kept `$cred` reference naming an instance no longer fails a launch.
- Inside a session, the store holds instances under their own names:
  `credentials list --instance` shows `github+dev` where it showed `github`, and `credentials get --instance github+dev` resolves.
  Kind-addressed reads and install hooks read what they read today, and the banner shows no credentials, as before.
