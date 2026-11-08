AUTH_KIND = 'claude_code'

KIND = {
  'description': 'Claude Code OAuth token',
  'install': {
    'env': {
      'CLAUDE_CODE_OAUTH_TOKEN': {'secret': '{{insert #name}}'},
    },
  },
}
