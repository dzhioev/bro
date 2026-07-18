{{iff #hold = unattended}}
{{include holds/unattended.md}}
{{eliff #hold = detached}}
{{include holds/detached.md}}
{{eliff #hold = attended}}
{{include holds/attended.md}}
{{eliff #hold = guided}}
{{include holds/guided.md}}
{{end}}
