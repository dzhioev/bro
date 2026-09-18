{{when #harness = claude}}
# Summoning session

Arm `Monitor` once, before the first summon, on exactly `summon watch`, persistent.
The start, messages, and end of every summon beneath you then reach you as notifications:
your own, and the ones your children make in turn, marked with the request they were summoned under.

When a child asks a question, answer with `summon say <quest> '<answer>' --reply-to <question>`.
Then collect the child's eventual answer with `summon check --wait <quest>` rather than summoning it again.
{{end}}
