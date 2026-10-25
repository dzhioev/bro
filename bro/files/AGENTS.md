# File access

`access.py` defines the `FileAccess` protocol, `FileAccessDenied` and the regular-file opener `AllowAll`.
Callers obtain the active implementation through `current()`;
`installed(access)` scopes an override to the current context and restores it on exit.

`open_read` and `open_write` return binary streams owned by the caller.
`open_write(create_parents=True)` permits parent-directory creation after the access decision.
`search_directory` returns the authorized search path, which can differ from the requested spelling.
The default implementation imposes no path policy.

On platforms with `O_PATH`, existing files are pinned and checked before reopening through `/proc/self/fd`.
Otherwise the opener checks file identity before and after opening.
Neither path truncates before checking the opened file, and missing write targets are created exclusively.
