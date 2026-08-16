# Environment awareness

At session start, detect your environment. Do not produce any visible output — silently incorporate this context into your planning.

Call the `bro::banner` tool once. It returns the structured session facts as `key: value` lines (`kind`, `name`, `bro`, `workspace_host_path`, `workspace_container_path`, `docker_shell_command`, `cw_command`, `may_summon`, `trail_id`). Interpret them as follows:

1. `kind: container` means you are inside a docker container. Managed session containers also report `name`, `workspace_container_path`, and `docker_shell_command`; an unmanaged container omits them rather than claiming `/workspace` belongs to the session:
   - No direct filesystem access to the host
   - git (push over HTTPS) and the `gh` CLI are pre-authenticated in managed sessions
   - Push your changes in a managed session; the host cannot see uncommitted work
   - `docker_shell_command` (`cw exec <name>`) is what the user runs from their host shell to drop into a managed container

2. Use the session `name` as a hint about the work scope

3. `may_summon` lists the bros this session may summon — plan delegation from it instead of probing: `may_summon: none` means this session delegates to nobody, and the line's absence means the launcher published no list (an unmanaged environment). A listed target can still be denied for another reason, but an unlisted one always is. The list is fixed at launch and nothing in-session widens it; widening means relaunching with `--grant @<bro>`, which is the user's call.

4. `trail_id` is the trail this session is recorded into. It can roll mid-session — never cache it, read it off the banner when you need it.

5. If a `session_recording: FAILING` line appears (always first in the output), session recording is broken — the transcript isn't reaching trails and this session could be lost on `--drop`. Don't swallow it: tell the user up front and point at `<claude config dir>/session-recorder.log` for the cause. A red statusLine warning also stays pinned for the user, but surface it in your first reply too.
