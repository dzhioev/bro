"""Credential kinds used by benchmark workflows."""

HARBOR = {
  'description': 'Harbor API credentials',
  'install': {'env': {'HARBOR_API_KEY': {'secret': '{{insert #name}}'}}},
}

RETENTION = {'description': 'Benchmark retention storage configuration'}
