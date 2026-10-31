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

Settled by the owner on 2026-10-31 (the decision record, §13, lists every decision with its status):

1. One content vocabulary, owned by the wire port, replaces the Chat Completions-shaped dicts that message content travels in today; `mu` takes it directly.
2. One file content class carries `data`, `mime_type` and an optional `filename`, with a constructor that reads a path and takes the type from the framework's own extension table.
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
- choosing a recipe when a workspace resumes, and the full selection grammar on `bro.native.llm`, which belong to the LLM-configuration work ([#1150](https://github.com/dzhioev/bro/issues/1150));
- size limits on files, where each vendor's own limit applies;
- identifying a file's type from its bytes.

Constraints:
core imports no other workspace member, and declaration modules import no SDK at module level (`bro/llm/AGENTS.md`);
recorded trails stay readable;
backward compatibility is not a constraint (`AGENTS.md`, "Conventions").

## 2. Concepts

- **Content item** — one element of a user message's content: a `Text` or a `File` (§3.1).
  `Content` is the union type.
  The response side's `Part` (`bro/llm/records.py`) is a different concept and keeps its name.
- **MIME type** — a string `type/subtype` in lowercase, without parameters: `image/png`, `application/pdf`.
- **Extension table** — the framework's map from a filename suffix to the MIME type `File.from_path` declares for it (§3.2).
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

### 3.1 Content items

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
- **C4.** `File.filename` is `None` or a non-empty `str`;
  a wire that carries a filename sends it as given (§6).
- **C5.** `UserMessage.content` is a non-empty tuple whose items are `Text` or `File`.
  A `str`, a list or an empty tuple is refused.

A wrong type raises `TypeError`, a wrong value `ValueError`.

`data_url()` returns `data:<mime_type>;base64,<data in standard base64>`.

`File.from_path(path, mime_type=None)`:

1. Takes `filename` from the path's last component: `File.from_path('scans/march.pdf')` has `filename='march.pdf'`.
2. Takes `mime_type` as given, or looks up the filename's last suffix, lowercased, in the extension table (§3.2):
   `Chart.PNG` declares `image/png`.
   A filename without a suffix, or with a suffix the table does not hold, raises `ValueError` naming the path and the `mime_type` argument:
   `notes`, `scan.tiff`, and `report.pdf.gz`, whose last suffix is `.gz`.
3. Opens the path through the file-access seam, `bro.files.access.current().open_read`, and reads the whole file.
   The seam's refusals propagate: `FileAccessDenied`, the `ValueError` for a path that is not a regular file, and `FileNotFoundError`.

The type comes from the name or the caller, never from the bytes.
The model gate checks the declared type;
the vendor checks the bytes (DeepSeek documents that it detects the format from the content).
`from_path` sets no size bound: the file is read into memory, and the vendor's limit applies when it is sent.

### 3.2 The extension table

`bro/llm/wire.py` holds the extension table as `EXTENSION_TYPES`, a mapping from suffix to MIME type.

- **E1.** Each key is a lowercase suffix with its leading dot, and each value is a MIME type valid under C3.
- **E2.** The table holds the suffixes the vendors' documentation names for content a listed model accepts (§4), each mapped to a MIME type that every vendor accepting that format lists.
  A suffix no single type serves at every vendor accepting its format stays out of the table, and its callers pass `mime_type`.
- **E3.** The table is copied by hand from the vendors' pages when they change, as the model-inputs tables are, and its source comment names the pages.
  Nothing else is consulted: neither Python's `mimetypes` tables nor the host's `mime.types` files, so a guess depends on neither the Python version nor the host.

The table, from the pages read on 2026-10-31
— OpenAI's images guide for the image suffixes and the "Full list of accepted file types" of its file-inputs guide for the rest:

| MIME type | suffixes |
|---|---|
| `image/png` | `.png` |
| `image/jpeg` | `.jpg`, `.jpeg` |
| `image/gif` | `.gif` |
| `image/webp` | `.webp` |
| `application/pdf` | `.pdf` |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | `.xlsx` |
| `application/vnd.ms-excel` | `.xla`, `.xlb`, `.xlc`, `.xlm`, `.xls`, `.xlt`, `.xlw` |
| `text/csv` | `.csv` |
| `text/tsv` | `.tsv` |
| `text/x-iif` | `.iif` |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `.docx` |
| `application/msword` | `.doc`, `.dot` |
| `application/vnd.oasis.opendocument.text` | `.odt` |
| `application/rtf` | `.rtf` |
| `application/vnd.openxmlformats-officedocument.presentationml.presentation` | `.pptx` |
| `application/vnd.ms-powerpoint` | `.pot`, `.ppa`, `.pps`, `.ppt`, `.pwz`, `.wiz` |
| `text/plain` | `.bat`, `.conf`, `.def`, `.dic`, `.in`, `.list`, `.log`, `.text`, `.txt` |
| `text/markdown` | `.markdown`, `.md` |
| `text/html` | `.htm`, `.html` |
| `text/xml` | `.xml` |
| `application/json` | `.json` |
| `text/css` | `.css` |
| `text/javascript` | `.js`, `.mjs` |
| `text/x-asm` | `.asm`, `.s` |
| `text/x-c` | `.c`, `.h` |
| `text/x-c++` | `.cc`, `.cpp`, `.cxx`, `.hh` |
| `text/x-perl` | `.pl` |
| `text/x-python` | `.py` |
| `text/x-sh` | `.ksh` |
| `text/x-sql` | `.sql` |
| `text/x-rst` | `.rst` |
| `message/rfc822` | `.eml`, `.mht`, `.mhtml`, `.mime`, `.nws` |
| `text/calendar` | `.ics`, `.ifb` |
| `text/x-vcard` | `.vcf` |
| `application/x-subrip` | `.srt` |
| `text/vtt` | `.vtt` |

That is 70 suffixes over 36 types.
The four image types and `application/pdf` carry the same names at every vendor that accepts them;
every other type is accepted by OpenAI's listed models alone, so it carries OpenAI's name:
`.tsv` is `text/tsv`, not `text/tab-separated-values`.
Where OpenAI's pages group a suffix under a category, its type comes from that category's list:
the presentation suffixes `.pwz` and `.wiz` are `application/vnd.ms-powerpoint`.

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
| `openai` | `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-astra`, `gpt-5.6-terra`, `gpt-5.6-luna` | `image/png`, `image/jpeg`, `image/webp`, `image/gif`, and the 126 types of the file-inputs guide's "Full list of accepted file types", `application/pdf` among them |
| `deepseek` | `deepseek-v4-pro` | none |
| `deepseek` | `deepseek-flash` | `image/jpeg`, `image/png`, `image/gif`, `image/webp` |
| `anthropic` | `claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-fable-5`, `claude-fable-5-1`, `claude-haiku-4-5-20251001`, `claude-haiku-5-5` | `image/jpeg`, `image/png`, `image/gif`, `image/webp`, `application/pdf` |
| `echo` | `echo` | none |

A vendor's list is copied lowercased, since C3 refuses uppercase:
OpenAI's full list spells both `text/x-R` and `text/x-r`, which the table holds once.

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
  It checks the recipe a run will drive, once every layer is applied, and never a recipe that is only resolved, discovered or read:
  - `ride`'s authority scope checks the recipe it settles for a managed start (`ride/ride/authority_scope.py`, `recipe`), after the host list pick and the launch selection are laid over the standing recipe.
    Every root start, resume and summon settles its recipe there, and so does `ride scope`, which therefore previews the refusal;
    an unlisted model fails before a session or a container starts.
  - The native launchers check the recipe an in-process `bro run` or `bro chat` drives (`harnesses/native/harness/bro/launch/run.py`), the persona's declaration under the selection or `--resolved-llm`, before the runner starts.
  - `wires.open` checks the recipe before it constructs a client.
    Every model call of the native loop or of `mu` opens its client there,
    so it is where `mu`'s material recipe is checked, right after `mu` resolves it,
    and it catches every recipe no launch checked, such as a trail header's recipe that `bro.fork.fork` rebuilds when its caller gives none.

  `providers.resolve`, `Harness.resolve_recipe` and the installation's discovery of each persona's standing recipe (`Installation.take` in `ride/ride/authority_reading.py`) check nothing.
  So a persona whose declaration names an unlisted model keeps a valid standing recipe:
  a host list entry or a launch selection that names a listed model, or adds `+passthrough`, settles an admitted recipe over it,
  and only a launch that lays nothing of the kind over it is refused.
- **A3.** `providers.resolve_model` keeps its rules (a short name, else a full id, else the name verbatim if it fits the provider's `MODEL_ID`).
  Admission runs after the selection's model and flags are applied, so `openai:gpt-7+passthrough` is admitted and `openai:gpt-7` is not.
- **A4.** Reading a recipe never checks admission:
  loading, displaying and pricing a recorded recipe of a model since delisted keeps working.
  A fork runs the recipe its caller supplies, which `wires.open` admits like any other:
  `bro chat <bro> --fork` runs the persona's declaration under the fork's selection, a managed continuation runs the recipe ride resolves for the resume (§10.2), and `bro.fork.fork` called without a recipe runs the trail header's.

## 6. Wire encoding

- **W1.** A wire client encodes a user message in two steps.
  First it calls `require_accepted(recipe, content)` (`bro/llm/wire.py`), the model gate, which raises `UnsupportedContent("<mime type> content is not supported by <model>")` for the first `File` the recipe does not accept.
  Then it encodes each item; an item its protocol cannot carry raises `UnsupportedContent`, the protocol gate.
  Both raise before any request is sent.
- **W2.** A message whose content is exactly one `Text` is encoded as plain string content;
  any other content, several items or any `File`, is encoded as a list of encoded items.
  So every text-only turn of the native loop sends the request it sends today.
- **W3.** A wire reads no file:
  the Responses client's PNG-only path reading and the Chat client's extension-guessed path reading are deleted, since a `File` carries its bytes.

| item | Responses (`wires/responses`) | Chat (`wires/chat`) | echo |
|---|---|---|---|
| `Text` | `input_text` | `text` | joined with the other `Text` items by `\n` |
| `File`, `image/*` | `input_image` with `image_url` the data URL and `detail` `auto` | `image_url` with the data URL and `detail` `auto` | refused by the model gate (echo accepts none) |
| other `File` | `input_file` with `file_data` the data URL, `filename` the `File`'s filename or `file` when it has none, and `detail` `auto` | refused by the protocol gate: Chat Completions carries files only in OpenAI's dialect, and no Chat-wire recipe lists a non-image type | refused by the model gate |

Responses refuses an `input_file` whose `file_data` comes without a `filename` (§14, X1).
The model does not see the name, and the vendor processes a file by its declared type,
so the placeholder `file` changes nothing but the request's validity.

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
  `resolve()` sets the recipe's flag after it applies the model, and admission reads the result (A2, A3).
  A selection that names a provider starts from the provider's default recipe, so its flag comes from the selection alone;
  a selection that names only a model, an effort or a flag keeps the standing recipe's flag.
- **G3.** Every surface that takes, forwards or reports `fast` does the same for `passthrough`:
  - the piece flag `--passthrough` beside `--fast` (`bro/launch/llm_flags.py`), excluded with `--llm` like the other piece flags, and with `--resolved-llm` (`harnesses/native/harness/bro/launch/run.py`);
  - the summon payload (`bro/summon.py`);
  - `ride`'s launch and `scope` options (`ride/ride/cli.py`);
  - the authority scope's option (`ride/ride/authority_scope.py`):
    an open flag that is not overridable and that a manual start refuses, like `fast`, read from the resolved recipe's `passthrough` field;
  - the forwarded argv of a session (`ride/ride/session.py`) and of a worker (`ride/ride/bro_worker.py`).

  The lowering of a resolved recipe back into start fields for `dive-in`'s task prefetch (`ride/ride/scope.py`, `scoped_secrets`) carries the flag too, though it carries no `fast`:
  the prefetch resolves those fields again, and without the flag it would refuse the unlisted model the launch admitted.

  Harbor's `model_name` reaches `providers.parse` and `LLMSelection.format()` as written (`benchmark/bro/benchmark/harbor_agent.py`), so it takes the suffix without a change of code;
  the trial's bundle reads it once rebuilt (§10.2).
- **G4.** The names of the selection's options, and their flag-valued subset (`fast`, `passthrough`), are defined once in `bro/llm/providers.py`;
  every surface in G3 derives from that definition what it registers, excludes, forwards, refuses and lowers.
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
  The command keeps its `--provider` and `--model` flags and takes no `passthrough`,
  so it runs listed models only, and `wires.open` refuses an unlisted one (A2).
- **L5.** The test doubles that implement `LLM` (in `harnesses/native`, `bro`, `browser` and `ride` tests) follow the new signature.

## 9. `mu`

- **M1.** `mu(instructions, result, *content, role=None)` and `mu.aio(...)` take the instructions as the system message and the content items as the one user message.
  The instructions are the caller's rules for the call;
  the content is the material the rules apply to.
  A call needs at least one content item (C5):
  `mu(instructions, Result)` raises `ValueError` and sends nothing.
- **M2.** `Wire.structured(instructions, result, content, request_timeout)` takes `content: tuple[Content, ...]`.
  The Responses client sends the instructions and the user message encoded by W1 and W2;
  the Chat client appends the result's schema to the instructions, as it does today.
- **M3.** `bro/llm/mu.py` no longer defines `Content`, `Text`, `JSON`, `DateTimeEncoder` or `Markdown`;
  callers import `Text` and `File` from `bro.llm.wire`.
  The spell interpreter (`bro/spells.py`) sends `Text(json.dumps(request, indent=4))`.
  The other callers (`bro/datasources/searchable.py`, `browser/bros/browser/mcp.py`, `llms/deepseek/bro/deepseek/routes_test.py`) change their imports only.

## 10. Records, other processes and rollout

### 10.1 Records

- A trail header records the recipe's `dump()`, so the `passthrough` key appears only on passthrough recipes.
- `user_input` step bodies stay text (L2).
  `File` content is not recorded in steps, so a restore or a fork does not replay it, as for an attached image today.
- A raw request record carries each wire's encoding, including the base64 data of files, as for images today.
- `TRAIL_FORMAT` does not change.

### 10.2 Contracts with other processes

Each contract below is shared with a process deployed or updated apart from the change:
another installation of the framework, a vendor's API, or the hosted trails server.
An installation is re-synced once it runs a revision carrying this design;
a root keeps the runtime it froze at its start, and so do the sessions it starts, so a root started before its installation's re-sync keeps the old rules until it ends.

- **Recipe strings with `+passthrough`** — in `~/.bro.json` (host presets and per-bro lists), a repository's `[tool.bro.llm]` presets, `mu` materials, and benchmark `model_name`s.
  An installation from before this design refuses the suffix as unsupported.
  A root reads `~/.bro.json` when it starts and grammar-checks every recipe in it,
  so one `+passthrough` anywhere in that file fails every root start of an older installation.
  A session's `mu` material is hydrated from the host store when the session starts and parsed at each call,
  so the suffix there fails the `mu` calls (cast, summaries, `look`) of every older session started after it is written, the children of an older root included.
  Order: the suffix is written only after every installation that reads the configuration is re-synced
  — every checkout on the host that starts roots, every other repository's framework pin that reads the host store, and the benchmark bundle
  — and every root started before its installation's re-sync has ended.
- **The benchmark bundle** is an installation of its own.
  Harbor forwards each trial's recipe to `ride solo --runtime-bundle` in the task container, which runs the bundle `benchmark bundle` built,
  and `cached()` reuses any complete bundle whatever commit built it.
  Each trial uploads the shared bundle when it installs (`BroAgent.install`), and a rebuild deletes the bundle before writing the new one,
  so a running job's later or retried trials would pick up whichever bundle is there when they install, or find it incomplete.
  Order: no Harbor job runs across the change.
  Running jobs end or are cancelled before the re-sync, the bundle is rebuilt after it, and only then do jobs start again;
  from the rebuild on, trials refuse unlisted models like any re-synced installation.
  A `+passthrough` recipe in a benchmark preset, or in a `mu` instance that trials hydrate (`mu+benchmark`), waits for the rebuild.
- **Recipe dumps with a `passthrough` key** — trail headers, `resolved_llm` (`RIDE_RESOLVED_LLM`, `--resolved-llm`), and benchmark retention records.
  An older reader ignores the key, since every provider's `_from_dict_impl` reads only its own keys,
  and runs the model as before, having no admission.
  No order is needed;
  within a ride, a dump crosses only processes of one runtime bundle.
- **Configured unlisted models** — a host list entry or preset, a `mu` material, a benchmark preset, a persona's declared recipe in another repository, and a kept workspace's recorded selection.
  A re-synced installation refuses every one of them that ends up as a run's recipe (A2), while an older installation keeps running them.
  Order: before an installation is re-synced, each unlisted model in the configuration it reads is moved to a listed one, which works on both versions;
  `+passthrough` instead follows the first contract's order.
  `ride resume` resolves a workspace's recorded selection again under the current runtime and takes no `--llm`,
  so a kept workspace started with an explicit unlisted model (`--llm openai:gpt-5.5`) does not resume after the re-sync.
  Its trail continues with `bro chat <bro> --fork <trail>`, whose selection can name a listed model or the recorded one with `+passthrough`, or the work moves to a new session.
  This repository's own configuration, benchmark presets and personas name only listed models.
- **The vendors' request shapes** — on 2026-10-31 Responses accepted each shape this design sends (§14):
  `input_file` with `filename` `file` and `detail` `auto`, `input_image` with `detail` `auto`, and string content for one `Text`.
  DeepSeek's API reference defines `image_url` with `detail` `auto` and text parts in list content.
- **The hosted trails server** reads nothing this design changes:
  its image carries trail formats and wire decode sides and no provider declaration, its projection decodes responses and reads text bodies,
  and this design changes neither a decode side nor a format.
  It is not redeployed.
- **The summon payload's `passthrough` field** is never read by another runtime:
  a root's launcher, its launch handler and every child it starts run the root's runtime bundle,
  and a manual child re-executes from the runtime of the root that prepared it.
- **The `mu` interface of extensions in other repositories** — its signature, `Text` and `File` imported from `bro.llm.wire`, and the deleted `JSON` —
  changes for a repository when it moves its framework pin;
  no process of one version calls another's.

### 10.3 Rollout order

1. Before the re-sync, every unlisted model named in `~/.bro.json`, in repository presets and in every `mu` instance moves to a listed one.
2. Every running Harbor job ends or is cancelled, and no new one starts until step 3 is done.
3. The installations re-sync, the benchmark bundle is rebuilt, and fresh roots start from them.
4. Other repositories move their framework pins when they adopt the content routes.
5. Only after the first contract's condition holds may `+passthrough` appear in shared configuration or a benchmark preset.

## 11. Failure behavior

| condition | raises | raised by | before |
|---|---|---|---|
| a run's recipe that is not admitted | `LLMSelectionError` | `ride`'s authority scope, the native launchers, `wires.open` (A2) | any session or request |
| a `File` the recipe does not accept | `UnsupportedContent` | `require_accepted` (W1) | the request |
| a `File` the recipe accepts and the wire cannot carry | `UnsupportedContent` | the client's encoder (W1) | the request |
| a content item or `UserMessage` breaking C1 to C5, such as a `mu` call without content | `TypeError` or `ValueError` | the class | it exists |
| `from_path` on a name without a suffix, or with a suffix the extension table does not hold | `ValueError` | `File.from_path` | the file is opened |
| `from_path` on a refused, irregular or missing path | `FileAccessDenied`, `ValueError`, `FileNotFoundError` | the access seam | the file is read |
| a recipe string with an unknown suffix | `LLMSelectionError` | `providers.parse` | resolution |
| a dump whose `passthrough` is not a boolean | `TypeError` | `LLMSpec.from_dict` | the recipe exists |
| a declaration whose `MODELS` or `DEFAULT_MODEL` is unlisted, or whose table is malformed | `LLMSelectionError` (provider unavailable) | `providers.declaration` | the provider is used |
| what the vendor refuses (a passthrough recipe's content, an animated GIF on OpenAI) | the provider's API error | the vendor | — |

## 12. Examples

1. `mu(instructions, Totals, Text('Extract the totals.'), File.from_path('march.pdf'))` with `mu` material `openai:terra:low`:
   the extension table gives `.pdf` the type `application/pdf`, so `from_path` gives `File(<bytes>, 'application/pdf', 'march.pdf')`;
   `wires.open` admits `gpt-5.6-terra` (listed);
   `require_accepted` passes (`application/pdf` is in its inputs);
   Responses sends a system message and a user message `[input_text, input_file(file_data='data:application/pdf;base64,…', filename='march.pdf', detail='auto')]`.
2. The same call with material `deepseek:pro:high`:
   `require_accepted` raises `UnsupportedContent('application/pdf content is not supported by deepseek-v4-pro')`, and no request is sent.
3. On `deepseek:flash`, `File(png, 'image/png')` is sent as an `image_url` data URL;
   `File(pdf, 'application/pdf')` is refused by the model gate, since flash lists only images;
   on a Chat-wire recipe that listed `application/pdf`, the protocol gate would refuse it instead.
4. `File(data, 'image/tiff', 'scan.tiff')` on `openai:sol` is refused by the model gate;
   on `openai:sol+passthrough` it passes, Responses sends it as `input_image`, and OpenAI's refusal comes back as its API error.
5. `File(pdf, 'application/pdf')`, which has no filename, is sent to Responses as an `input_file` with `filename='file'`;
   `File(pdf, 'application/pdf', 'scans/march.pdf')` is sent with `filename='scans/march.pdf'`.
6. `--llm openai:gpt-7:high` is refused with `LLMSelectionError` by the authority scope at launch;
   `--llm openai:gpt-7:high+passthrough` is admitted, and so is its piece-flag spelling, `--provider openai --model gpt-7 --effort high --passthrough`.
7. A `bro-dev` trail recorded on `gpt-5.5`, which is not listed:
   `rewind show` displays its header's recipe;
   `bro chat bro-dev --fork <trail>` continues it on `bro-dev`'s declared `gpt-6.1-sol`;
   `bro.fork.fork(trail, step)` called without a recipe rebuilds `gpt-5.5`, and `wires.open` refuses it.
8. On the Claude harness, a launch whose selection names no provider or model, such as `--llm ::xhigh`, runs the harness's session default with its model deferred, and is admitted for that reason;
   `--llm anthropic` names the provider and runs its default `claude-opus-5-5`, admitted as listed;
   `--llm anthropic:claude-opus-6` is refused unless it carries `+passthrough`.
9. `my-reader` declares `openai:gpt-7`, which is not listed:
   a managed launch with nothing laid over the declaration is refused at the authority scope;
   with `--llm :sol`, or with a host list entry `openai:terra`, it runs the listed model;
   with `--llm +passthrough` it runs `gpt-7`.
10. `File.from_path('report.pdf.gz')`, `File.from_path('notes')` and `File.from_path('scan.tiff')` raise `ValueError`;
    `File.from_path('Chart.WEBP')` declares `image/webp`, and `File.from_path('scan.tiff', 'image/tiff')` succeeds.
11. `mu(instructions, Result)`, with no content item, raises `ValueError` and sends nothing.

## 13. Decision record

D1 to D8 were decided by the owner in the discussion;
P1 to P11 were raised by the design and settled by the owner with its approval on 2026-10-31;
P12 to P19 were raised by the design's review and settled by the owner the same day, P17 amending P3 and P19 amending P6.

| id | decision | status |
|---|---|---|
| D1 | content items `Text` and `File` in the wire port, `UserMessage.content` a tuple of them, no dict parts (§3.1) | settled |
| D2 | `File(data, mime_type, filename=None)` and `File.from_path` (§3.1) | settled |
| D3 | model inputs per model and MIME type, copied from vendor documentation; `Text` always accepted (§4) | settled |
| D4 | unlisted models refused by default; the model gate always runs (§5, §6) | settled |
| D5 | the `passthrough` flag admits unlisted models and accepts every MIME type; `accepts` keeps its name (§4, §5) | settled |
| D6 | `JSON`, `DateTimeEncoder` and `Markdown` deleted from `mu` (§9) | settled |
| D7 | the flag is named `passthrough` (§4, §7) | settled |
| D8 | every wire sends `detail` `auto` for the image and file content it carries (§6) | settled |
| P1 | `mu`'s first argument named `instructions` (§9) | settled |
| P2 | `LLM.send` takes typed inputs instead of role dicts (§8) | settled |
| P3 | `from_path` reads through the access seam and refuses a name the extension table does not type (§3.1) | settled, amended by P17 |
| P4 | an empty `File` refused (C2) | settled |
| P5 | the table is a class attribute of the recipe, covering `MODELS` and `DEFAULT_MODEL`, validated when the declaration loads (§4) | settled |
| P6 | admission enforced before any session and before any request (A2) | settled, amended by P19 |
| P7 | `+passthrough` suffix, `--passthrough` piece flag and an open authority option, mirroring `fast` (§7) | settled |
| P8 | the selection's option names defined once (G4) | settled |
| P9 | a single-`Text` message encoded as string content (W2) | settled |
| P10 | the Chat wire carries only image files (§6) | settled |
| P11 | OpenAI's inputs include every documented `input_file` type, not only PDF (§4) | settled |
| P12 | a `File` without a filename is sent to Responses with `filename` `file` (§6) | settled |
| P13 | a wire sends a filename as given, path separators included (C4, §6) | settled |
| P14 | `UserMessage.content` is never empty, so a `mu` call needs content (C5, M1) | settled |
| P15 | `bro.native.llm` keeps its flags and runs listed models only (L4) | settled |
| P16 | a kept workspace whose recorded selection names an unlisted model does not resume after the re-sync (§10.2) | settled |
| P17 | `from_path` takes its type from the framework's extension table alone, never from Python's tables or the host's (§3.2) | settled |
| P18 | a `mu` man page documents the interface for extensions (§15) | settled |
| P19 | admission checks the recipe a run drives, at `ride`'s authority scope, the native launchers and `wires.open`, never a recipe that is only resolved or discovered, so a persona's unlisted declaration stays a standing recipe a listed selection or `+passthrough` can override (A2) | settled |

## 14. Assumptions and evidence

- **X1.** Whether Responses accepts an `input_file` with `file_data` and no `filename`: refuted on 2026-10-31.
  `gpt-5.6-luna` answered `400 missing_required_parameter` for a PDF, a plain-text and a Markdown file sent without one.
  An empty or extensionless `filename` was accepted;
  asked for the attached file's name, the model answered that it saw none;
  and processing followed the declared type, whatever the name:
  the same CSV bytes declared `text/csv` and `text/plain` were processed differently.
  P12 settles the encoding (§6).
- **X2.** Whether DeepSeek's Chat endpoint accepts list content for a text-only message: it does.
  DeepSeek's API reference defines a user message's `content` as a string or a non-empty array of content parts, text parts among them,
  and #1091's live verification ran cast and `look` through a DeepSeek-backed `mu`, whose every call sent its text as a list.
  C5 keeps the array non-empty.
- **X3.** OpenAI's full `input_file` list, read on 2026-10-31: 127 distinct types, one of them `text/x-R`, which the table holds lowercased as `text/x-r`, a type the list also names;
  126 types remain (§4).

Checked on 2026-10-31 and no longer assumed:
Responses accepted `input_image` with `detail` `auto`, `input_file` with `filename` `file` and `detail` `auto`, filenames containing `/` and `\`, and string content for one `Text`;
the extension table covers every suffix OpenAI's file-inputs guide documents, each mapped to a type its lists name, checked by script against the page.

Unverified until the implementation's live probe:
`deepseek-flash` taking an `image_url` data URL with `detail` `auto`, which DeepSeek's vision guide and API reference document but no key in reach of the review could send.

## 15. Documentation

- `bro/reference/mu.md`, a new page served as the man topic `mu` from `bro/datasources/references.py` beside `llm-providers`,
  and mounted with `man(mu)` by the `dev`, `lead` and `eyebro` personas, which mount `man(llm_providers)`:
  `mu`'s signature and roles, `Text`, `File`, `from_path` and the extension table, the model and protocol gates, and the errors.
  The `mu` credential's material stays in `bro/reference/permissions.md`, "Auxiliary LLM credentials", which the page links.
- `bro/llm/AGENTS.md`: the `wire.py` entry (content items, the extension table, `require_accepted`), the `mu.py` entry, which points at the man page,
  and the `providers.py` sentence calling the short-name table "a convenience, not a whitelist".
- `bro/reference/llm_providers.md`:
  - the provider contract (`MODEL_INPUTS`, admission, `passthrough`, no `accepts` override) and the same "not a whitelist" sentence;
  - the client contract (encode `Text` and `File`, call `require_accepted`, the protocol gate);
  - "Adding a model to an existing provider": a `MODEL_INPUTS` row copied from the vendor's pages, an unlisted model refused unless the recipe carries `+passthrough`, and a newly documented suffix added to the extension table;
  - "Operator adoption": selecting `+passthrough`, and the order of §10.3.
- `bro/llm/providers.py`: the `resolve_model` docstring.
- `wires/responses/AGENTS.md`: what `content.py` encodes.
- `bro/reference/ride.md`: the LLM selection flags, the recipe grammar, and the resume of a recorded unlisted model (P16).
- `bro/reference/permissions.md`: the `passthrough` row of the bro option table.
- `bro/launch/llm_flags.py`: the `--llm` and `--passthrough` help.
- `benchmark/AGENTS.md` and the docstring of `harbor_agent.llm_recipe`: Harbor's `model_name` grammar with `+passthrough`.
- Any other page naming a deleted or renamed symbol, found by grep when the change is made.
