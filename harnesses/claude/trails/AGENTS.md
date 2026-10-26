# Claude trail format

`bro-claude-trails` registers the `claude` trail format from `bro/claude_trails/format.py`.
It owns the transcript envelope, classification, header fields, lineage and projection, decoding assistant content through the Messages wire roster entry.
It depends on core and `bro-wire-messages`; reading trails needs no harness or provider SDK.
The package `__init__.py` stays empty.

Formatting, linting, typing and the test gate belong to the repository root.
Build with `uv build --package bro-claude-trails`.

## Lineage

- `bro/claude_trails/lineage.py` owns the claude evidence contract and resolves lineage from it:
  the adopted segment, its lines' record uuids and digests, and the sibling segments sharing those records.
  It verifies that evidence against candidate headers' `lineage_head` and answers with the file ranges the recording trail owns, plus how that trail is reached:
  the trail this lifetime attaches to, a fork point for a new one, a decline while a history copy is mid-write, or a root.
  Trail identity follows the segment:
  a verified same-segment resume attaches, so a linear conversation stays one trail across process restarts and a fork edge marks an actual fork.
  A trail whose blaze response was lost holds no row to verify, and is attachable on the `cuts` its mint stored instead, which is what makes a blaze idempotent for the segment it names.
  `LineageIndex` is the store-internal surface it reads through, two lookups each backend implements its own way:
  the trails recording a set of segments
  — `DynamoStore` queries the `segment-started_at-index` GSI (partition `segment`, range `started_at`, projecting the whole header) over the top-level `segment` attribute, `LocalStore` filters its header scan
  — and whether any of them stores one record uuid, the mid-write test and the only row read a resolution can make.
  Nothing on this path crosses the wire, and continuity across recorder lifetimes comes from the stored rows alone, through the head they folded.
