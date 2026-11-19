# Managed-session image assets

These assets ship in `bro-ride` and are consumed by `ride.workspace.build_context`.
The shell prelude remains in the framework's `bro/setup/` directory and is resolved through `bro.shell`.

## Components

- `container/Dockerfile` — the shared runtime image:
  platform tools,
  the ride user,
  entrypoint,
  and shell helpers.
- `container/entrypoint.sh` — in-container workspace setup:
  user remapping, git safety, then exec of the supplied `do-ride` command.
  Credential hooks, provisioning and the session broxy belong to `do-ride`.

The runtime image contains no Python distribution from the ride installation.
A frozen runtime bundle is materialized into a named volume and mounted read-only at `/var/ride/runtime`.
`test_smoke.sh` is the host-only image/entrypoint check, including attached and detached launches;
it is checkout-only and excluded from the wheel.
Harness-specific image assets belong to their harness implementations.
