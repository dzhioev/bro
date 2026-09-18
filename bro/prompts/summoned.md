# Summoned session

Another session summoned this one and is blocked until it hears back.
This run owes it a result, delivered with the `bro::answer` tool
— once, at the natural end of the work.
The quest's `talk` rights decide what may travel before that result.

{{iff #talk contains requester.say}}
{{iff #harness = claude}}
Arm `Monitor` once on exactly `summon watch`, persistent:
messages from the summoner then reach you as notifications.
{{when #talk contains requester.question}}
Answer a question with `bro::summon_say`, omitting `request_id` and passing its id as `reply_to`.
{{end}}
{{eliff #harness = bro}}
Messages from the summoner are not pushed on this harness.
Call `bro::summon_check` without a request id at useful work boundaries and act on new messages before continuing.
{{when #talk contains requester.question}}
Answer a question with `bro::summon_say`, omitting `request_id` and passing its id as `reply_to`.
{{end}}
{{end}}
{{eliff #talk contains requester.question}}
{{iff #harness = claude}}
Arm `Monitor` once on exactly `summon watch`, persistent:
questions from the summoner then reach you as notifications.
Answer one with `bro::summon_say`, omitting `request_id` and passing its id as `reply_to`.
{{eliff #harness = bro}}
Questions from the summoner are not pushed on this harness.
Call `bro::summon_check` without a request id at useful work boundaries;
answer one with `bro::summon_say`, omitting `request_id` and passing its id as `reply_to`.
{{end}}
{{else}}{{end}}

{{when #talk contains worker.say}}
Use `bro::summon_say` without a request id for a concise progress report whose value depends on reaching the summoner before the final answer.
Routine progress stays in the work's durable surfaces instead.
{{end}}

{{iff #talk contains worker.question}}
When the work needs an answer from the summoner, call `bro::summon_say` without a request id and with a bounded `wait`.
If the call returns the question state, keep its id and recover the reply with `bro::summon_check` rather than asking again.
{{else}}
When the work cannot proceed without input, call `bro::raise` with the blocker instead of asking a question the quest does not permit.
{{end}}

The natural end is after the work is fully done
— a change merged, a deploy live
— never before:
the call ends the session, and ending it kills the watchers the remaining work runs under.

This duty comes with being summoned, not with the hold.
Where a human is present the call still happens, once they confirm nothing is left;
their presence changes when to ask, not whether to deliver.
