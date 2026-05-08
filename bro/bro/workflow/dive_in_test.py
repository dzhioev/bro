#!/usr/bin/env python
import pytest
from dive_in import _resolve_task_id

UUID = '35ad38d8-5a6d-81ea-bce6-e4caf17ece7f'
HEX = '35ad38d85a6d81eabce6e4caf17ece7f'


class TestResolveTaskId:
  def test_uuid(self):
    assert _resolve_task_id(UUID) == UUID

  def test_notion_so_url(self):
    url = f'https://www.notion.so/dzhioev/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_notion_site_url(self):
    url = f'https://notion.site/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_url_with_query_string(self):
    url = f'https://www.notion.so/dzhioev/some-task-{HEX}?source=copy_link'
    assert _resolve_task_id(url) == UUID

  def test_url_with_shell_escaped_query_string(self):
    url = f'https://www.notion.so/dzhioev/some-task-{HEX}\\?source\\=copy_link'
    assert _resolve_task_id(url) == UUID

  def test_url_without_www(self):
    url = f'https://notion.so/dzhioev/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_http_url(self):
    url = f'http://www.notion.so/dzhioev/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_invalid_ref_raises(self):
    with pytest.raises(ValueError, match='--task must be a Notion URL or a UUID task ID'):
      _resolve_task_id('not-a-valid-ref')

  def test_bare_hex_raises(self):
    with pytest.raises(ValueError):
      _resolve_task_id(HEX)
