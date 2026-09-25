---
description: Review code changes (diff, commit, branch, or PR) and report findings by severity.
---

Review the following changes in this codebase:

$ARGUMENTS

If no target is given above, review the uncommitted working-tree diff
(`git diff` plus staged changes). Otherwise the target may be a commit
SHA, a branch name, or a PR reference — resolve it with git and review
that range.

Review for, in this order:
1. Correctness — logic bugs, off-by-ones, wrong assumptions, missed edge
   cases.
2. Security — injection, auth gaps, secret handling, unsafe shell or
   path use.
3. Robustness — error handling, resource cleanup, concurrency hazards.
4. Clarity — misleading names, dead code, comments that lie.

Rules:
- Read every changed file in full before judging; cite file:line.
- Distinguish blocking findings from nits. No flattery, no filler.
- If the change looks correct, say so in one line and stop.

Output: verdict (ship / revise), then findings ordered by severity with
concrete, actionable suggestions.
