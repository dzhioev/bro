# One-shot turn end

Whenever nothing else remains, end the turn:
the run idles until work completes or new traffic wakes it.
At each turn end, the runtime takes the first applicable route:

- A mission carried by the session watch, a model-owned watch, pending watch lines, or an awaited reply keeps the run waiting silently.
- Background work or a summoner who can still speak gets one notice;
  later unchanged turn endings keep waiting for it.
- An uncovered mission or failed session watch gets one notice;
  the next unchanged turn end ends the run and orphans the uncovered work.
- No live work ends the run immediately.

Ending the run kills its watchers and host-supervised workers and detaches expected workers.
