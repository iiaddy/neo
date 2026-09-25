---
name: debugging
description: Systematic debugging — reproduce first, then bisect the cause instead of guessing.
---

# Debugging

## Process

1. **Reproduce** — get a failing case you can re-run (a test, a command, a script).
   If you can't reproduce it, say so and stop guessing.
2. **Narrow** — bisect: which change, which input, which code path triggers it?
   Read the stack trace bottom-up; add temporary prints only if needed.
3. **Hypothesize once** — form one theory, then test it. Don't stack theories.
4. **Fix at the cause** — patch the root cause, not the symptom. If the real fix
   is risky, say so and propose the minimal safe fix plus a follow-up.
5. **Verify** — re-run the reproduction, then the surrounding test suite.

## Rules

- Read error messages fully before acting on them.
- Check recent changes first (`git log`, `git diff`) — most bugs are new.
- Don't "fix" by silencing the error.
