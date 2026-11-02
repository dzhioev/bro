"""A recipe and the source that selected it."""

import json
import os
from dataclasses import dataclass

from bro.llm.llm import Recipe
from bro.llm.providers import format_recipe

RESOLVED_RECIPE_ENV = 'RIDE_RESOLVED_RECIPE'
IN_PROCESS_SOURCE = 'in-process'


@dataclass(frozen=True)
class ResolvedRecipe:
  recipe: Recipe
  source: str

  def __post_init__(self) -> None:
    if not isinstance(self.source, str) or not self.source:
      raise ValueError('resolved recipe source must be a non-empty string')

  def dump(self) -> dict:
    return {'recipe': self.recipe.dump(), 'source': self.source}

  @classmethod
  def from_dict(cls, data: dict) -> 'ResolvedRecipe':
    if not isinstance(data, dict) or data.keys() != {'recipe', 'source'}:
      raise ValueError('resolved recipe must contain recipe and source')
    return cls(Recipe.from_dict(data['recipe']), data['source'])

  def encode(self) -> str:
    return json.dumps(self.dump(), separators=(',', ':'))

  @classmethod
  def from_environment(cls) -> 'ResolvedRecipe | None':
    encoded = os.environ.get(RESOLVED_RECIPE_ENV)
    return None if encoded is None else cls.from_dict(json.loads(encoded))

  def display(self) -> str:
    return f'{format_recipe(self.recipe)} ({self.source})'
