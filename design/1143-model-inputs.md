# Design: model inputs — typed message content, listed models and passthrough recipes

This file is the specification of [#1143](https://github.com/dzhioev/bro/issues/1143).
It lives on the branch `design/1143-model-inputs`, which is never merged:
the pull request that carries it is a design review only.

## 1. Goal, scope and constraints

Goal: the framework's typed one-shot calls (`mu`) and the native loop send images and documents to a model through any wire client, in one provider-neutral vocabulary;
content a model does not take, and a model the framework does not describe, is refused before any request unless the recipe opts out.

What it enables, for example:

- `mu(instructions, Totals, Text('Extract the totals.'), File.from_path('march.pdf'), role='reader')` answers on an OpenAI recipe.
- The same call on `deepseek:pro` raises `UnsupportedContent` naming `application/pdf` and `deepseek-v4-pro`, and sends nothing.
- `bro.native.llm --provider deepseek --model flash 'what does it show?' --attach chart.png` sends the image;
  the same command with `--model pro` is refused before the request.
- `--llm openai:gpt-7:high` is refused at launch, since `gpt-7` is not a listed model;
  `--llm openai:gpt-7:high+passthrough` launches, and the vendor alone decides what that model takes.

Settled by the owner during the discussion of 2026-10-31 (the decision record, §13, lists every decision with its status):

1. One content vocabulary, owned by the wire port, replaces the Chat Completions-shaped dicts that message content travels in today; `mu` takes it directly.
2. One file content class carries `data`, `mime_type` and an optional `filename`, with a constructor that reads a path.
3. Whether a recipe accepts a file is decided per model and per MIME type, from each vendor's documentation of each listed model.
4. A model its provider does not list is refused by default, and the accepted types are always checked before a request.
5. One recipe flag, `passthrough`, admits unlisted models and makes every MIME type accepted;
   the check keeps its name, `accepts`, and returns true under the flag.
6. `JSON`, `DateTimeEncoder` and `Markdown` leave `mu`.

Out of scope, each with where it belongs:

- image or file content in tool results ([#1046](https://github.com/dzhioev/bro/issues/1046));
- offering an agent tools according to what its model accepts ([#1046](https://github.com/dzhioev/bro/issues/1046), [#1047](https://github.com/dzhioev/bro/issues/1047));
- `file::ask`, `detail` levels and cache breakpoints on file content ([#1048](https://github.com/dzhioev/bro/issues/1048));
- a client for the Messages wire, so Anthropic recipes stay decode-only and their table only gates launches (§5);
- size limits on files, where each vendor's own limit applies;
- identifying a file's type from its bytes.

Constraints:
core imports no other workspace member, and declaration modules import no SDK at module level (`bro/llm/AGENTS.md`);
recorded trails stay readable;
backward compatibility is not a constraint (`AGENTS.md`, "Conventions").

## 2. Concepts

- **Content item** — one element of a user message's content: a `Text` or a `File` (§3).
  `Content` is the union type.
  The response side's `Part` (`bro/llm/records.py`) is a different concept and keeps its name.
- **MIME type** — a string `type/subtype` in lowercase, without parameters: `image/png`, `application/pdf`.
- **Model inputs** — for one model, the set of MIME types of `File` content it accepts.
  `Text` is accepted by every model and is not part of the set.
- **Listed model** — a model id that is a key of its recipe class's model-inputs table, `MODEL_INPUTS` (§4).
- **Passthrough recipe** — a recipe whose `passthrough` flag is set (§4):
  the framework consults no model list for it, so its model and content pass through to the vendor unchecked.
- **Admitted recipe** — a recipe allowed to drive a model (rule A1, §5).
- **Selection** — a launch's recipe choice as `--llm` or the piece flags spell it (`LLMSelection`, `bro/llm/providers.py`);
  the **standing recipe** is the one it applies to, the persona's declared or the configured recipe.
- **Model gate** — the refusal of a `File` whose MIME type the recipe does not accept (§6).
- **Protocol gate** — the refusal of a `File` that the wire's protocol cannot carry, though the recipe accepts it (§6).

## 3. Message content

`bro/llm/wire.py` defines the content items beside the inputs it already defines (`SystemPrompt`, `UserMessage`, `Notification`, `ToolOutput`):

```python
@dataclass(frozen=True)
class Text:
  text: str


@dataclass(frozen=True)
class File:
  data: bytes
  mime_type: str
  filename: str | None = None

  @classmethod
  def from_path(cls, path: str | os.PathLike[str], mime_type: str | None = None) -> 'File': ...

  def data_url(self) -> str: ...


Content = Text | File


@dataclass(frozen=True)
class UserMessage:
  content: tuple[Content, ...]
```

Construction rules, each enforced by the class at construction:

- **C1.** `Text.text` is a `str`, possibly empty.
- **C2.** `File.data` is `bytes` and is not empty.
- **C3.** `File.mime_type` matches `[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*`, the restricted-name grammar of RFC 6838 in lowercase.
  An uppercase or parameterised value is refused, never normalised.
- **C4.** `File.filename` is `None` or a non-empty `str`.
  Path separators are allowed; a wire that cannot carry them replaces them (§6).
- **C5.** `UserMessage.content` is a tuple whose items are `Text` or `File`; it may be empty.
  A `str` or a list is refused.

A wrong type raises `TypeError`, a wrong value `ValueError`.

`data_url()` returns `data:<mime_type>;base64,<data in standard base64>`.

`File.from_path(path, mime_type=None)`:

1. Takes `filename` from the path's last component: `File.from_path('scans/march.pdf')` has `filename='march.pdf'`.
2. Takes `mime_type` as given, or guesses it from the filename with Python's built-in extension table, `mimetypes.MimeTypes().guess_type(filename)`.
   That table does not read the host's `mime.types` files, so the guess does not depend on the host.
   A filename the table does not know (`notes`, and on Python 3.12 `chart.webp` and `memo.docx`) raises `ValueError` naming the path and the `mime_type` argument.
   A compression suffix (`report.pdf.gz`, which the table reports as `application/pdf` with encoding `gzip`) raises `ValueError` too, since the bytes are not of the guessed type.
3. Opens the path through the file-access seam, `bro.files.access.current().open_read`, and reads the whole file.
   The seam's refusals propagate: `FileAccessDenied`, the `ValueError` for a path that is not a regular file, and `FileNotFoundError`.

The type comes from the name or the caller, never from the bytes.
The model gate checks the declared type;
the vendor checks the bytes (DeepSeek documents that it detects the format from the content).
`from_path` sets no size bound: the file is read into memory, and the vendor's limit applies when it is sent.

## 4. Recipes

`LLMSpec` (`bro/llm/llm.py`) gains the model-inputs table, the flag and the checks that read them.

```python
@dataclass(frozen=True)
class LLMSpec(ABC):
  MODEL_INPUTS: ClassVar[Mapping[str, frozenset[str]]]
  passthrough: bool = field(default=False, kw_only=True)

  def admitted(self) -> bool: ...
  def accepts(self, mime_type: str) -> bool: ...
  def dump(self) -> dict: ...
  def _dump_impl(self) -> dict: ...  # abstract
```

- **R1.** `MODEL_INPUTS` maps each listed model id to its model inputs.
  Every provider recipe class defines it.
  The recipe's model is `selection()['model']`; `None` means the model is deferred to the session, which only Anthropic recipes allow (`deferred_fields`).
- **R2.** `accepts(mime_type)` is true for a passthrough recipe;
  otherwise it is true when the model is listed and `mime_type` is in its model inputs;
  otherwise it is false.
  It is defined once, in the base class: DeepSeek's and echo's overrides are deleted.
- **R3.** `admitted()` is rule A1 (§5).
- **R4.** `dump()` is concrete in the base class:
  it returns `_dump_impl()`, which each provider implements in place of today's `dump()`, plus `"passthrough": true` when the flag is set.
  A recipe without the flag dumps exactly as today.
  `from_dict()` builds the recipe through `_from_dict_impl()` as today, then applies a `passthrough` key when present;
  a value that is not a JSON boolean raises `TypeError`.

Loading a declaration (`providers.declaration`) also validates the table:
`MODEL_INPUTS` is a mapping from `str` to `frozenset` of MIME types valid under C3, and `DEFAULT_MODEL` and every value of `MODELS` are keys of it.
A declaration that fails is unavailable, as a broken declaration is today.

The tables, from each vendor's documentation as read on 2026-10-31;
each table's source comment names the pages it was copied from, and a change of vendor documentation is copied by hand, as the price tables are:

| provider | model | model inputs |
|---|---|---|
| `openai` | `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-astra`, `gpt-5.6-terra`, `gpt-5.6-luna` | `image/png`, `image/jpeg`, `image/webp`, `image/gif`, `application/pdf`, and every MIME type the file-inputs guide's full list names for `input_file` |
| `deepseek` | `deepseek-v4-pro` | none |
| `deepseek` | `deepseek-flash` | `image/jpeg`, `image/png`, `image/gif`, `image/webp` |
| `anthropic` | `claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-fable-5`, `claude-fable-5-1`, `claude-haiku-4-5-20251001`, `claude-haiku-5-5` | `image/jpeg`, `image/png`, `image/gif`, `image/webp`, `application/pdf` |
| `echo` | `echo` | none |

Sources:
OpenAI's model page for each model ("Input modalities: text, image"), its images guide (PNG, JPEG, WebP, non-animated GIF)
and its file-inputs guide (PDF parsing needs a vision-capable model, which all five are; the other document types are text-extracted with no stated model requirement);
DeepSeek's models and pricing page (Vision "✓" for `deepseek-flash`, "Not supported" for `deepseek-v4-pro`) and its vision guide (JPEG, PNG, GIF, WebP; no file input documented);
Anthropic's vision and PDF-support pages, whose supported-model lists include all eight models (JPEG, PNG, GIF, WebP; `application/pdf`).

A MIME type cannot say whether a GIF is animated:
an animated GIF passes the model gate for OpenAI, which refuses it, and for Anthropic, which reads its first frame.

## 5. Admission

- **A1.** A recipe is admitted when it is a passthrough recipe, its model is deferred, or its model is listed.
- **A2.** `providers.require_admitted(recipe)` raises `LLMSelectionError` for a recipe that is not admitted, naming the model, the provider's short names and the `+passthrough` suffix (§7).
  It is enforced at two points:
  - `providers.resolve` checks its result.
    Every launch surface resolves its recipe there (`Harness.resolve_recipe` for the native and Claude harnesses, `ride`'s authority scope, `mu`),
    so an unlisted model fails before a session or a container starts.
  - `wires.open` checks the recipe before it constructs a client.
    Every model call of the native loop or of `mu` opens its client there, including recipes rebuilt from a record (a fork's trail header, `--resolved-llm`) that never pass through resolution.
- **A3.** `providers.resolve_model` keeps its rules (a short name, else a full id, else the name verbatim if it fits the provider's `MODEL_ID`).
  Admission runs after the selection's model and flags are applied, so `openai:gpt-7+passthrough` is admitted and `openai:gpt-7` is not.
- **A4.** Reading a recipe never checks admission:
  loading, displaying and pricing a recorded recipe of a model since delisted keeps working;
  a fork of it is refused at `wires.open` unless the fork's own selection names a listed model or `+passthrough`.

## 6. Wire encoding

- **W1.** A wire client encodes a user message in two steps.
  First it calls `require_accepted(recipe, content)` (`bro/llm/wire.py`), the model gate, which raises `UnsupportedContent("<mime type> content is not supported by <model>")` for the first `File` the recipe does not accept.
  Then it encodes each item; an item its protocol cannot carry raises `UnsupportedContent`, the protocol gate.
  Both raise before any request is sent.
- **W2.** A message whose content is exactly one `Text` is encoded as plain string content;
  any other content is encoded as a list of encoded items.
  So every text-only turn of the native loop sends the request it sends today.
- **W3.** A wire reads no file:
  the Responses client's PNG-only path reading and the Chat client's extension-guessed path reading are deleted, since a `File` carries its bytes.

| item | Responses (`wires/responses`) | Chat (`wires/chat`) | echo |
|---|---|---|---|
| `Text` | `input_text` | `text` | joined with the other `Text` items by `\n` |
| `File`, `image/*` | `input_image` with `image_url` the data URL and `detail` `auto` | `image_url` with the data URL and `detail` `auto` | refused by the model gate (echo accepts none) |
| other `File` | `input_file` with `file_data` the data URL, `filename` with `/` and `\` replaced by `_` and omitted when `None`, and `detail` `auto` | refused by the protocol gate: Chat Completions carries files only in OpenAI's dialect, and no Chat-wire recipe lists a non-image type | refused by the model gate |

The Responses encoder lives in `wires/responses/bro/wire_responses/content.py`;
`convert_content_part`, `convert_message`, `image_to_content`, `png_to_content` and `image_file_to_content` are deleted.

The Responses client sent images with `detail` `high` before this design, and the Chat client sent no `detail`, whose default DeepSeek does not document;
`auto` lets the vendor choose per image and per rendered PDF page:
OpenAI documents it as `high` for files on GPT-5.6 and later models, and DeepSeek as currently equal to `original`, the full-resolution image.
A setting on `File` that chooses the level is [#1048](https://github.com/dzhioev/bro/issues/1048)'s.

A restore replays a recorded `user_input` or `notification` step as `UserMessage((Text(body),))` through the same encoder;
the Responses client's hand-built replay item goes.

## 7. Selecting `passthrough`

- **G1.** The recipe grammar is `provider:model:effort` followed by the optional suffixes `+fast` and `+passthrough`, each at most once, in either order.
  `LLMSelection.format()` writes `+fast` before `+passthrough`.
  Any other suffix is refused with `LLMSelectionError`.
  A suffix alone applies to the standing recipe, as `+fast` does today: `+passthrough`.
- **G2.** `LLMSelection` gains `passthrough: bool`.
  `over()` combines it as it combines `fast`:
  a launch selection that names a provider is complete and keeps only its own flag;
  for any other launch selection, the result carries the flag when either the launch selection or the configured default does.
  `resolve()` sets the recipe's flag after the model and before admission (A3).
  A selection that names a provider starts from the provider's default recipe, so its flag comes from the selection alone;
  a selection that names only a model, an effort or a flag keeps the standing recipe's flag.
- **G3.** Every surface that takes, forwards or reports `fast` does the same for `passthrough`:
  the piece flag `--passthrough` beside `--fast` (`bro/launch/llm_flags.py`, excluded with `--llm` like the other piece flags);
  the summon payload (`bro/summon.py`);
  `ride`'s launch and `scope` options (`ride/ride/cli.py`);
  the authority scope's option, an open flag that is not overridable like `fast` (`ride/ride/authority_scope.py`), which reads the flag back from the resolved recipe's `dump()`;
  the forwarded argv of a session (`ride/ride/session.py`) and of a worker (`ride/ride/bro_worker.py`).
- **G4.** The names of the selection's options are written out separately at each of those surfaces.
  They are defined once in `bro/llm/providers.py`, with the flag-valued subset (`fast`, `passthrough`), and every surface derives its handling from that definition.
- **G5.** `mu`'s material names its recipes in the same grammar, so `"llm": "openai:gpt-7:low+passthrough"` works with no change to `mu`.

## 8. The native loop and the chat command

- **L1.** `LLM.send(inputs, *, request_timeout=None)` takes a sequence of `SystemPrompt` and `UserMessage` instead of role dicts.
  After a restore, `Loop` drops the `SystemPrompt` items of the first send, as it drops `system` dicts today;
  `_convert_message` is deleted.
- **L2.** The tracked `user_input` step body is the message's `Text` items joined by `\n`, as `_extract_text` computes today;
  a message of only `File` items records an empty body.
- **L3.** The runner sends `[SystemPrompt(system_prompt), UserMessage((Text(input),))]` for a run and `[UserMessage((Text(message),))]` for a later turn.
- **L4.** `bro.native.llm --attach PATH...` sends `UserMessage((Text(request), *(File.from_path(path) for path in paths)))`;
  a path `from_path` refuses fails the command with that error.
- **L5.** The test doubles that implement `LLM` (in `harnesses/native`, `bro`, `browser` and `ride` tests) follow the new signature.

## 9. `mu`

- **M1.** `mu(instructions, result, *content, role=None)` and `mu.aio(...)` take the instructions as the system message and the content items as the one user message.
  The instructions are the caller's rules for the call;
  the content is the material the rules apply to.
- **M2.** `Wire.structured(instructions, result, content, request_timeout)` takes `content: tuple[Content, ...]`.
  The Responses client sends the instructions and the user message encoded by W1 and W2;
  the Chat client appends the result's schema to the instructions, as it does today.
- **M3.** `bro/llm/mu.py` no longer defines `Content`, `Text`, `JSON`, `DateTimeEncoder` or `Markdown`;
  callers import `Text` and `File` from `bro.llm.wire`.
  The spell interpreter (`bro/spells.py`) sends `Text(json.dumps(request, indent=4))`.
  The other callers (`bro/datasources/searchable.py`, `browser/bros/browser/mcp.py`, `llms/deepseek/bro/deepseek/routes_test.py`) change their imports only.

## 10. Records and rollout

- A trail header records the recipe's `dump()`, so the `passthrough` key appears only on passthrough recipes.
- `user_input` step bodies stay text (L2).
  `File` content is not recorded in steps, so a restore or a fork does not replay it, as for an attached image today.
- A raw request record carries each wire's encoding, including the base64 data of files, as for images today.
- `TRAIL_FORMAT` does not change.
  The hosted trails server stores rows verbatim and never rebuilds a recipe, so this design needs no redeploy of it.
- An installation from before this design ignores a `passthrough` key in a recipe dump, and refuses the `+passthrough` suffix as an unsupported suffix.
  So configuration that names the suffix (host recipe lists, `--llm` presets, `mu` material) is written only after every installation reading it is re-synced.
- Once an installation is re-synced, its configured recipes that name an unlisted model stop launching.
  Before re-syncing, the operator checks the host's recipe lists and presets and the `mu` material's `llm` values,
  and moves each unlisted model to a listed one or adds `+passthrough` after the re-sync.
  This repository's own configuration and personas name only listed models.
- A root keeps the runtime it froze at start, and so do the sessions it summons:
  they run under the rules of that runtime until a root starts after the re-sync.

## 11. Failure behavior

| condition | raises | raised by | before |
|---|---|---|---|
| a recipe that is not admitted | `LLMSelectionError` | `providers.resolve`, `wires.open` (A2) | any session or request |
| a `File` the recipe does not accept | `UnsupportedContent` | `require_accepted` (W1) | the request |
| a `File` the recipe accepts and the wire cannot carry | `UnsupportedContent` | the client's encoder (W1) | the request |
| a content item or `UserMessage` breaking C1 to C5 | `TypeError` or `ValueError` | the class | it exists |
| `from_path` on an unknown or compressed name | `ValueError` | `File.from_path` | the file is opened |
| `from_path` on a refused, irregular or missing path | `FileAccessDenied`, `ValueError`, `FileNotFoundError` | the access seam | the file is read |
| a recipe string with an unknown suffix | `LLMSelectionError` | `providers.parse` | resolution |
| a dump whose `passthrough` is not a boolean | `TypeError` | `LLMSpec.from_dict` | the recipe exists |
| a declaration whose `MODELS` or `DEFAULT_MODEL` is unlisted, or whose table is malformed | `LLMSelectionError` (provider unavailable) | `providers.declaration` | the provider is used |
| what the vendor refuses (a passthrough recipe's content, an animated GIF on OpenAI) | the provider's API error | the vendor | — |

## 12. Examples

1. `mu(instructions, Totals, Text('Extract the totals.'), File.from_path('march.pdf'))` with `mu` material `openai:terra:low`:
   `from_path` gives `File(<bytes>, 'application/pdf', 'march.pdf')`;
   resolution admits `gpt-5.6-terra` (listed);
   `require_accepted` passes (`application/pdf` is in its inputs);
   Responses sends a system message and a user message `[input_text, input_file(file_data='data:application/pdf;base64,…', filename='march.pdf')]`.
2. The same call with material `deepseek:pro:high`:
   `require_accepted` raises `UnsupportedContent('application/pdf content is not supported by deepseek-v4-pro')`, and no request is sent.
3. On `deepseek:flash`, `File(png, 'image/png')` is sent as an `image_url` data URL;
   `File(pdf, 'application/pdf')` is refused by the model gate, since flash lists only images;
   on a Chat-wire recipe that listed `application/pdf`, the protocol gate would refuse it instead.
4. `File(data, 'image/tiff', 'scan.tiff')` on `openai:sol` is refused by the model gate;
   on `openai:sol+passthrough` it passes, Responses sends it as `input_image`, and OpenAI's refusal comes back as its API error.
5. `File(data, 'application/pdf', 'Payslip4/2026.pdf')` on Responses is sent with `filename='Payslip4_2026.pdf'`.
6. `--llm openai:gpt-7:high` raises `LLMSelectionError` at resolution;
   `--llm openai:gpt-7:high+passthrough` is admitted, and so is its piece-flag spelling, `--provider openai --model gpt-7 --effort high --passthrough`.
7. A fork of a trail recorded on `gpt-5.5`, which is not listed:
   the header's recipe is rebuilt and displayed, and the fork is refused at `wires.open`;
   a fork whose selection names a listed model (`--llm openai:terra`) or the flag (`--llm +passthrough`) proceeds.
8. On the Claude harness, `--llm anthropic` with the model left to the session is admitted, since its model is deferred;
   `--llm anthropic:claude-opus-6` is refused unless it carries `+passthrough`.
9. `File.from_path('report.pdf.gz')` and `File.from_path('notes')` raise `ValueError`;
   `File.from_path('chart.webp', 'image/webp')` succeeds.

## 13. Decision record

D1 to D8 were decided by the owner in the discussion; P1 to P11 were raised by the design and settled by the owner with its approval on 2026-10-31.

| id | decision | status |
|---|---|---|
| D1 | content items `Text` and `File` in the wire port, `UserMessage.content` a tuple of them, no dict parts (§3) | settled |
| D2 | `File(data, mime_type, filename=None)` and `File.from_path` (§3) | settled |
| D3 | model inputs per model and MIME type, copied from vendor documentation; `Text` always accepted (§4) | settled |
| D4 | unlisted models refused by default; the model gate always runs (§5, §6) | settled |
| D5 | the `passthrough` flag admits unlisted models and accepts every MIME type; `accepts` keeps its name (§4, §5) | settled |
| D6 | `JSON`, `DateTimeEncoder` and `Markdown` deleted from `mu` (§9) | settled |
| D7 | the flag is named `passthrough` (§4, §7) | settled |
| D8 | every wire sends `detail` `auto` for the image and file content it carries (§6) | settled |
| P1 | `mu`'s first argument named `instructions` (§9) | settled |
| P2 | `LLM.send` takes typed inputs instead of role dicts (§8) | settled |
| P3 | `from_path` reads through the access seam, guesses with Python's built-in table, and refuses unknown and compressed names (§3) | settled |
| P4 | an empty `File` refused (C2) | settled |
| P5 | the table is a class attribute of the recipe, covering `MODELS` and `DEFAULT_MODEL`, validated when the declaration loads (§4) | settled |
| P6 | admission enforced at `providers.resolve` and `wires.open` (A2) | settled |
| P7 | `+passthrough` suffix, `--passthrough` piece flag and an open authority option, mirroring `fast` (§7) | settled |
| P8 | the selection's option names defined once (G4) | settled |
| P9 | a single-`Text` message encoded as string content (W2) | settled |
| P10 | the Chat wire carries only image files (§6) | settled |
| P11 | OpenAI's inputs include every documented `input_file` type, not only PDF (§4) | settled |

## 14. Unverified assumptions

- **X1.** Whether Responses accepts an `input_file` with `file_data` and no `filename`: the guide does not say, and all its examples send one.
  A `File` without a filename is sent without one (§6); a live probe settles it.
- **X2.** Whether DeepSeek's Chat endpoint accepts list content for a text-only message.
  W2 keeps single-`Text` messages off it;
  a `mu` call with several `Text` items on DeepSeek sends a list, as every `mu` call does today.
- **X3.** OpenAI's full `input_file` list is copied from the guide's "Full list of accepted file types" when the table is written;
  this document names only its categories.

## 15. Documentation

- `bro/llm/AGENTS.md`: the `wire.py` entry (content items, `require_accepted`), the `mu.py` entry, and the `providers.py` sentence calling the short-name table "a convenience, not a whitelist".
- `bro/reference/llm_providers.md`: the provider contract (`MODEL_INPUTS`, admission, `passthrough`, no `accepts` override),
  the client contract (encode `Text` and `File`, call `require_accepted`, the protocol gate), and the same "not a whitelist" sentence.
- `bro/llm/providers.py`: the `resolve_model` docstring.
- `wires/responses/AGENTS.md`: what `content.py` encodes.
- `bro/reference/ride.md`: the LLM selection flags and the recipe grammar.
- `bro/reference/permissions.md`: the `passthrough` row of the bro option table.
- `bro/launch/llm_flags.py`: the `--llm` and `--passthrough` help.
- Any other page naming a deleted or renamed symbol, found by grep when the change is made.
