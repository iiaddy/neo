"""Session forks: copy-on-write clone of a JSONL session.

Like opencode's fork — a new session row with a ``forked_from`` parent id —
but adapted to neo's JSONL SessionStore: all records are deep-copied into a
new ``<new_sid>.jsonl`` file, self-references to the old sid are remapped to
the new sid, and a fresh meta record is prepended. Every other ``ses_`` id
referenced in the payloads (``forked_from`` parents, subagent sessions, ...)
is preserved, so fork-of-fork chains keep valid parent links. The original
session is untouched. Pure function on the store, no UI.
"""
from __future__ import annotations

import json
import re
import time

from ..agent.session import _new_id, SessionStore


def fork_session(store: SessionStore, sid: str) -> str:
    """Clone session ``sid`` into a brand-new session id and return it.

    Only the session's own id is remapped to the new id; every other
    ``ses_`` id referenced in the payloads (``forked_from`` parents,
    subagent sessions, ...) is preserved, so fork-of-fork chains keep
    valid parent links. Raises ValueError if the session has no records.
    """
    records = store.load(sid)
    if not records:
        raise ValueError(f"unknown session: {sid}")

    new_sid = _new_id("ses")
    # Self-references (payloads pointing at the forked session itself)
    # resolve to the new session id. Word boundaries keep longer tokens
    # that merely contain the sid as a substring untouched.
    self_ref = re.compile(r"\b" + re.escape(sid) + r"\b")

    def remap(rec: dict) -> dict:
        text = json.dumps(rec, ensure_ascii=False)
        return json.loads(self_ref.sub(new_sid, text))

    title = ""
    for rec in records:
        if rec.get("t") == "meta":
            title = rec.get("title", "") or ""
            break

    store.append(new_sid, {
        "t": "meta",
        "title": f"{title} (fork)" if title else "fork",
        "forked_from": sid,
        "created_at": time.time(),
    })
    for rec in records:
        if rec.get("t") == "meta":
            continue  # original meta is replaced by the fresh one above
        store.append(new_sid, remap(rec))
    return new_sid
