# Managed workspace machinery

`ride.workspace` is the containerization and workspace layer beneath every managed launch. A workspace is `workspaces/<name>/` under the checkout's runtime state root: an isolated tree plus its metadata, lock, exit record, resume record, and host log.

## Design

- **Harness-neutral.** Workspace modules know no Claude or native-harness policy. Launch surfaces supply commands, environment, mounts, and credential tiers as plain launch data.
- **Core contracts point downward.** Runtime paths, project configuration, and git helpers remain in `bro.workspace`; this package imports those contracts, `bro.base`, and the broker interfaces. The framework never imports `ride`.
- **Lazy broker import.** `spawn.py` is imported only after the `containers.broker_enabled` / `container_broker_enabled` gates. `BROKER_DISABLED`, and an environment that cannot import the broker, must short-circuit first.
- **No re-export hub.** Consumers and package code import submodules directly.

## Components

- `build_context.py` — normalized container build context; injects `ride.setup` image assets and shell preludes resolved from the installed `bro` package
- `docker.py` — broker-free container launch description, image hashing/building, container creation, scoped-store copy, attach suspension, and Docker inspection
- `metadata.py` — workspace kind and persisted metadata
- `model.py` — workspace factories, locking, inspection, clean-exit records, and teardown for worktree and container kinds
- `worktrees.py` — host worktree creation and provisioning through the operated repository's `setup.sh`
- `containers.py` — container execution and attachment plus broker availability gates
- `spawn.py` — Docker and host-process broker spawner adapters, bounded child output, terminal ownership, and host-log redirection
- `store.py` — scoped credential tiers, override finalization, host materialization, and container tar packing
- `launch_smoke_test.py` — host-only cold-image launch check, run by the gate's Docker stage
