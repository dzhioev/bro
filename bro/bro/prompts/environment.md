# Environment awareness

At session start, detect your environment. Do not produce any visible output — silently incorporate this context into your planning.

Call the `bro::banner` tool once. It returns the structured session facts as `key: value` lines (`kind`, `name`, `bro`, `workspace_host_path`, `workspace_container_path`, `docker_shell_command`, `cw_command`). Interpret them as follows:

1. `kind: container` means you are inside an isolated docker container:
   - No direct filesystem access to the host
   - Git push uses HTTPS with a GitHub token
   - `gh` CLI is available and pre-authenticated via `GH_TOKEN`
   - Push your changes; the host cannot see uncommitted work
   - `docker_shell_command` (`cw exec <name>`) is what the user runs from their host shell to drop into a shell inside the container

2. Use the session `name` as a hint about the work scope

3. If a `session_log_sync: FAILING` line appears (always first in the output), the session-log sync is broken — transcripts aren't reaching S3/DynamoDB and this session could be lost on `--drop`. Don't swallow it: tell the user up front and suggest re-running `setup/bootstrap_session_log.sh`. A red statusLine warning also stays pinned for the user, but surface it in your first reply too.
