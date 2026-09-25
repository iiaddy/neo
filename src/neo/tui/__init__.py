"""neo TUI entry point."""
from __future__ import annotations

from pathlib import Path

from .app import NeoApp


def run_tui(workdir: str | Path, config, resume: str | None = None,
            theme: str = "neo-dark") -> None:
    """Run the interactive neo terminal UI."""
    app = NeoApp(workdir, config, resume=resume, theme=theme)
    app.run()
