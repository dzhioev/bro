# Environment awareness

At session start, detect your environment.
Do not produce any visible output
— silently incorporate this context into your planning.

Call the `bro::banner` tool once.
It returns the structured session facts as `key: value` lines:
`isolation`, `repo`, `name`, `bro`, `llm`, `workspace_host_path`, `workspace_container_path`, `docker_shell_command`, `ride_command`, `party`, `summoned`, `may_summon`, `talk`, `permits`, and `trail_id`.
The `llm` row names the admitted recipe in canonical text and its source.
Interpret them as follows:

1. `isolation: boxed` means the workspace runs in its own container.
   A boxed session reports `name`, `workspace_container_path`, and `docker_shell_command`:
   - It has no direct filesystem access beyond its mounts.
   - When `repo` names an attachment, git over HTTPS and the `gh` CLI are pre-authenticated.
     Push changes from the managed session because an outside checkout cannot see uncommitted workspace work.
   - `repo: none (detached)` means no repository is attached.
     `/workspace` is a plain writable directory, not a checkout, so do not infer git state from cwd.
   - `docker_shell_command` (`ride exec <name>`) is what the user runs from the launcher to enter the box.
   - Bare commands resolve from the root's pinned runtime;
     the repository's own guide says how its tools are reached.

2. `isolation: unboxed` means the workspace tree runs directly on the launcher's filesystem.
   The launcher may itself be inside a foreign container, so unboxed does not imply access to the physical host.
   An absent isolation means no ride session published the fact.
   A `party: joined as … (shared tree)` line means the session joined its summoner’s workspace and works concurrently in the same tree;
   its absence means this is the workspace’s first session or no launcher published membership.

3. Treat `repo` as the attachment fact;
   never probe cwd git to decide whether the managed session is attached.
   Use the session `name` as a hint about the work scope.

4. `summoned` says whether this session owes another one a result.
   `summoned: yes` means a summoner is blocked on it, and the run delivers through the `bro::answer` tool
   — whatever the hold, and whoever else is in the conversation.
   That line is the whole answer to "does this session owe a reply";
   `may_summon` below is a different question and never answers it.

5. `may_summon` lists the bros this session may summon
   — the outgoing half, about children this session can spawn, never about where it came from.
   It grants, it never asks:
   summon only where your instructions call for a summon or leave the delegation to your judgment;
   plan that summon from this list instead of probing.
   `may_summon: none` means this session may summon nobody,
   and the line's absence means this process has no mission authority channel.
   A listed target can still be denied for another reason, but an unlisted one always is.
   An entry answers to the bros it derives from as well as to its own name,
   and the line marks it with them:
   an entry shown as `acme-reviewer (reviewer, bro)` is the `reviewer` a text asking for one means.
   The summon still targets the listed name itself
   — a name an entry merely answers to is denied unless it is listed in its own right.
   Where several entries answer to one role they all qualify;
   pick by their descriptions.
   The banner reads this list live.
   The operator may change it with `ride override <mission> --grant @<bro>` or `--revoke @<bro>`;
   a watch notification reports the change.

6. `talk` lists the chat moves fixed for this run's own quest.
   `owner.say` and `owner.question` let the summoner send messages or questions to a summoned run;
   `worker.say` and `worker.question` let that run report or consult its summoner.
   `talk: none` means the quest is mute, while an absent line means the launcher published no talk.
   The host enforces these rights and nothing in-session widens them.

7. Authority rows read the session's host record live.
   `permits` lists usable permissions other than bro targets, such as `bro.placement=join`, `webview`, `webview.vnc`, and `bro.cred=github+reviewer`.
   `may_summon` lists usable bro targets; conditional entries carry their `for` restrictions.
   `may_give` lists give-only entries with their recipient restrictions, such as `webview.cred=cookies+assistant to @browser`.
   A missing authority row means there is no mission channel, not that the host published an empty authority.
   A start needs its type and every selected option value, defaults included.
   A child may receive use this session lacks if it matches the allowed recipients; onward give cannot be wider than this session's own.
   An unmarked summon defaults boxed and never falls back to unboxed or a join.
   Request another placement explicitly when allowed.
   The operator can change concrete bro targets and session credentials through `ride override`; other changes need a new start or resume.
   The [permission reference](../reference/permissions.md) defines the grammar and checks; its [cheat sheet](../reference/permissions_cheatsheet.md) covers usual setups.

8. `trail_id` is the trail this session is recorded into.
   It can roll mid-session
   — never cache it, read it off the banner when you need it.

9. If a `session_recording:` line appears (always first in the output), session recording is broken
   — the transcript isn't reaching trails and this session could be lost on `--drop`.
   `FAILING` is a recorder that is erroring;
   `STOPPED` is one that is no longer running at all, so nothing will resume on its own.
   Don't swallow either:
   tell the user in your first reply, and point them at what the line says to `see` for the cause.
