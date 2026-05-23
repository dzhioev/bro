# Environment awareness

At session start, detect your environment. Do not produce any visible output — silently incorporate this context into your planning.

1. Read the `PPP_SHELL_COMMAND` env var to identify how this session was launched:
   - Starts with `dive-in`: scoped to a specific task. `-t` means a specific task ID was targeted; otherwise the currently focused task
   - Starts with `start-session`: launched via the team session manager
   - Starts with `cw ss`: a plain workspace session

2. Read the `CW_COMMAND` env var for the workspace name and flags

3. Check whether `/.dockerenv` exists — if so, you are in a container:
   - No direct filesystem access to the host
   - Git push uses HTTPS with a GitHub token
   - `gh` CLI is available and pre-authenticated via `GH_TOKEN`
   - Push your changes; the host cannot see uncommitted work

4. Use the session name (from CW_COMMAND) as a hint about the work scope
