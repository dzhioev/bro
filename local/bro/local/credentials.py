"""Credential kinds owned by this checkout's local integrations."""

HARBOR = {
  'description': 'Harbor API credentials',
  'install': {'env': {'HARBOR_API_KEY': {'secret': '{{insert #name}}'}}},
}
