"""managed workspaces: the containerization/workspace layer under the launch surfaces.

A *workspace* is `workspaces/<name>/` under the checkout's runtime state root: an
isolated per-task copy of the operated repo in `tree/`, plus the records kept
about it. `meta.json` says which kind it is — a same-machine git worktree or a
docker container clone. This package owns the mechanics every launch surface
shares: workspace creation and provisioning, session-image build,
container create/attach/suspend, inspection and teardown (`model.Workspace`),
scoped credential-store hydration, the broker spawner adapters, and the session
banner. It knows nothing about any harness or agent framework — modules import
only `base`, `broker` (module-level only in `spawn`, imported past the
launch-path gates in `containers`), and the stdlib.

Consumers import submodules directly (`from bro.workspace.docker import Launch`);
intra-package code does the same — there is no re-export hub.
"""
