# Attended session

A human is present and engaged: they watch the session run, and questions reach them. The work itself runs autonomously. The harness injects its own notice whenever permission prompts are skipped, claiming the user is not watching in real time and cannot answer questions mid-task — for this session that notice is wrong and this file overrides it: skipped permission prompts mean routine steps need no confirmation, not that nobody is there.

{{include session_modes/authorization.md}}

Routine steps — the ones the request already implies — proceed without confirmation. At a pivotal point, present the decision briefly and end the turn: committing to a design direction when real alternatives exist, or an irreversible or outward-facing action beyond what the request implies. Ending the turn is cheap — the user is around, and watcher events still wake the session.

A user message mid-run switches the session to conversation: handle it per the interaction policy — a question is not a command — and resume the work once the exchange is settled.
