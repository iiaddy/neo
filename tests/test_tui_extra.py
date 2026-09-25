"""Data-logic tests for the TUI extras: themes, session dialog, model picker."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from neo.tui.themes import THEMES, theme_names
from neo.tui.themes_extra import EXTRA_THEMES
from neo.tui.session_dialog import filter_sessions, format_session, rename_session
from neo.tui.model_picker import (
    filter_models,
    format_model_row,
    load_favorites,
    load_recents,
    pick_model,
    push_recent,
    save_favorites,
)

# ---------------------------------------------------------------------------
# themes
# ---------------------------------------------------------------------------

REQUIRED_KEYS = set(next(iter(THEMES.values())).keys())


def test_extra_themes_key_sets_match():
    assert len(EXTRA_THEMES) == 6
    expected_names = {"neo-ocean", "neo-forest", "neo-sunset",
                      "neo-mono", "neo-raspberry", "neo-arctic"}
    assert set(EXTRA_THEMES) == expected_names
    for name, theme in EXTRA_THEMES.items():
        assert set(theme.keys()) == REQUIRED_KEYS, name
        for key, value in theme.items():
            assert isinstance(value, str) and value.startswith("#"), (name, key)


def test_extra_themes_mergable():
    # themes.py merges EXTRA_THEMES into THEMES at import (Option B), so the
    # extras are already present; re-merging must be idempotent.
    merged = {**THEMES, **EXTRA_THEMES}
    assert merged == THEMES
    for name in EXTRA_THEMES:
        assert name in THEMES
        assert name in theme_names()


# ---------------------------------------------------------------------------
# session dialog
# ---------------------------------------------------------------------------

SESSIONS = [
    {"id": "ses_001", "title": "Fix auth bug", "model": "anthropic/x", "updated_at": 0},
    {"id": "ses_002", "title": "Refactor TUI widgets", "model": "openai/y", "updated_at": 0},
    {"id": "ses_003", "title": "Auth store design", "model": "", "updated_at": 0},
]


def test_filter_empty_query_returns_all():
    assert filter_sessions(SESSIONS, "") == SESSIONS
    assert filter_sessions(SESSIONS, "   ") == SESSIONS


def test_filter_prefix_beats_subsequence():
    # "auth" is a prefix of "Auth store design" but a subsequence of "Fix auth bug"
    out = filter_sessions(SESSIONS, "auth")
    assert [s["id"] for s in out] == ["ses_003", "ses_001"]


def test_filter_matches_id():
    out = filter_sessions(SESSIONS, "ses_002")
    assert [s["id"] for s in out] == ["ses_002"]


def test_filter_case_insensitive():
    out = filter_sessions(SESSIONS, "TUI")
    assert [s["id"] for s in out] == ["ses_002"]


def test_filter_no_match():
    assert filter_sessions(SESSIONS, "zzz-nope") == []


def test_format_session_row():
    row = format_session(SESSIONS[0])
    assert "Fix auth bug" in row
    assert "anthropic/x" in row
    assert "ses_001" in row


def test_rename_session_calls_store():
    calls = []

    class _Store:
        def set_title(self, sid, title):
            calls.append((sid, title))

    assert rename_session(_Store(), "ses_1", "  New title  ") == "New title"
    assert calls == [("ses_1", "New title")]


# ---------------------------------------------------------------------------
# model picker
# ---------------------------------------------------------------------------

def test_recents_push_dedup_order(tmp_path):
    p = tmp_path / "models.json"
    push_recent("a", p)
    push_recent("b", p)
    push_recent("a", p)
    assert load_recents(p) == ["a", "b"]  # deduped, most-recent-first


def test_recents_capped_at_10(tmp_path):
    p = tmp_path / "models.json"
    for i in range(12):
        push_recent(f"m{i}", p)
    recents = load_recents(p)
    assert len(recents) == 10
    assert recents[0] == "m11"
    assert recents[-1] == "m2"


def test_favorites_round_trip(tmp_path):
    p = tmp_path / "models.json"
    save_favorites(["x", "y"], p)
    assert load_favorites(p) == ["x", "y"]


def test_filter_models_prefix_beats_subsequence():
    models = ["openai/gpt-4o", "anthropic/claude-opus-4-6", "x/opn-model"]
    out = filter_models(models, "opn")
    assert out[0] == "x/opn-model"  # prefix match first


def test_pick_model_favorites_first():
    models = ["b-model", "a-model", "c-model"]
    out = pick_model(models, "", favorites=["c-model"], recents=["a-model"])
    assert out == ["c-model", "a-model", "b-model"]


def test_pick_model_query_filters_each_group():
    models = ["anthropic/claude-x", "openai/gpt-x", "anthropic/claude-y"]
    out = pick_model(models, "gpt", favorites=["anthropic/claude-x"],
                     recents=["anthropic/claude-y"])
    assert out == ["openai/gpt-x"]


def test_format_model_row_tags():
    assert "★" in format_model_row("m", favorite=True, recent=False)
    assert "recent" in format_model_row("m", favorite=False, recent=True)
    assert format_model_row("m", favorite=False, recent=False) == "m"
