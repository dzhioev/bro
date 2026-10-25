"""Core result envelopes and validation of type-declared payloads."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from bro.files.text import sanitize, sanitize_strings, validate_strings
from bro.files.types import FileType


def validate_payload(
  model: type[BaseModel], value: dict[str, Any], *, label: str
) -> dict[str, Any]:
  try:
    validate_strings(value)
    result = model.model_validate(sanitize_strings(value), strict=True).model_dump(
      mode='json', by_alias=False
    )
    if not isinstance(result, dict):
      raise ValueError('declared payload must be an object')
    validate_strings(result)
    if sanitize_strings(result) != result:
      raise ValueError('declared model introduced invisible characters')
    return result
  except (ValueError, TypeError):
    raise RuntimeError(
      f'invalid file reply: {label} does not satisfy its declared model or string rules'
    )


class ViewDescription(BaseModel):
  model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

  name: str
  about: str


class InfoResult(BaseModel):
  model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

  type: str
  size_bytes: int = Field(ge=0)
  sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
  views: list[ViewDescription]
  details: dict[str, Any]

  @classmethod
  def from_validated(
    cls, file_type: FileType, *, size_bytes: int, sha256: str, details: dict[str, Any]
  ) -> Self:
    return cls(
      type=file_type.name,
      size_bytes=size_bytes,
      sha256=sha256,
      views=[
        ViewDescription(name=view.name, about=sanitize(view.about).text) for view in file_type.views
      ],
      details=details,
    )


class ViewResult(BaseModel):
  model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

  type: str
  sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
  view: str
  content: dict[str, Any]

  @classmethod
  def from_validated(
    cls, file_type: FileType, *, sha256: str, view: str, content: dict[str, Any]
  ) -> Self:
    file_type.get_view(view)
    return cls(
      type=file_type.name,
      sha256=sha256,
      view=view,
      content=content,
    )
