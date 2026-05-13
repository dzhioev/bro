from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / 'prompts'


def get_prompt(file_name: str, **kwargs) -> str:
  text = (_PROMPTS_DIR / file_name).read_text()
  is_template = file_name.endswith('.template')
  if is_template and len(kwargs) == 0:
    raise ValueError(f'template {file_name} requires format arguments')
  if not is_template and len(kwargs) > 0:
    raise ValueError(f'{file_name} is not a template but got format arguments: {", ".join(kwargs)}')
  if is_template:
    return text.format(**kwargs)
  return text
