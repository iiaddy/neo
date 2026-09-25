"""neo commands — user-defined slash commands from Markdown files.

Custom commands live in ``.neo/commands/*.md`` (project) and
``~/.config/neo/commands/*.md`` (user). Optional YAML-lite frontmatter
between leading ``---`` lines declares metadata::

    ---
    description: Review the current diff
    agent: review
    model: sonnet
    subtask: true
    ---
    Review the diff for $1 ...

The body is a template expanded by :func:`neo.commands.engine.render`.
"""

from __future__ import annotations

from .engine import Command, discover, render

__all__ = ["Command", "discover", "render"]
