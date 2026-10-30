# File-type providers

`bro-filetypes` contributes the leaf namespace `bro.pdf` and registers its `TYPE` in `bro.file_types`.
Core imports no provider.
The declaration imports models only; `provider.py` and `engine.py` belong in the worker.

## Components

- `bro/pdf/__init__.py` — the PDF declaration and details model.
- `bro/pdf/engine.py` — direct ctypes bindings, initialized PDFium and scoped document/page/text handles.
- `bro/pdf/page_text.py` — assembly of the per-character list into sanitized text and ordered notes.
- `bro/pdf/provider.py` — preparation, inspection and view dispatch, including the has-text fast path.
- `bro/pdf/links.py` — link enumeration, URI byte display and spatial anchor selection.
- `bro/pdf/attachments.py` — declared-text attachment reads and content validation.
- `bro/pdf/form_fields.py` — widget values and deterministic nearby-label selection.
- `bro/pdf/view_limits.py` — cumulative string and entry accounting for views.
- `bro/pdf/manual.md` — the shipped caller reference.
- `bro/pdf/pdf_test_helper.py` — stdlib-only PDF generation for tests and checkout benchmarks; excluded from the wheel.
- `bro/pdf/hostile_test_helper.py` — stdlib-only hostile corpus generators, one per family; excluded from the wheel.
- `bro/pdf/hostile_corpus_sandbox_test.py` — the hostile corpus through the real confined worker, pinning each family's first-run outcome.

## Development

The root owns formatting, typing and the gate.
Provider tests run in `unit`; tests through the real confined worker run in `sandbox` (`local/AGENTS.md`).
Build with `uv build --package bro-filetypes`.
The engine dependency is pinned in this member's metadata; a bump is a security update and requires exercising both rosters.
