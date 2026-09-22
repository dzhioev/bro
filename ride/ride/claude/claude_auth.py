"""claude-session auth: the setup-token overlay."""

from bro.base import credentials, log

# auth env vars that outrank CLAUDE_CODE_OAUTH_TOKEN in claude's credential
# precedence: a value inherited from the launching shell would silently hijack
# the session's auth (an invalid one surfaces as a login/API-key error at the
# first call), so the launch scrubs them.
_OUTRANKING_AUTH_VARS = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')


def apply_claude_auth(env: dict[str, str], *, warn_when_missing: bool = False) -> None:
  """align a claude session env with the session auth model (reference/ride.md).

  scrubs the inherited vars that outrank the session's designated auth, then
  overlays the long-lived `claude setup-token` credential (`claude_code`). the
  session's private claude state (`ride/ride/claude/claude_config.py`) carries
  no OAuth credentials file, so the token is a managed session's whole auth —
  both launch surfaces gate on it before anything is created, and
  `warn_when_missing` diagnoses a runner that reaches this layer without the
  preflight contract. a managed session already carries the same var from the
  secret's registry install hook; re-applying it here is idempotent.
  """
  for var in _OUTRANKING_AUTH_VARS:
    if env.pop(var, None) is not None:
      log.verbose('scrubbed inherited %s from the claude session env', var)
  token = credentials.try_get('claude_code')
  if token is None:
    if warn_when_missing:
      log.warning(
        'claude_code secret not resolvable; the session starts unauthenticated — mint a '
        'token with `claude setup-token` and store it at %s',
        credentials.default_store().material_path('claude_code'),
      )
    return
  env['CLAUDE_CODE_OAUTH_TOKEN'] = token
