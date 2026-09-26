"""JSONL session storage."""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path


def _new_id(prefix: str) -> str:
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    rand = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
    return f"{prefix}_{ts}_{rand}"


@dataclass
class SessionStore:
    root: Path = field(default_factory=lambda: Path.home() / ".neo" / "sessions")

    def __post_init__(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self.root / f"{sid}.jsonl"

    def new(self, title: str = "", model: str = "") -> str:
        sid = _new_id("ses")
        self.append(sid, {"t": "meta", "title": title, "model": model,
                          "created_at": time.time()})
        return sid

    def append(self, sid: str, record: dict) -> None:
        with open(self._path(sid), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load(self, sid: str) -> list[dict]:
        p = self._path(sid)
        if not p.is_file():
            return []
        out = []
        bad = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    bad += 1
        if bad:
            logging.warning("session %s: skipped %d corrupt JSONL line(s) in %s",
                            sid, bad, p)
        return out

    def list(self) -> list[dict]:
        items = []
        for p in sorted(self.root.glob("ses_*.jsonl"), reverse=True):
            title, model = "", ""
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("t") == "meta":
                        title = rec.get("title", title)
                        model = rec.get("model", model)
            except OSError:
                pass
            items.append({
                "id": p.stem,
                "title": title,
                "model": model,
                "updated_at": p.stat().st_mtime,
            })
        return items

    def set_title(self, sid: str, title: str) -> None:
        self.append(sid, {"t": "meta", "title": title})

    def messages_from_records(self, records: list[dict]) -> list[dict]:
        """Rebuild provider-neutral messages from stored records."""
        msgs: list[dict] = []
        for r in records:
            t = r.get("t")
            if t == "user":
                msgs.append({"role": "user", "content": r.get("text", "")})
            elif t == "assistant":
                msgs.append({"role": "assistant",
                             "content": r.get("text"),
                             "tool_calls": r.get("tool_calls", [])})
            elif t == "tool_result":
                msgs.append({"role": "tool", "tool_call_id": r.get("call_id", ""),
                             "name": r.get("tool", ""), "content": r.get("output", ""),
                             "is_error": r.get("is_error", False)})
        return msgs
