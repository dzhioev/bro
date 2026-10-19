"""Prospective bro credential scopes and credential-store views."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bro.base import credentials
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from bro.harness import Harness
  from bro.llm.llm import LLMSpec
  from ride.harness import SessionHarness
  from ride.repository import Repository


class LaunchScopeError(Exception):
  pass


@contextlib.contextmanager
def launch_scope_errors() -> Iterator[None]:
  try:
    yield
  except (ValueError, credentials.SecretNotFound) as error:
    raise LaunchScopeError(str(error)) from error


@dataclass(frozen=True)
class ScopeRecipe:
  name: str
  harness: Harness
  auth_secret: str | None
  llm_key: bool


def launch_llm_spec(
  driver: SessionHarness, attachment: str | None, bro_name: str, llm: str | None
) -> LLMSpec:
  from ride.authority_launch import request
  from ride.authority_reading import Reading
  from ride.authority_scope import checked_recipe
  from ride.bro_worker import BroType
  from ride.repository import open_repository

  with launch_scope_errors():
    reading = Reading.take(None if attachment is None else open_repository(attachment))
    fields = {'target': bro_name, 'harness': driver.name}
    if llm is not None:
      fields['llm'] = llm
    requested, _ = request(BroType, fields, reading, reading.operator(), forward=False)
    descriptor = reading.rules.normalize(requested, reading)
    return checked_recipe(requested, descriptor, reading)


def scoped_secrets(
  bro_name: str,
  recipe: ScopeRecipe,
  *,
  attachment: str | None = None,
  attachment_repository: Repository | None = None,
  llm_spec: LLMSpec | None = None,
  cred: Sequence[str] = (),
  grant: Sequence[str] = (),
  revoke: Sequence[str] = (),
  recording: bool = True,
) -> ScopedSecrets:
  from ride.authority_launch import credentials_for, request
  from ride.authority_reading import Reading
  from ride.bro_worker import BroType
  from ride.repository import open_repository

  with launch_scope_errors():
    repository = attachment_repository or (
      None if attachment is None else open_repository(attachment)
    )
    reading = Reading.take(repository)
    fields = {
      'target': bro_name,
      'harness': recipe.harness.name,
      'pass': list(cred),
      'grant': list(grant),
      'revoke': list(revoke),
    }
    if llm_spec is not None:
      fields.update(provider=llm_spec.TYPE, **llm_spec.selection())
      fields = {name: value for name, value in fields.items() if value is not None}
    requested, _ = request(BroType, fields, reading, reading.operator(), forward=False)
    return credentials_for(reading.rules.start(requested, reading.operator(), reading))


def credential_store(scoped: ScopedSecrets) -> credentials.Store:
  return credentials.Store(credentials.default_registry(), credentials.STORE_DIR, scoped.selection)


def launch_view_store(scoped: ScopedSecrets) -> credentials.Store:
  with launch_scope_errors():
    return credentials.scoped_view_store(
      credential_store(scoped), scoped.required, optional=scoped.optional
    )
