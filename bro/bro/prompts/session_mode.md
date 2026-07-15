{{iff #mode = unattended}}
{{include session_modes/unattended.md}}
{{eliff #mode = detached}}
{{include session_modes/detached.md}}
{{eliff #mode = attended}}
{{include session_modes/attended.md}}
{{eliff #mode = guided}}
{{include session_modes/guided.md}}
{{end}}
