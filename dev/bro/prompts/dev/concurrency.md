# Concurrency

- Synchronize on the event itself, never on a stand-in for it.
  When correctness depends on an event, block on its actual completion signal
  — a handshake, EOF, event, or exit code — and leave runaway bounds to the supervising layer.
  A stand-in is anything whose order against the event depends on how long something takes:
  a tuned timeout, a sleep, or another observable that usually lands first
  — an entry appearing in a table read as its consumer having taken it.
  A wait on a stand-in is a race whose outcome depends on machine load;
  so is a wait whose expiry silently continues as though the event happened.
  Tests are held to this step by step:
  before a step acts on a state, the test waits for the signal that establishes exactly that state,
  and where the code exposes none, the test is restructured or the code gains one.
  A test that passes on either side of such a race does not test what it is named for, flaky or not.
  A checker that flags sleeps cannot see a stand-in event, so this stays the author's and the reviewer's to hold.
  A timeout is appropriate when expiry fails loudly or degrades with a warning:
  readiness gates that warn, test deadlines that fail the test, and CLI deadlines that exit non-zero are bounds, not correctness fallbacks.
