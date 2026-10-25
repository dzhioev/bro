"""Types and providers for snapshot-to-result tests."""

import hashlib
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from bro.files.confinement import SandboxReport
from bro.files.models import validate_payload
from bro.files.protocol import InvalidInput, Job, Reply
from bro.files.types import Limits


class Details(BaseModel):
  model_config = ConfigDict(extra='forbid')
  title: str
  page_count: int


class Page(BaseModel):
  page_number: int
  notes: list[str]
  text: str


class Content(BaseModel):
  pages: list[Page]


class Provider:
  def prepare(self) -> str:
    return 'snapshot-test 1'

  def info(self, snapshot: bytes, limits: Limits) -> dict[str, Any]:
    if snapshot == b'invalid':
      raise InvalidInput('damaged document; provide a readable document')
    if snapshot == b'engine':
      raise LookupError('private document contents')
    return {'title': hashlib.sha256(snapshot).hexdigest(), 'page_count': 2}

  def view(
    self, snapshot: bytes, view: str, pages: list[int] | None, limits: Limits
  ) -> dict[str, Any]:
    return {
      'pages': [
        {'page_number': number, 'notes': ['untrusted text'], 'text': snapshot.decode('utf-8')}
        for number in (pages if pages is not None else [1, 2])
      ]
    }


provider = Provider()


class StubRunner:
  def __init__(self):
    self.jobs: list[Job] = []

  async def run(self, job: Job) -> Reply:
    self.jobs.append(job)
    payload = (
      provider.info(job.snapshot, job.file_type.limits)
      if job.operation == 'info'
      else provider.view(job.snapshot, **job.arguments, limits=job.file_type.limits)
    )
    model = (
      job.file_type.details
      if job.operation == 'info'
      else job.file_type.get_view(job.arguments['view']).content
    )
    return Reply(validate_payload(model, payload, label='stub'), 'stub', cast(SandboxReport, {}))
