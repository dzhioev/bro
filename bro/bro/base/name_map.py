from collections.abc import Iterator, Mapping


def _normalize(s: str) -> str:
  return s.strip().casefold()


class NameMap[V]:
  """case-insensitive, whitespace-tolerant name → value lookup.

  Useful when an LLM (or human) emits a free-form name that needs to be matched
  against a known set. Keys and queries are normalised via strip() + casefold()
  before lookup, so "Therapy ", "therapy", and "Therapy" all resolve to the
  same value.

  Strict by design: no prefix or fuzzy matching — only normalised exact match.
  Two source keys whose normalised forms collide are a data bug and raise
  ValueError at construction.
  """

  def __init__(self, items: Mapping[str, V]):
    self._by_normalized: dict[str, tuple[str, V]] = {}
    for name, value in items.items():
      normalized = _normalize(name)
      existing = self._by_normalized.get(normalized)
      if existing is not None:
        raise ValueError(
          f'duplicate names after normalisation: "{existing[0]}" and "{name}"'
        )
      self._by_normalized[normalized] = (name, value)

  def resolve(self, query: str) -> V:
    """return the value for query; raise LookupError on miss.

    The error message includes the available names so it can be fed back to a
    caller (e.g. an LLM) as actionable feedback.
    """
    entry = self._by_normalized.get(_normalize(query))
    if entry is None:
      available = ', '.join(sorted(orig for orig, _ in self._by_normalized.values()))
      raise LookupError(f'no match for "{query}" (available: {available})')
    return entry[1]

  def get(self, query: str, default: V | None = None) -> V | None:
    entry = self._by_normalized.get(_normalize(query))
    return entry[1] if entry is not None else default

  def __contains__(self, query: object) -> bool:
    if not isinstance(query, str):
      return False
    return _normalize(query) in self._by_normalized

  def __len__(self) -> int:
    return len(self._by_normalized)

  def __iter__(self) -> Iterator[str]:
    return (orig for orig, _ in self._by_normalized.values())

  def names(self) -> list[str]:
    return [orig for orig, _ in self._by_normalized.values()]
