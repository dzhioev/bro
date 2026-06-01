# Environment awareness

At session start, detect your environment. Do not produce any visible output — silently incorporate this context into your planning.

Run `cw banner --llm` once via Bash. It prints the structured session facts on stdout (`kind`, `name`, `bro`, `workspace_host_path`, `workspace_container_path`, `docker_shell_command`, `cw_command`, `launch_command`). Interpret them as follows:

1. `launch_command` tells you how this session was launched:
   - Starts with `dive-in` with `-t <id>` or `--focus`: scoped to a specific task (`-t` names it directly; `--focus` uses whatever is currently focused)
   - Starts with `dive-in` without either flag: a clean session unattached to any task — start by asking what to work on
   - Starts with `start-session`: launched via the team session manager
   - Starts with `cw ss`: a plain workspace session

2. `kind: container` means you are inside an isolated docker container:
   - No direct filesystem access to the host
   - Git push uses HTTPS with a GitHub token
   - `gh` CLI is available and pre-authenticated via `GH_TOKEN`
   - Push your changes; the host cannot see uncommitted work
   - `docker_shell_command` (`cw exec <name>`) is what the user runs from their host shell to drop into a shell inside this container

3. Use the session `name` as a hint about the work scope
