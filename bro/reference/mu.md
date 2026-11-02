# Structured one-shot calls

`mu` runs a stateless call on a configured recipe and returns a validated pydantic model.
Install the selected provider with its `client` extra.
The `mu` credential supplies complete recipe text and provider credential material, plus optional role-specific pairs;
see [Auxiliary LLM credentials](permissions.md#auxiliary-llm-credentials) for material and hydration rules.

## Interface

```python
from pydantic import BaseModel

from bro.llm.mu import mu
from bro.llm.wire import File, Text


class Totals(BaseModel):
  total: float


result = mu(
  'Extract the invoice total.',
  Totals,
  Text('Read this invoice.'),
  File.from_path('march.pdf'),
  role='reader',
)
```

`mu(instructions, result, *content, role=None)` blocks;
`await mu.aio(instructions, result, *content, role=None)` is the async interface.
`instructions` is the system message, `result` is the result model class, and the content items form one user message.
At least one content item is required, even for a text-only call.
For JSON input, pass `Text(json.dumps(value))` with the caller's desired serialization options.

`role` selects a named pair from the credential's `roles` mapping.
An absent or unknown role uses the default pair.
The framework uses `dispatch` for spell interpretation, `summary` for source summaries, and `reader` for focused page reading.
Each pair is bound with `bind_recipe`, without configuration layers or a harness.
A call never falls back to a different provider or key.

## Content

Import content from `bro.llm.wire`:

- `Text(text)` holds a string, including an empty string.
- `File(data, mime_type, filename=None)` holds nonempty bytes, a lowercase MIME type without parameters, and an optional nonempty filename.
  Names are passed as given, including path separators.
- `File.from_path(path, mime_type=None)` takes the basename as its filename and reads the whole file through `bro.files.access.current().open_read`.
  Without an explicit type, it looks up the last suffix, lowercased, in `EXTENSION_TYPES` in `bro.llm.wire`.
  That checked-in table follows the vendor guides, independently of the host and Python's MIME tables.
  For example, `Chart.WEBP` declares `image/webp`;
  `notes`, `scan.tiff` and `report.pdf.gz` require an explicit `mime_type`.
- `File.data_url()` returns a standard base64 data URL with the declared MIME type.

The declared type comes from the name or the caller, never from inspecting the bytes.
Files are read into memory without a framework size bound;
the vendor's limits apply to the request.

## Content gates and wire encoding

Every client checks each file against the recipe's per-model `MODEL_INPUTS` table before encoding content.
Text is always accepted.
A file a model does not accept raises `UnsupportedContent` naming its MIME type and the model, before any request.
The provider tables follow vendor documentation;
[llm-providers](llm_providers.md) describes their maintenance.

A second gate checks what the protocol can carry.
Responses sends images as `input_image` and other files as `input_file`;
a file with no name is sent with the filename `file`.
Chat sends image files as `image_url` and refuses non-image files even if the recipe's table accepts them.
Both protocols send files as data URLs with `detail: auto`.
The diagnostic echo wire accepts text only.
Messages has no structured-call client.

One `Text` is sent as string content;
several content items or any file use a content list, except echo, which joins text with newlines.
The call is independent of conversation state.
Native-loop records retain text input and raw wire requests, but do not replay file attachments on restore or fork.

## Errors

- Invalid content types raise `TypeError`;
  invalid values and empty content raise `ValueError`.
- An unknown suffix raises `ValueError` before opening the path, naming the `mime_type` argument.
  Missing files, irregular paths and access refusals propagate the file-access seam's errors.
- Malformed or unavailable `mu` configuration raises `ValueError` with a `mu credential` prefix.
- The model or protocol gate raises `bro.llm.llm.UnsupportedContent` before sending.
- An uncompleted response raises `bro.llm.llm.LLMStopped` with the neutral stop and vendor reason.
  Provider failures propagate after the shared transient-error retry policy.
- Completed replies that do not validate against `result` raise a validation error;
  unreadable replies and refusals raise a wire error.
