#!/usr/bin/env python
import pytest

import cw
from dive_in import _pick_fresh_name, _resolve_task_id

UUID = '35ad38d8-5a6d-81ea-bce6-e4caf17ece7f'
HEX = '35ad38d85a6d81eabce6e4caf17ece7f'


class TestResolveTaskId:
  def test_uuid(self):
    assert _resolve_task_id(UUID) == UUID

  def test_notion_so_url(self):
    url = f'https://www.notion.so/workspace/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_notion_site_url(self):
    url = f'https://notion.site/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_url_with_query_string(self):
    url = f'https://www.notion.so/workspace/some-task-{HEX}?source=copy_link'
    assert _resolve_task_id(url) == UUID

  def test_url_with_shell_escaped_query_string(self):
    url = f'https://www.notion.so/workspace/some-task-{HEX}\\?source\\=copy_link'
    assert _resolve_task_id(url) == UUID

  def test_url_without_www(self):
    url = f'https://notion.so/workspace/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_http_url(self):
    url = f'http://www.notion.so/workspace/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_app_notion_com_url(self):
    url = f'https://app.notion.com/p/workspace/some-task-{HEX}?source=copy_link'
    assert _resolve_task_id(url) == UUID

  def test_notion_com_url(self):
    url = f'https://notion.com/workspace/some-task-{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_url_without_slug(self):
    url = f'https://www.notion.so/{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_workspace_prefixed_url_without_slug(self):
    url = f'https://www.notion.so/workspace/{HEX}'
    assert _resolve_task_id(url) == UUID

  def test_app_notion_com_url_without_slug(self):
    url = f'https://app.notion.com/p/workspace/{HEX}?source=copy_link'
    assert _resolve_task_id(url) == UUID

  def test_invalid_ref_raises(self):
    with pytest.raises(ValueError, match='--task must be a Notion URL or a UUID task ID'):
      _resolve_task_id('not-a-valid-ref')

  def test_bare_hex_raises(self):
    with pytest.raises(ValueError):
      _resolve_task_id(HEX)


@pytest.fixture
def fake_proj(monkeypatch, tmp_path):
  monkeypatch.setattr(cw, '_project_root', lambda: tmp_path)
  worktrees = tmp_path / '.claude' / 'worktrees'
  containers = tmp_path / 'var' / 'cw' / 'containers'
  worktrees.mkdir(parents=True)
  containers.mkdir(parents=True)
  return worktrees, containers


class TestPickFreshName:
  def test_base_when_unused(self, fake_proj):
    assert _pick_fresh_name('idea') == 'idea'

  def test_bumps_when_worktree_exists(self, fake_proj):
    worktrees, _ = fake_proj
    (worktrees / 'idea').mkdir()
    assert _pick_fresh_name('idea') == 'idea-2'

  def test_bumps_when_container_exists(self, fake_proj):
    _, containers = fake_proj
    (containers / 'idea').mkdir()
    assert _pick_fresh_name('idea') == 'idea-2'

  def test_walks_past_multiple_collisions(self, fake_proj):
    worktrees, containers = fake_proj
    (worktrees / 'idea').mkdir()
    (containers / 'idea-2').mkdir()
    (worktrees / 'idea-3').mkdir()
    assert _pick_fresh_name('idea') == 'idea-4'

  def test_namespaces_are_combined(self, fake_proj):
    # a host worktree blocks the container slug too, and vice versa.
    worktrees, containers = fake_proj
    (worktrees / 'idea').mkdir()
    (containers / 'idea-2').mkdir()
    assert _pick_fresh_name('idea') == 'idea-3'
