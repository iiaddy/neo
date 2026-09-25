"""Fuzzy matching for the `/` command palette and `@` file completion."""
from __future__ import annotations


def fuzzy_score(query: str, target: str) -> float | None:
    """Subsequence fuzzy score; None when query is not a subsequence of target.

    Higher is better. Rewards prefix matches, word-boundary hits and
    compact match spans.
    """
    q, t = query.lower(), target.lower()
    if not q:
        return 1.0
    ti = 0
    score = 0.0
    first = last = -1
    for qi, ch in enumerate(q):
        pos = t.find(ch, ti)
        if pos == -1:
            return None
        if qi == 0:
            first = pos
            if pos == 0:
                score += 3.0
            elif pos > 0 and t[pos - 1] in " _-/":
                score += 2.0
        else:
            if pos == last + 1:
                score += 1.5  # contiguous run
            elif last >= 0 and t[last + 1:pos].strip(" _-/") == "":
                score += 0.75  # skipped only separators
        last = pos
        ti = pos + 1
        score += 1.0
    span = last - first + 1
    score += max(0.0, 4.0 - span * 0.2)
    return score


def fuzzy_filter(query: str, items: list[str], limit: int = 12) -> list[str]:
    """Return up to `limit` items matching query, best first."""
    scored = []
    for item in items:
        s = fuzzy_score(query, item)
        if s is not None:
            scored.append((s, item))
    scored.sort(key=lambda p: (-p[0], p[1]))
    return [item for _, item in scored[:limit]]
