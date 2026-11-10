"""repository write access and explicitly trusted GitHub accounts."""

from pathlib import Path
from typing import Any
from urllib.parse import quote

from bro.extra.github import api


class Trust:
  def __init__(self, owner: str, repo: str):
    self._owner = owner
    self._repo = repo
    self._write_access: dict[str, bool] = {}
    self.logins: set[str] = set()

  def read(self, path: Path) -> None:
    self.logins = {
      line.strip().casefold() for line in path.read_text().splitlines() if line.strip()
    }

  def trusted(self, item: dict[str, Any], token: str) -> bool:
    login = item['user']['login'].casefold()
    if login in self.logins or item['author_association'] == 'OWNER':
      return True
    if login not in self._write_access:
      url = (
        f'https://api.github.com/repos/{self._owner}/{self._repo}'
        f'/collaborators/{quote(login, safe="")}/permission'
      )
      answer = api.get(url, token, absent_statuses=frozenset({403, 404}))
      self._write_access[login] = answer is not None and answer['permission'] in (
        'write',
        'admin',
        'maintain',
      )
    return self._write_access[login]


def untrusted(item: dict[str, Any], kind: str) -> dict[str, Any]:
  return {'user': item['user']['login'], 'kind': kind, 'url': item['html_url']}
