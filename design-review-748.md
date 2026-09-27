# Design review: credentials — pass rights (#748)

**Design review only — never to be merged.**
This file is a throwaway copy of the `## Design` section of [#748](https://github.com/dzhioev/bro/issues/748), put on a branch so it can be reviewed as a document.
The task page is the single source of truth:
once the review settles, the design is written back there and this pull request is closed unmerged.
`## Goal` and `## Problem` below are context from the task page, not under review.

## Goal

An owner chooses, per launch, which instance of a credential kind a child mission holds, from the instances it may pass to missions of that type, without holding their material itself.

## Problem

A summon request carries no credentials (#767, rule 6):
a child's credentials come only from its own bro, harness, model and configuration.
So a child's instance of a kind is fixed per bro and project in the host config, and its owner has no say in it;
a credential one run needs goes to every run of that bro on the project, as the lead's verify phase gets its own.
A worker container holds no credentials at all.

Cases this rules out:

1. A lead that summons `@bro-dev` with different `github` instances depending on the project a dev works on.
2. A browser bro that opens webviews with different logged-in profiles (`browser+alice`, `browser+bob`) for different users, without holding any profile's cookies itself.

## Design

Settled with the user on 2026-09-26 in #748's design session (trail `01m3fmsz3s-zej85rq3-jsmrj4mk`), on #767's credential rules and #768's permission document as they stand on `master`;
then reviewed against the code and revised with the user in #748's plan phase (trail `01m3g1k55f-aq58ra2w-nb1fqd09`).

Terms:

- to *pass* an instance is to have a mission its owner launches hold it, while the owner doesn't;
- a *pass right* is a member of the `pass` field of one mission type's payload in the owner's `launch` section, `:launch.<type>.pass.<kind>+<instance>`:
  it lets the owner pass that instance to missions of that type.

Rules:

1. **The `pass` field.**
   A worker type whose missions can hold credentials declares a `pass` field in its payload schema, a set of credential names:
   each `kind+instance` with a registered kind, the empty instance written `kind+`.
   The framework fixes the field's name and meaning for every type that declares it, as it fixes the shape of `creds.use`;
   it is a third field kind beside #768's sets and flags.
   This task declares it for the bro type;
   #733 declares it for the webview type, with the delivery into a webview's container.
2. **Spelling.**
   A pass right is spelled `:launch.<type>.pass.<name>`, with `<name>` in the `creds` list grammar:
   `:launch.bro.pass.github+proj-a`, `:launch.bro.pass.claude_code+team`, `:launch.bro.pass.github+`.
   Every other segment keeps `[a-z][a-z0-9-]*`;
   a credential name holds no `.`, so the name is always the fourth segment.
   A value naming no instance, such as `:launch.bro.pass.github`, fails, naming `github+<instance>` and `github+`.
   `:creds.…` stays refused.
   Reading `~/.bro.json` checks a pass right's spelling alone;
   whether its kind is registered is checked by the launches whose layers name it, as #767 checks credential names (its rule 4).
3. **Layers.**
   A pass right folds like any `launch` name (#768, rule 7):
   host `defaults`, the project and bro entries, launch flags, resume flags and the summon request grant and revoke it, and the last word wins.
   No seed or persona holds one.
   The repository's `[tool.bro]` refuses it in `grant` and `revoke` alike, since the repository names no instance (#767, rule 3).
   A summon request may grant only a pass right the summoner holds, as for any `launch` member;
   a child holds only the pass rights its own configuration and its summon request give it, never its summoner's by inheritance.
   A request's grants are checked against the summoner's section before the child's section is folded:
   a pass right the summoner doesn't hold is denied before anything looks up its kind or its presence,
   so a denial never tells the summoner whether an instance it may not pass exists.
4. **Presence.**
   Every pass right a launch holds must name an instance present in the store the launch reads, judged by name alone as #767 judges presence.
   It is checked where the `launch` section is folded, which knows the layer or flag that granted each name:
   a root launch or a resume holding an absent one fails, and a summon whose child's section would hold one is denied, each naming that layer, the summon request included.
   A section the host fixed is not checked again when its session starts
   — a summoned child's, and a manual child's at its user's launch:
   a pass right loads nothing, and a pass of an instance removed since then fails the receiving launch's own load (rule 6).
   `ride scope` lists each pass right as `PRESENT` or `MISSING` rather than failing.
5. **A pass.**
   A launch request may carry `pass`, a list of credential names in the `creds` list grammar, at most one per kind.
   The host checks it before the type's own check, the one payload field it interprets:
   each name must be a pass right the owner holds for the request's type, or the request is denied, naming the names beyond it.
   The type's run delivers the passes, and the host refuses a pass on a run that has no store to deliver it to:
   a job, and a container until #733 gives a container one.
   The launch audit records each request's passes by name.
   `summon` takes them as `--pass <kind>+<instance>`, repeatable, and the `summon` tool as `passes`:
   a tool's parameters are its function's, and `pass` is a Python keyword.
6. **What a summoned bro holds.**
   A request's passes become the child's credential launch flags, `--grant <kind> --cred <kind>+<instance>` for each:
   the child holds each passed kind at the passed instance, whether its configuration holds that kind, picks another instance of it, or revokes it.
   It loads the name as any picked one (#767, rule 5):
   the instance must be present and load, its install hook runs, and the targets its kept `$cred` references reach ship with it, though the owner holds no pass right for them.
   Like any pick, it is the child's pick of that kind everywhere, so the child's other credentials whose `$cred` references name the kind read the passed instance too (#767, rule 3).
   Holding is the whole effect, so a passed instance reaches the child however held credentials do, and never through the owner:
   today the host hydrates it from the host store into the child's own store.
   A joined member's store sits where its summoner can read it, in their shared container or, unboxed, on the host;
   so a join doesn't keep a passed instance from its summoner, just as an unboxed session is a convenience rather than a boundary.
7. **Manual summons.**
   A manual request's passes ride in the pending record, checked when the host accepts the request.
   At the user's launch, the user's own launch flags merge over the passes' flags on the user's authority, the way a resume's flags merge over a recorded launch (#767, rule 7):
   a `--grant` or `--revoke` of a passed kind replaces the pass's grant, a `--cred` replaces its pick, and a `--revoke` drops its pick as well.
   The child's pass rights are part of its `launch` section, fixed at acceptance with the rest of it.
8. **Resume.**
   A summoned child's resume record carries its credential launch flags
   — the passes' flags, with a manual child's own merged over them (rule 7)
   — beside the request's `launch` grants it records today;
   a resume replays as a root launch, as it does now.
9. **Visibility.**
   Pass rights are `launch` names:
   the broker keeps them in each peer's `launch` section and enforces them, `RIDE_LAUNCH` carries them, and the banner's `permits` row and `ride scope` render them.
10. **Credential grants in a summon.**
    A summon request still refuses a credential kind in `grant` or `revoke` (#767, rule 6);
    the error names `pass` beside the host config's entry for the child.

In set terms, over #767's and #768's:

```
passable(x, T) = { (k, i) | :launch.T.pass.k+i ∈ names(x) }, when :launch.T ∈ names(x)
every (k, i) of every passable(x, T) is present, or x's launch fails
a request r of type T by owner o:
  r.pass ⊆ passable(o, T), at most one instance per kind
a bro child c of r:
  flags(c) = { --grant k, --cred k+i | (k, i) ∈ r.pass }, a manual child's own flags merged over them
  kinds(c), pick(k) = #767's, with flags(c) as c's launch flags
```

### Rollout

The change is additive:
every file and record an older version wrote stays readable by the new one.
What a version other than the writer's reads:

- `~/.bro.json`, read whole by every installation on every machine that shares the file, whenever it launches, or resolves a credential outside a session:
  each checkout of this repository, each checkout of a repository that pins the framework (ppp and kap), each `uv tool` installation of it, and the frozen or materialized runtime of every live root, whose broker reads the file at each summon.
  A pass right is the one name that doesn't cross versions:
  an older installation refuses `:launch.<type>.pass.…` wherever it reads one, so a pass right in the file fails every launch and every host credential read of that installation.
  The benchmark's trial bundle reads the host store under `BRO_STORE`, never this file.
- The resume record.
  A pass right among its grants doesn't resume under an older version;
  its passes are recorded as `--grant` and `--cred`, which every version since #767 reads.
  A record naming a materialized runtime re-executes into that runtime to resume, so once a pass right is in `~/.bro.json`, a kept workspace recorded under an older materialized runtime doesn't resume.
- Nothing else crosses versions.
  The summon request, the pending manual record with its new `pass` key, the peer facts and `RIDE_LAUNCH` stay inside one party, which runs its root's runtime for its whole life;
  a manual child's launch re-executes into the runtime its token names before it reads the record.
  A root started before the upgrade takes no pass:
  its broker denies a request carrying `pass` as an unknown field.

The order:

1. Land the change.
2. Upgrade the host's checkout of this repository.
   A root started from it afterwards takes pass rights on its launch flags at once;
   roots already running keep their runtime and are unaffected.
   A phase verifying this change launches from a root started after this step, holding the pass rights it exercises on its launch flags.

Writing a pass right into `~/.bro.json` is no step of this rollout.
Before one goes in, upgrade every installation that reads the file, on every machine that shares it through dotfiles:
each checkout of this repository, pulled and set up;
each `uv tool` installation;
and each checkout of ppp and kap, pulled and set up once `[[bump bro]]` has moved its pin past the landing, since the bump syncs only its own managed workspace.
Then end every root started from an older runtime, and finish every kept workspace recorded under an older materialized runtime.

Rolling back takes every pass right out of `~/.bro.json` and out of every launch command, then downgrades the checkout;
a workspace recorded with a pass right among its grants doesn't resume under the older version.

### Rejected alternatives

- Bounding a pass by what the owner holds or may pass:
  once a session can use a credential it can't see, as through an authentication proxy outside its container, holding no longer implies the power to hand an instance on;
  and the broker would have to learn every session's `creds.use`, a manual child's from another process.
- One `creds.pass` set for every mission type, with a pass only picking among kinds the mission already holds:
  it keeps browser profiles away from bros only through the kinds a receiver declares.
  It costs three things:
  a kind must be granted to every run of a bro before one run can be passed it;
  worker containers need a "held only when passed" declaration;
  and a rejection surfaces only after the host accepts the request.
- Spelling a pass right as a pure path, `:launch.bro.pass.github.proj-a`:
  the path has no spelling for the empty instance, and kinds such as `claude_code` would widen the segment grammar for every name.
- Wildcard pass rights (`github+*`): they would let a session pass any stored instance, the user's own included.
- Inheriting the summoner's pass rights: #768 never inherits `launch`, and an explicit grant keeps each child at the least authority.
- Refusing passes to a joined member, whose store sits where its summoner can read it:
  the user chose to document that a join doesn't keep a passed instance from its summoner, as unboxed sessions are documented.
- A root `--pass` flag: `--grant <kind> --cred <kind>+<instance>` already says it.
- Scoping a pass right to one target bro: neither case needs it, and `launch.bro.bros` already bounds whom a session summons.
- A pass layer of its own between a manual child's configuration and its user's flags:
  a user's `--revoke` of a passed kind would leave the pass's pick naming an unheld kind, which needs a rule of its own, while the resume merge already says how a later flag replaces an earlier one.
- Writing pass rights into `~/.bro.json` as part of this rollout:
  it would first need ppp's and kap's bumps and every other installation upgraded, and verifying the change needs only pass rights on launch flags.
