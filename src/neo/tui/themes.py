"""neo TUI themes — color definitions applied as CSS variables."""
from __future__ import annotations

THEMES: dict[str, dict[str, str]] = {
    "neo-dark": {
        "background": "#0d1117",
        "surface": "#161b22",
        "panel": "#1c2128",
        "border": "#30363d",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "accent": "#58a6ff",
        "green": "#3fb950",
        "red": "#f85149",
        "yellow": "#d29922",
        "user_bg": "#1c2b3a",
    },
    "neo-light": {
        "background": "#ffffff",
        "surface": "#f6f8fa",
        "panel": "#eaeef2",
        "border": "#d0d7de",
        "text": "#1f2328",
        "muted": "#59636e",
        "accent": "#0969da",
        "green": "#1a7f37",
        "red": "#d1242f",
        "yellow": "#9a6700",
        "user_bg": "#ddf4ff",
    },
    "neo-amber": {
        "background": "#14100a",
        "surface": "#1e1810",
        "panel": "#2a2115",
        "border": "#4a3a22",
        "text": "#f5eeda",
        "muted": "#a89880",
        "accent": "#e8a33d",
        "green": "#7fb069",
        "red": "#e0705f",
        "yellow": "#e8a33d",
        "user_bg": "#2a2115",
    },
}

DEFAULT_THEME = "neo-dark"


# Extra themes (neo-ocean/forest/sunset/mono/raspberry/arctic) live in
# themes_extra.py so the base file stays small; merged here so they are
# first-class everywhere theme_names()/get_theme() are used.
try:
    from .themes_extra import EXTRA_THEMES as _EXTRA_THEMES
    THEMES.update(_EXTRA_THEMES)
except Exception:
    pass


def theme_names() -> list[str]:
    return list(THEMES)


def get_theme(name: str) -> dict[str, str]:
    return THEMES.get(name, THEMES[DEFAULT_THEME])
