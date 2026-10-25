from bros.browser import references


def test_scenarios_page_reads_without_rendering_errors():
  body = references.scenarios.read()
  assert '## Download a file' in body
