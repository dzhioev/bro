{{iff #harness = bro}}
Call `bro::job('<command>', mode='watch')` once and keep the job id.
Its lines arrive with tool results as background-job notifications;
use `bro::poll` when a pending marker says more output remains.
Whenever nothing else remains, call `bro::chill()`:
the run wakes on the next line and idles at no cost in between.
End the watch with `bro::kill(id=<job id>)` once it is no longer needed;
a run that ends while the job runs kills it.
{{eliff #harness = claude}}
Run `watch-run <command>` with the Bash tool in the background, once:
it runs the command for the rest of the session and keeps every line it prints.
Whenever waiting is all that is left, run `watch-next` with the Bash tool in the background and end the turn:
it returns as soon as a watch has new lines, each tagged `[<command>]`, and its result opens your next turn.
Act on the lines, then run `watch-next` again the same way.
It may start beside `watch-run` in the same turn, and one `watch-next` serves every watch you run;
with several, name a command after it to read that watch alone.
End the watch with `TaskStop` on the `watch-run` task once it is no longer needed;
the session cannot end while it runs.
Never put a watch on the Monitor tool:
it dies at a deadline and drops what arrives before it is armed again.
{{end}}
