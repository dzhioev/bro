# Testing

- A test asserts what the code does, not what the source says.
  A test's expected value copied from the implementation
  — a constant, a default, a registry entry, prompt text
  — makes the assertion a mirror:
  the only failure it can catch is an edit that forgot to update the copy, so it verifies nothing and taxes every legitimate change to the value.
  The gate is naming a failure the assertion would catch besides "someone changed the value";
  none means it is a second copy of a decided fact
  — cut it.
  Cover a value through the behavior it drives
  — the branch a default selects, the effect a config entry has
  — and cover checked-in data structurally (it parses, entries are well-formed, references resolve), never by restating entries;
  a data-only change then needs no new test.
  When the claim is that a declared value reaches a surface, compare the surface against the imported declaration (`assert timeout == DEFAULT_TIMEOUT`), not a retyped copy;
  when the claim is that assembly merges sources, assert one representative member per source, not the transcribed roster;
  when the claim is that a surface selected the right file or branch, one copied phrase is the marker
  — chosen to discriminate the fork, not a transcript of the content.
  A full roster or a copied literal stays right in two places:
  where real machinery between declaration and observation
  — entry points, a built wheel, a spawned process
  — can silently drop or admit members,
  and where the value is an external contract something outside the repository reads as-is
  — an API's required headers, a persisted disk layout, an env var another process consumes
  — so referencing the symbol would let a breaking rename slip through;
  a pinning test says which consumer it pins, in its name or in one comment.

- A test that waits on another thread, process, or async event is also held to the `concurrency` guide page of `man-source::read`.
