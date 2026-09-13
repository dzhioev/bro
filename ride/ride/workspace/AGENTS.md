# Managed workspace machinery

`ride.workspace` is the containerization and workspace layer beneath every managed launch.
A workspace is `<runtime-base>/workspaces/<name>/`:
a globally named tree plus its recorded optional repository attachment, lock, exit record, resume record, and host log.

## Design

- **Harness-neutral.**
  Workspace modules know no Claude or native-harness policy.
  Launch surfaces supply commands, environment, mounts, and credential tiers as plain launch data.
  `ride.session.started_party_launch` prepares either isolation before supervision starts `do-ride`, which owns the per-session setup.
- **Isolation is recorded.**
  `workspace.json` fixes boxed or unboxed isolation at creation.
  Every attached tree is an independent clone.
  A detached unboxed workspace may record an externally owned tree;
  only legacy cleanup knows how to release a linked worktree.
- **Core contracts point downward.**
  Runtime paths, project configuration, and git helpers remain in `bro.workspace`;
  this package imports those contracts, `bro.base`, and the broker interfaces.
  The framework never imports `ride`.
- **Lazy broker import.**
  `spawn.py` is imported only after the `containers.broker_enabled` gate.
  `BROKER_DISABLED`, and an environment that cannot import the broker, must short-circuit first.
- **No re-export hub.**
  Consumers and package code import submodules directly.

## Components

- `build_context.py` — normalized, separate runtime and project-image contexts;
  path attachments read the working tree, URL attachments the resolved commit
- `docker.py` — runtime/project image hashing and builds, plus lazy per-ride container-runtime resolution with the daemon mount preflight;
  broker-free launch descriptions, root-bounded bind validation, container creation, scoped-store copy, member-exec preparation and checked in-container kills,
  attach suspension, Docker inspection, and the bridge gateway a container reaches its launcher through
- `metadata.py` — strict `workspace.json` records, boxed/unboxed isolation, and optional external-tree identity
- `model.py` — workspace factories, namespace/session locking, external-tree ownership, inspection, clean-exit records, and isolation-specific teardown
- `worktrees.py` — the surviving unboxed `setup.sh` runner
- `clones.py` — clone creation for every attached tree, upstream retargeting, base checkout, and submodule initialization
- `containers.py` — boxed execution and attachment plus the broker availability gate
- `spawn.py` — boxed, unboxed-process, and member-exec broker spawner adapters, bounded child output, process-group and record-checked exec kills,
  the shared party-member registry, throwaway-workspace or joined-member record teardown, a boxed member's local-trail adoption into the ride's host store, private-credential teardown, terminal ownership, and launcher-log redirection
- `store.py` — scoped credential tiers, override finalization, directory materialization, and container tar packing
- `launch_smoke_test.py` — host-only cold-image launch check, run by the gate's Docker stage
- `host_docker_test_helper.py` — the checkout to build from, a throwaway root the daemon can bind-mount, the host's daemon endpoint, and the host-only skips
  — what a test driving the real docker daemon needs from the host it runs on
