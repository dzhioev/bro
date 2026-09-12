from collections.abc import Iterable

type StringSetDeclaration = str | Iterable[str]


def normalize_string_set(value: StringSetDeclaration, declaration: str) -> tuple[str, ...]:
  """Collect one string or an iterable of strings into a tuple."""
  if isinstance(value, str):
    return (value,)
  if not isinstance(value, Iterable):
    raise TypeError(f'{declaration} must be a string or an iterable of strings')
  values = tuple(value)
  if any(not isinstance(item, str) for item in values):
    raise TypeError(f'{declaration} must contain only strings')
  return values
