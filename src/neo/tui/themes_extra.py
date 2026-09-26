"""Extra neo TUI themes. Same dict format as tui/themes.py:

background, surface, panel, border, text, muted, accent,
green, red, yellow
"""
from __future__ import annotations

EXTRA_THEMES: dict[str, dict[str, str]] = {
    "neo-ocean": {
        "background": "#0a141c",
        "surface": "#10202e",
        "panel": "#162b3d",
        "border": "#26485f",
        "text": "#dceefb",
        "muted": "#7fa3b8",
        "accent": "#4cc3d9",
        "green": "#4ade80",
        "red": "#f87171",
        "yellow": "#fbbf24",
    },
    "neo-forest": {
        "background": "#0c130d",
        "surface": "#141f16",
        "panel": "#1c2b1f",
        "border": "#2e4631",
        "text": "#e3f0e4",
        "muted": "#8aa88d",
        "accent": "#7bc98a",
        "green": "#4ade80",
        "red": "#f87171",
        "yellow": "#eab308",
    },
    "neo-sunset": {
        "background": "#171008",
        "surface": "#211409",
        "panel": "#2e1c0e",
        "border": "#54341c",
        "text": "#f7ead9",
        "muted": "#b99878",
        "accent": "#fb923c",
        "green": "#4ade80",
        "red": "#ef4444",
        "yellow": "#fde047",
    },
    "neo-mono": {
        "background": "#000000",
        "surface": "#111111",
        "panel": "#1d1d1d",
        "border": "#3d3d3d",
        "text": "#f2f2f2",
        "muted": "#8a8a8a",
        "accent": "#e0e0e0",
        "green": "#bdbdbd",
        "red": "#f2f2f2",
        "yellow": "#9e9e9e",
    },
    "neo-raspberry": {
        "background": "#150a12",
        "surface": "#1f0f1b",
        "panel": "#2c1527",
        "border": "#552544",
        "text": "#fbeef6",
        "muted": "#b58ba6",
        "accent": "#ec4899",
        "green": "#4ade80",
        "red": "#ef4444",
        "yellow": "#fbbf24",
    },
    "neo-arctic": {
        "background": "#eef3f7",
        "surface": "#e2ebf1",
        "panel": "#d4e0e9",
        "border": "#aebfd0",
        "text": "#16222e",
        "muted": "#5d7285",
        "accent": "#0284c7",
        "green": "#15803d",
        "red": "#dc2626",
        "yellow": "#b45309",
    },
}

EXTRA_THEME_NAMES: list[str] = list(EXTRA_THEMES)
