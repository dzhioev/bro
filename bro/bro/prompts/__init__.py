from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
  from collections.abc import Iterable

  from llm.mcp import Harness, Wire


class PromptLoader:
  """contained prompt-file loading rooted at one package directory."""

  def __init__(self, directory: Path):
    self.directory = directory.resolve()

  def _resolve(self, file_name: str) -> Path:
    path = (self.directory / file_name).resolve()
    if not path.is_relative_to(self.directory):
      raise ValueError(f'prompt name escapes the prompts directory: {file_name!r}')
    return path

  def get_prompt_path(self, file_name: str) -> Path:
    """return the contained path without reading it."""
    return self._resolve(file_name)

  def get_prompt(self, file_name: str, **kwargs) -> str:
    text = self._resolve(file_name).read_text()
    is_template = file_name.endswith('.template')
    if is_template and len(kwargs) == 0:
      raise ValueError(f'template {file_name} requires format arguments')
    if not is_template and len(kwargs) > 0:
      raise ValueError(
        f'{file_name} is not a template but got format arguments: {", ".join(kwargs)}'
      )
    if is_template:
      return text.format(**kwargs)
    return text


_loader = PromptLoader(Path(__file__).parent)
get_prompt_path = _loader.get_prompt_path
get_prompt = _loader.get_prompt


def hold_fragment(
  hold: str,
  *,
  harness: Optional['Harness'] = None,
  wire: Optional['Wire'] = None,
  creds: Optional['Iterable[str]'] = None,
) -> str:
  """render the hold fragment for `hold` — the one rendering path every
  injection site uses, so the `{{…}}` directives in the hold text never leak
  unrendered. `hold.md` selects the per-level file on the `#hold` fact,
  which only this call supplies: everything else renders hold-neutrally and a
  stray `#hold` directive there raises.
  """
  import llm.mcp  # lazy: keeps the loader importable without the llm layer

  return llm.mcp.render_text(
    get_prompt('hold.md'), hold=hold, harness=harness, wire=wire, creds=creds
  ).strip()
