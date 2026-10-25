# File reading

## Access

`access.py` defines the `FileAccess` protocol, `FileAccessDenied` and the regular-file opener `AllowAll`.
Callers obtain the active implementation through `current()`;
`installed(access)` scopes an override to the current context and restores it on exit.

`open_read` and `open_write` return binary streams owned by the caller.
`open_write(create_parents=True)` permits parent-directory creation after the access decision.
`search_directory` returns the authorized search path, which can differ from the requested spelling.
The default implementation imposes no path policy.

On platforms with `O_PATH`, existing files are pinned and checked before reopening through `/proc/self/fd`.
Otherwise the opener checks file identity before and after opening.
Neither path truncates before checking the opened file, and missing write targets are created exclusively.

## Types and results

- `types.py` — frozen `FileType`, `Signature`, `View` and `Limits` declarations, exported from `bro.files`.
  Declarations import models, never parsers; provider imports belong to workers.
- `registry.py` — `bro.file_types` metadata discovery, single-entry loading and `TypeRegistry` caching.
  `identify(head)` loads every type, while `resolve(head, type=...)` selects only the explicit type.
- `pages.py` — `parse_pages` checks syntax and the type bound before expansion;
  `select_pages` resolves the parsed selection against the document count.
  Selections are one-based sorted sets; `None` means all pages.
- `text.py` — `sanitize` strips invisible characters and reports printable tag payloads with input offsets;
  `sanitize_strings` and `validate_strings` traverse JSON objects, including keys.
- `models.py` — `InfoResult` and `ViewResult` core envelopes.
  Their `from_reply` constructors validate the type-declared payload and preserve its serialized dictionary.
- `operations.py` — `read_reference` and manual field coverage validation.
  Shared caller rules are in `reference.md`; a type ships its own manual.

Tests here run in the gate's `unit` stage without a worker or a parser installation.
File-type authors start at `bro/reference/extending.md`, "Adding a file type".
