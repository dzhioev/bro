# #733 design review: the `cookies+<instance>` profile credential

A throwaway copy of the `## Design` section of [#733](https://github.com/dzhioev/bro/issues/733), for review only.
The task page is the source of truth;
this file is never merged.

## Design

Settled with the user on 2026-09-27 in #733's design session (trail `01m3hesgfw-kqgyfzde-hhmw0nhh`), on `master` at `e6baed61`, starting from #748's `## Design`;
it supersedes `## Shape`, which is dropped.
Then reviewed against the code and the pinned Playwright MCP 0.0.82 (playwright-core `1.64.0-alpha-1789764292000`) and revised with the user in #733's review-and-plan phase (trail `01m3hyq787-ffd0yapp-h6aky144`).

Terms:

- a *profile* is one stored `cookies+<instance>`: a Playwright storage state, the JSON object `{"cookies": [...], "origins": [{"origin", "localStorage", "indexedDB"?}]}`;
- *setup* is `webview setup <instance>`, the host command that writes a profile;
- *capture* is `webview capture`, the container-side verb setup runs;
- a *foreground run* is a worker type's container run by a host command outside any ride;
- a *literal kind* is a credential kind whose material the resolver returns as stored.

Rules:

1. **The `cookies` kind.**
   `bro-webview` registers the `cookies` credential kind through `bro.credentials`, literal (rule 10) and with no install hook.
   Its material is a profile.
   Setup is the framework's only writer;
   a hand-written file of the same shape works alike.
2. **The webview's `pass` field.**
   `WebviewType` declares `pass` in its launch schema, bounded to `cookies`.
   `LaunchPass` gains an optional `kinds` bound, checked where a launch section folds, beside the registered-kind check:
   a pass right under a bounded type naming another kind fails the launch holding it, as a set member outside its choices does, and `ride scope` reports it the same way.
   The bro type's `pass` stays unbounded.
   The rest is #748's:
   the right is spelled `:launch.webview.pass.cookies+<instance>`, folds through the same layers, must name a present instance, and renders on the banner's `permits` row and in `ride scope`.
   At most one pass per kind makes at most one profile per webview.
3. **A container run delivers its passes.**
   The host refuses passes on a job alone.
   `WorkerContainerLaunch` carries a `Container` run's passes,
   and the host's lowering hydrates exactly those instances from the host store, each as the pick of its kind, into the store a worker container already gets at `/home/ride/.bro` under `BRO_STORE`.
   Hydration runs off the broker loop before the container exists, through `credentials.build_scoped_store` as for a session, inside the lowering's cleanup:
   an instance removed since the owner's launch folded ends the mission `failed:launch` naming it, and removes the throwaway workspace.
   A type opts in by declaring `pass`:
   for a type without it no owner holds a pass right, so every pass is refused.
   The store lives in the container's writable layer and dies with it, never in the workspace tree the host keeps after a failure.
   No install hook runs in a worker container.
4. **The owner's side.**
   `webview open --cookies <instance>` sends `pass: ["cookies+<instance>"]` with the launch, which the launch audit records by name.
   The owner's own store never holds the kind.
5. **The daemon loads the profile.**
   Before starting Playwright MCP, `webview serve` reads the `cookies` kind from its store by kind.
   When present, it checks the profile's envelope
   — an object whose `cookies` and `origins` are lists, each origin an object with a string `origin`
   — and leaves the rest to Playwright, since a profile crosses versions (`### Rollout`).
   A failed check fails the daemon before `listening`, naming the defect and never a value, since a worker's output tail reaches its owner in its death result.
   The daemon writes the profile at 0600 into the private temporary directory that holds its Playwright config, outside `/workspace`,
   and starts Playwright MCP with `--storage-state` on that file beside `--isolated`.
   Without it, the browser starts empty as today.
   The file stays for the webview's life, since Playwright MCP reads it again whenever it re-creates the isolated context, as after `browser_close`;
   a re-created context starts from the passed profile again.
   What a run changes in the browser dies with the webview.
6. **What a pass keeps from the owner.**
   The owner drives a browser logged in with the profile and never receives the material.
   The daemon's vocabulary hands out no stored cookie:
   Playwright MCP's `storage` capability, whose tools save the context's state and list its cookies, stays off in `serve`;
   `browser_run_code_unsafe` stays withheld;
   the network tools render headers through Playwright's `headers()`, which omits cookie headers;
   and the tools that read a file, `browser_file_upload` and `browser_drop`, reach only `/workspace` and the output directory within it, so never the profile file.
   What logged-in pages show or expose to their own script, including non-HttpOnly cookies and localStorage through `browser_evaluate`, the owner reads as the logged-in user would.
   A compromise of Chromium or of the server process reaches the profile, as it reaches the rest of the container (#638, "Isolation and the human").
7. **Setup.**
   `webview setup <instance> [--url URL] [--fresh] [--indexed-db] [--port PORT]` runs on the host, in a terminal:
   it refuses inside a managed session (`RIDE_ISOLATION` set), whose store is a scoped copy that dies with it, and without a terminal on stdin, where the user confirms the login.
   It runs `bro/webview:<hash>`, the image webviews run, as a foreground run (rule 9) with `webview capture` as its command and the noVNC port on the launcher's loopback (rule 11), building any missing image first.
   It prints the noVNC URL once the browser is up;
   the user logs in, keeps the browser window open, and presses Enter, or Ctrl-C to discard.
   Setup then asks capture for the storage state, checks that it is a profile, and writes it to the host store (`BRO_STORE` when set, else `~/.bro`) as `cookies+<instance>`.
   It prints counts and never a value:
   cookies and their sites, origins with storage, IndexedDB origins, and bytes.
   - **Seeding.**
     Unless `--fresh`, the browser starts with the stored instance loaded, so the user redoes only an expired login, a site that remembers the device skips its 2FA, and a profile gains sites over several runs.
     A capture identical to the stored instance writes nothing and says so, which also catches a closed browser window that reset the context to the seed.
   - **IndexedDB.**
     It is captured with `--indexed-db`, or when the stored instance being re-captured holds any, so a re-run never silently drops it;
     `--fresh` without `--indexed-db` drops it.
   - **Refusals.**
     A capture with no cookies and no origin storage is refused, since it is a logged-out profile.
     A stored name `creds.json` annotates with a source other than `local` is refused, since material written under it would never be read.
   - **The write.**
     `bro/base/credentials.py` gains the store's first programmatic writer, writing a stored name's material atomically at 0600 under `creds/`.
     Setup reads its seed through the resolver, by kind under an explicit selection.
8. **Capture.**
   `webview capture` is the container-side verb beside `serve`, and refuses to run without setup's options in its environment.
   It shares the daemon's start-up: Xvfb, the VNC stack, and Playwright MCP headed with `--isolated`, `--no-sandbox`, and downloads refused, with the view always on and no broker.
   Its working and output directory is a private temporary directory in the container layer;
   the seed arrives as the first line on stdin, `null` when setup starts empty, and loads through `--storage-state`;
   and it navigates to `--url` when given.
   It says ready on stdout, and on setup's request writes the storage state as one JSON line on stdout:
   a fixed `browser_run_code_unsafe` snippet has Playwright save `page.context().storageState({ path, indexedDB })` into the private directory, which capture reads and removes.
   Its stdout carries that exchange alone:
   the processes it starts write their output to its stderr.
   Setup's own code is capture's only client, so the tool `serve` withholds from an owner is safe here.
   EOF on stdin, a supervised process's exit, or a malformed request ends it non-zero;
   setup then writes nothing and shows the container's output tail.
   The container mounts only the runtime, so the state reaches the host disk only through setup's store write.
9. **The foreground run.**
   `ride/ride/worker_container.py` gains a context manager that runs a worker type's `WorkerContainer` outside any ride, taking the type's name only for the image tag.
   It freezes the invoking installation, resolves the runtime image and volume, and builds the worker image under its tag reservation.
   It publishes the declared ports on loopback, each on its requested host port or an available one (rule 11), with `RIDE_PUBLISHED_PORTS`,
   and runs the command with the runtime volume read-only and stdin and stdout piped to the caller, with no workspace, store, broker, or artifact view;
   leaving the block removes the container.
   `bro-webview` depends on `bro-ride` for it.
   Only its setup module imports `ride`, loaded lazily by the `setup` verb;
   a policy test keeps `ride` out of the type module and the daemon, which every launch-scope fold and every broker root load.
10. **Literal kinds.**
    A credential registry entry may declare `"literal": true` beside `description` and `install`.
    The resolver returns a literal kind's material as stored, expanding no `{"$cred": …}` node in it, and hydration follows none of them.
    A profile is JSON that websites author, and Playwright saves an IndexedDB record's plain-object value verbatim:
    expanded, a site's stored `{"$cred": "anthropic"}` would become the host's key in that site's storage at the next hydration or seed read.
11. **A fixed view port.**
    `webview open --vnc --port PORT` and `webview setup <instance> --port PORT` publish the noVNC view on that launcher-loopback port rather than an available one,
    so one standing forward such as `ssh -L PORT:127.0.0.1:PORT` reaches every run on a remote host.
    `open` sends it as the launch field `vnc_port`, which the webview refuses without `vnc`.
    `WorkerContainer.published_ports` maps each container port to its requested host port, `None` where the host picks an available one.
    The host refuses a requested port below 1024, since the Docker daemon binds it as root;
    a port already taken fails the launch or setup, as a port taken before `docker start` does today.

In set terms, over #748's:

```
passable(x, webview) = { (cookies, i) | :launch.webview.pass.cookies+i ∈ names(x) }; a right for another kind fails at fold
a webview w launched by request r of owner o:
  r.pass ⊆ passable(o, webview), at most one instance
  store(w) = hydrate(host store, r.pass), each pass the pick of its kind, its material literal
  browser(w) starts from store(w)'s cookies when present, else empty
```

### Rollout

The change is additive:
a file or record an older version wrote stays readable, and a ride runs one bundle for its life.
What crosses versions:

- `~/.bro.json`:
  an installation older than this change fails every launch whose layers name `:launch.webview.pass.cookies+<instance>`, since its webview has no `pass` field and `cookies` is unregistered there;
  one older than #748 fails every read of the file.
  So, as #748's rollout says, a pass right goes into `~/.bro.json` only once every installation reading it is upgraded;
  until then it goes on launch flags.
- The profile, `creds/cookies+<instance>.cred` in the host store:
  setup writes it at the version of the checkout it runs from, and every ride hydrates it and its webview loads it at the version of that ride's bundle, older or newer.
  An installation older than this change never reads it, since it skips material of an unregistered kind.
  Among the rest, the daemon checks only the profile's envelope and Playwright's validator drops the fields it does not know,
  so a profile captured under a newer Playwright MCP pin loads under an older one.
- `creds.json` in the host store:
  nothing here writes it;
  a `defaults` pick of `cookies` would fail every older installation's store read, since `defaults` refuses an unregistered kind,
  so none goes in before every installation reading the store is upgraded.
- Worker images and runtime volumes are content-addressed and shared on the host:
  setup's build prunes an unused older `bro/webview` tag as a ride's build does, and a ride whose next webview needs that tag rebuilds it.
- Nothing else crosses versions:
  a webview's launch request and store, and the capture exchange, live within one ride or one setup run, each on one frozen installation.

The order:

1. Land the change.
2. Upgrade the host's checkout.
3. The user runs `webview setup <instance>` from the upgraded checkout and logs in by hand.
4. A root started from the upgraded checkout with `--grant :launch.webview --grant :launch.webview.pass.cookies+<instance>` verifies;
   its launch fails unless step 3 left the instance in the host store.

Rolling back takes every `cookies` pass right out of every launch command, then downgrades the checkout;
stored profiles stay, unread by the older version.

### Rejected alternatives

- `browser` as the kind's name, taken by the upcoming `browser` bro;
  the user chose `cookies` over `webview_profile`, `site_data`, and others.
- A `--profile` flag: "profile" and "cookies" would name one thing.
- A `WorkerContainer` field naming the instances a type wants, the earlier draft:
  the request's `pass` already names them, bounded by the owner's pass rights, and a second list would need a bound of its own.
- Refusing other kinds in the webview's `launch()`: the right would still fold, be presence-checked, and render, and never be usable.
- No bound: a mistaken grant would deliver another credential into the browser container.
- Delivering the profile through the owner, the container's environment, or a bind of a host file:
  the owner would hold it;
  the environment shows in `docker inspect` and in every process's environ;
  a bind keeps it on the host disk while the worker runs.
- Removing the daemon's profile file after the first context:
  Playwright MCP re-reads it to re-create the context, and the browser holds the same cookies in memory.
- Withholding `browser_evaluate` when a profile is passed:
  what a page exposes to its own script stays reachable by driving the page as the logged-in user.
- The `storage` capability in `serve`: it would hand the owner the profile's HttpOnly cookies.
- Writing the profile back at close (#638's `### Rejected`).
- Setup as a webview mission: the state would cross the mission chat, which the journal and the launch audit keep in full.
- Playwright's `open --save-storage` for capture:
  it saves only when the browser closes, swallowing a failed save, and would redo the display stack in shell.
- A persistent Chromium user-data directory as the profile: not text material, one browser at a time, and far larger.
- Always capturing IndexedDB:
  apps cache bulk data there, so a profile could reach hundreds of MB, copied into every webview and replayed at its warm-up.
  Never capturing it:
  a site keeping its login there, as Firebase Authentication does, could not be used.
- Setup starting empty by default: every re-run would redo every login, 2FA included.
- A `ride setup <type>` verb over a worker-type setup hook: no member dependency, but a contract method only the webview uses.
- Waiting for the proposed `bro-claude` and `bro-container` splits (`### Follow-ups`):
  they are separate restructurings, and when `bro-container` exists only setup's imports move.
- Setup refusing a capture that holds a `$cred` key, in place of a literal kind:
  a hand-written or earlier profile would still expand.
- Storing the profile base64-encoded so it never parses as JSON: the file would stop being a readable storage state.
- A strict profile schema in the daemon: a profile captured under a newer Playwright MCP pin would fail an older webview.
- A view port only for setup: `webview open --vnc` needs the same standing forward on a remote host.

### What changes in practice

- A session holding `:launch.webview` and `:launch.webview.pass.cookies+alice` runs `webview open --cookies alice`.
  Its webview starts logged in wherever alice's profile is, while the session's own store never holds the cookies.
- `webview setup alice --url https://example.com` on the host prints a noVNC URL;
  the user logs in there and presses Enter, and a re-run redoes only what expired.
- `--port 6080` on `webview setup` or `webview open --vnc` publishes the view on `127.0.0.1:6080`, so one standing `ssh -L 6080:127.0.0.1:6080` reaches either.
- A pass right for any other kind under `webview` fails wherever it's written.
- `webview open` fails naming a profile removed since the owner's launch, or one that is not a storage state.
- A `{"$cred": …}` node a site stored in its IndexedDB stays that node in the site's storage.

### Implementation sketch

To settle in the plan phase:

- `bro/base/credentials.py`:
  literal kinds in `CredentialKind`, the resolver, and hydration;
  the store writer.
- `bro/worker_types.py`:
  `LaunchPass.kinds`, validated at type load;
  `WorkerContainer.published_ports` as a mapping to requested host ports.
- `ride/ride/scope.py`: the bound in `_launch_name_schema`.
- `ride/ride/launch_control.py`: passes refused on a job alone and carried on `WorkerContainerLaunch`.
- `ride/ride/worker_container.py`: hydration inside the lowering's cleanup, requested host ports, and the foreground run.
- `webview/`:
  - the `cookies` kind (`credentials.py`), and the `pass` field and `vnc_port` (`worker.py`);
  - the profile check and load, and the start-up shared with capture (`serve.py`);
  - capture and setup;
  - the CLI verbs: `open --cookies` and `--port`, `setup` with `--port`, and `capture`;
  - `pyproject.toml`: the `bro-ride` dependency, the `bro.credentials` entry point, and the deptry map;
  - the import-policy test.
- Docs:
  `webview/AGENTS.md`;
  `bro/reference/ride.md` ("Session permissions and credentials" for the shipped schemas and passes to containers, "Worker containers" for the store and requested ports);
  `bro/setup/AGENTS.md` (the `cookies` material, literal entries, the writer);
  `ride/AGENTS.md` (`worker_container.py`, the pass invariant);
  root `AGENTS.md` (the webview row);
  and `README.md` (the pass sentence).

### Verification the stages owe

- Units:
  - the bound at type load and at fold;
  - literal kinds: a `{"$cred": …}` node in a literal kind's material survives resolution and hydration as stored, while another kind's still expands;
  - a container's passes carried and a job's refused;
  - hydration into the worker store, and a missing instance failing the launch with the workspace removed;
  - requested host ports: published as requested, refused below 1024, and `vnc_port` refused without `vnc`;
  - `serve` passing `--storage-state` exactly when a profile is present, failing before `listening` on a malformed one with an error that names no value, and never enabling a capability;
  - capture's exchange, seed, IndexedDB flag, and EOF, with nothing but the exchange on its stdout;
  - setup's refusals (session, terminal, a source other than `local`, empty), the unchanged no-write, IndexedDB kept on re-capture, and a summary without values;
  - the writer (atomic, 0600, canonical names);
  - the import policy.
- `webview_e2e`, host-only:
  it runs on its own runner in every pull request's CI, a stage's pull request into its integration branch included, while `run-tests` skips it inside a container.
  - a synthetic `cookies+e2e` with a cookie, an HttpOnly cookie, and a localStorage entry for `https://example.com`;
  - a root holding its pass right runs `webview open --cookies e2e`, navigates there, and reads the cookie and the localStorage entry through `browser_evaluate`,
    while the owner's store lacks the kind and neither `browser_evaluate` nor `browser_network_request` output shows the HttpOnly cookie's value;
  - a pass beyond the rights is denied;
  - `webview open --vnc --port <free port>` reports its view on that port;
  - setup, driven under a pseudo-terminal, seeded from `cookies+e2e` captures the same cookie back, and `--indexed-db` round-trips an IndexedDB record.
- The verification phase:
  the user logs in to a real site through setup's view, and a webview opened with that profile shows the logged-in page.

### Follow-ups

Proposed in this session and not filed:

1. Extract the Claude harness into `bro-claude` over a harness-neutral base image.
2. Then extract the container runtime (runtime bundle, base image, worker images, the foreground run) into `bro-container` beneath `ride`, which setup's imports then move to.

### Open

None.
