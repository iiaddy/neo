"""Session forks: copy-on-write clone of a JSONL session.

Like opencode's fork — a new session row with a ``forked_from`` parent id —
but adapted to neo's JSONL SessionStore: all records are deep-copied into a
new ``<new_sid>.jsonl`` file, every ``ses_`` id found anywhere in the record
payloads is minted fresh (self-references to the old sid land on the new
sid), and a fresh meta record is prepended. The original session is untouched.
Pure function on the store, no UI.
"""
from __future__ import annotations

import json
import re
import time

from ..agent.session import _new_id, SessionStore

_SES_ID = re.compile(r"\bses_\d{14}_[a-z0-9]{4}\b")


def fork_session(store: SessionStore, sid: str) -> str:
    """Clone session ``sid`` into a brand-new session id and return it.

    Raises ValueError if the session has no records.
    """
    records = store.load(sid)
    if not records:
        raise ValueError(f"unknown session: {sid}")

    # Collect every ses_ id referenced anywhere in the payloads.
    ids: set[str] = set()
    for rec in records:
        ids.update(_SES_ID.findall(json.dumps(rec, ensure_ascii=False)))
    id_map = {old: _new_id("ses") for old in ids}

    new_sid = _new_id("ses")
    # Self-references (payloads pointing at the forked session itself)
    # resolve to the new session id, not to yet another fresh id.
    id_map[sid] = new_sid

    def remap(rec: dict) -> dict:
        text = json.dumps(rec, ensure_ascii=False)
        for old, new in id_map.items():
            text = text.replace(old, new)
        return json.loads(text)

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
