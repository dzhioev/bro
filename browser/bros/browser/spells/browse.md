---
name: browse
description:

  Run an owner-led browser session: summon it detached with `owner.question,worker.question` talk and an hours-sized timeout;
  send each instruction as a question and read the outcome in its reply;
  answer a clarification in its thread with a counter-question;
  share upload artifacts with the running quest and name them in the instruction;
  ask "done" to close the webview and receive the session log.
---

# browse

Run an owner-led webview until the owner asks "done".

## Start

Open one webview, using a profile or visible view only when the summon prompt names it.
If this is a summoned session, run [[watch quest watch]], keep that procedure active, and send nothing until the first owner question arrives.
If a human is driving this session directly, do not start a quest watch;
their conversation messages are the instructions and your conversation replies carry the outcomes.

## Each instruction

Treat each owner question as one thread.
Act on the instruction, then reply in the summoner's terms with:

- what you did and its outcome;
- where the browser is now;
- the content relevant to the instruction and the session goal;
- the controls the owner can name next.

Do not expose element refs, page markup such as snapshot syntax or HTML, or tool names;
Markdown formatting is fine.
Keep the reply within the 16 KiB message bound;
write longer data to a file and report its artifact ref.

If the instruction is ambiguous or crosses a policy line it did not settle, ask a counter-question on `self` with `reply_to` set to the instruction's question id.
The owner answers that clarification with another question in the same thread;
after acting, reply to that question with the outcome.
If the owner answers as a plain say instead, send the outcome as a say because there is no question to close.

For an upload, use only a ref the owner shared with this quest for that purpose.
Pass it to `webview::share`, upload the returned path, and name the file in the outcome.
A file inside a shared directory keeps its own name.

When summoned, follow the active watch procedure to wait for the next quest-watch line rather than ending a turn with the watch and webview still live.

## Finish

On "done", close the webview and reply to that question.
If this is a summoned session, end the active watch through its procedure, then call `bro::answer` with a session log naming the sites, changes made on them, and refs of files produced.
If a human is driving this session directly, return that session log in the conversation and do not call `bro::answer`.
Condense actions when the log would exceed the answer bound because each instruction already received its outcome.
