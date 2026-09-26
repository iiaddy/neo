Generate a session title from the conversation so far.

Rules:
- At most 50 characters.
- Same language as the user's messages.
- Name the concrete task or topic (e.g. "Fix OAuth redirect loop"), not
  the tools used.
- No tool names, no quotes, no trailing period.
- Output only the title, nothing else.
