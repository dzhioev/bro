# One-shot turn end

Whenever nothing else remains, end the turn:
the run idles until work completes or new traffic wakes it.
The runtime judges each kind of work on its own at a turn end:

- A mission carried by the session watch, a live model watch, pending watch lines, an awaited reply, background work, or a summoner who can still speak keeps the run waiting silently.
- A failed waiter gets one notice, whatever else is live:
  an uncovered mission, a watch that is alive but has stopped beating, or a failed session watch.
  The next unchanged turn end ends the run if no live work remains, and orphans the uncovered work.
- No live work ends the run immediately.

Ending the run kills its watchers and host-supervised workers and detaches expected workers.

Start work that may outlast a short wait in the background and end the turn;
its exit wakes you.
Background work that prints nothing for a while wakes you with a line naming it, and again after longer silences.
Read its output, stop it, or end the turn again to keep waiting.
