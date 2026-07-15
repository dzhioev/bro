from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
  from collections.abc import Iterable

  from llm.mcp import Harness, Wire

_PROMPTS_DIR = (Path(__file__).parent / 'prompts').resolve()


def _resolve(file_name: str) -> Path:
  path = (_PROMPTS_DIR / file_name).resolve()
  if not path.is_relative_to(_PROMPTS_DIR):
    raise ValueError(f'prompt name escapes the prompts directory: {file_name!r}')
  return path


def get_prompt_path(file_name: str) -> Path:
  """return the filesystem path of a prompt file (does not read it).

  used when a caller needs to hand the file off to something that wants a Path
  (e.g. `bro.datasources.file.FileSource`), not the text. Pair with `get_prompt`
  when you want the contents instead.
  """
  return _resolve(file_name)


def get_prompt(file_name: str, **kwargs) -> str:
  text = _resolve(file_name).read_text()
  is_template = file_name.endswith('.template')
  if is_template and len(kwargs) == 0:
    raise ValueError(f'template {file_name} requires format arguments')
  if not is_template and len(kwargs) > 0:
    raise ValueError(f'{file_name} is not a template but got format arguments: {", ".join(kwargs)}')
  if is_template:
    return text.format(**kwargs)
  return text


def mode_fragment(
  mode: str,
  *,
  harness: Optional['Harness'] = None,
  wire: Optional['Wire'] = None,
  creds: Optional['Iterable[str]'] = None,
) -> str:
  """render the session-mode fragment for `mode` — the one rendering path every
  injection site uses, so the `{{…}}` directives in the mode text never leak
  unrendered. `session_mode.md` selects the per-level file on the `#mode` fact,
  which only this call supplies: everything else renders mode-neutrally and a
  stray `#mode` directive there raises.
  """
  import llm.mcp  # lazy: keeps the loader importable without the llm layer

  return llm.mcp.render_text(
    get_prompt('session_mode.md'), mode=mode, harness=harness, wire=wire, creds=creds
  ).strip()
