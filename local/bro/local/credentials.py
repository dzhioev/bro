"""Credential kinds owned by this checkout's local integrations."""

HARBOR = {
  'sources': [{'file': 'harbor_api_key'}],
  'install': {'env': {'HARBOR_API_KEY': {'secret': '{{insert #name}}'}}},
}
