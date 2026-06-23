import pytest

from base.name_map import NameMap


class TestResolve:
  def test_exact_match(self):
    m = NameMap({'Home': 1, 'Work': 2})
    assert m.resolve('Home') == 1
    assert m.resolve('Work') == 2

  def test_case_insensitive(self):
    m = NameMap({'Home': 1})
    assert m.resolve('home') == 1
    assert m.resolve('HOME') == 1
    assert m.resolve('hOmE') == 1

  def test_trim_whitespace_in_query(self):
    m = NameMap({'Home': 1})
    assert m.resolve('  Home  ') == 1
    assert m.resolve('\thome\n') == 1

  def test_trim_whitespace_in_key(self):
    m = NameMap({'Therapy ': 1})
    assert m.resolve('Therapy') == 1
    assert m.resolve('therapy') == 1

  def test_cyrillic(self):
    m = NameMap({'паша каждый день': 1, 'Дом': 2})
    assert m.resolve('паша каждый день') == 1
    assert m.resolve('ПАША КАЖДЫЙ ДЕНЬ') == 1
    assert m.resolve('Паша Каждый День') == 1
    assert m.resolve('Дом') == 2
    assert m.resolve('дом') == 2
    assert m.resolve('ДОМ') == 2

  def test_miss_raises_lookup_error(self):
    m = NameMap({'Home': 1, 'Work': 2})
    with pytest.raises(LookupError, match='no match for "Garden"'):
      m.resolve('Garden')

  def test_miss_lists_available_names(self):
    m = NameMap({'Home': 1, 'Work': 2})
    with pytest.raises(LookupError, match='available: Home, Work'):
      m.resolve('Garden')

  def test_no_substring_match(self):
    m = NameMap({'Therapy': 1})
    with pytest.raises(LookupError):
      m.resolve('Therap')
    with pytest.raises(LookupError):
      m.resolve('Therapy session')

  def test_empty_map(self):
    m = NameMap[int]({})
    with pytest.raises(LookupError, match='available: '):
      m.resolve('anything')


class TestDuplicateAtConstruction:
  def test_same_casing(self):
    with pytest.raises(ValueError, match='duplicate names'):
      NameMap({'Home': 1, 'home': 2})

  def test_trailing_whitespace(self):
    with pytest.raises(ValueError, match='duplicate names'):
      NameMap({'Home': 1, 'Home ': 2})

  def test_cyrillic_case(self):
    with pytest.raises(ValueError, match='duplicate names'):
      NameMap({'Ремонт': 1, 'РЕМОНТ': 2})


class TestGet:
  def test_hit(self):
    m = NameMap({'Home': 1})
    assert m.get('home') == 1

  def test_miss_returns_default(self):
    m = NameMap({'Home': 1})
    assert m.get('Garden') is None
    assert m.get('Garden', 42) == 42


class TestContains:
  def test_hit(self):
    m = NameMap({'Home': 1})
    assert 'Home' in m
    assert 'home' in m
    assert '  HOME  ' in m

  def test_miss(self):
    m = NameMap({'Home': 1})
    assert 'Garden' not in m

  def test_non_string(self):
    m = NameMap({'Home': 1})
    assert 1 not in m
    assert None not in m


class TestIteration:
  def test_iter_yields_originals(self):
    m = NameMap({'Home': 1, 'Therapy ': 2})
    assert sorted(m) == ['Home', 'Therapy ']

  def test_names(self):
    m = NameMap({'Home': 1, 'Therapy ': 2})
    assert sorted(m.names()) == ['Home', 'Therapy ']

  def test_len(self):
    assert len(NameMap({'a': 1, 'b': 2, 'c': 3})) == 3
    assert len(NameMap[int]({})) == 0
