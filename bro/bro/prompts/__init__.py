from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / 'prompts'


def get_prompt(file_name: str) -> str:
  return (_PROMPTS_DIR / file_name).read_text()
