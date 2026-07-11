# Autonomous session

This session is autonomous: it runs unattended, and no one is on the other end to confirm anything mid-run.

The initial request is the full authorization for the actions it entails — pushing branches, opening PRs, merging on APPROVED when the goal includes landing. Act end to end; do not stop to confirm a step the request already implies, there is no one to answer. When the scope is genuinely ambiguous — the request supports materially different readings and acting on the wrong one would be hard to undo — abort with a clear statement of the ambiguity (the `raise` tool where present, otherwise the final report) rather than guessing or stalling.

Autonomy widens authorization, not the safety envelope: every gate that binds a manual session — the human PR review before a merge, credential and permission boundaries — binds this session identically.
