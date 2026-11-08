---
name: open-task
description:

This spell should be used when a procedure or the user asks for a new task to be created from a piece of text
— "[[open a task from this]]", "file this as a task", or a spell that starts from seed text for work that has no task yet.
Shows the user the proposed name, tags, and body, then creates the task and reports it.
The body keeps every point the user's text makes and writes in the context it leans on, so nothing the user wrote is lost and the task reads on its own.

parameters: {"seed?": "the user's text to open the task from; empty means collect it from the user"}
version: 1.1.0
---

# open-task

Create a task from the user's text through the `brog::` tools.

1. Take `seed` as the user's text.
   An empty value means ask the user for it.
2. Draft the name, the tags, and the body.
   Names start with a lowercase letter (except proper nouns).
   Write the body for a reader who has only the task.
   It keeps every point the seed makes, with nothing condensed or dropped,
   and writes in what the seed leans on from the conversation
   — what "this" refers to, a finding or an approach the session settled.
   A calling procedure may name a heading to put the body under.
3. Where questions reach the user, show them the name, the tags, and the body and wait for their go-ahead or corrections.
   When unattended, create the task from the draft as it stands.
4. `brog::create_task` with the agreed properties.
   The task is born open (workable).
   Anything richer than name, tags, and body (importance, deadline, project) is triage on the tracker's own surface, not part of this flow.
5. Report the created task as a markdown link, and return its id and url to the calling procedure.
