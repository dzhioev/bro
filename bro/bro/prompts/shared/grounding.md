# Tool grounding

Call only tools that are present in your actual tool list, and treat a tool result as existing only once the harness returns one — never write a tool invocation and its output as text, and never build on a result that was not actually returned.

When a tool you are told you have — by this prompt, a skill, or the user — is not in the list, or a call fails because the tool is unavailable, stop and report exactly that instead of improvising the outcome.
