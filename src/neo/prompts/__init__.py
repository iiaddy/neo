"""Agent prompt templates, stored as markdown files (opencode-style).

Every prompt the agents use lives in its own ``.md`` file inside this
package instead of inline Python strings, so agent behavior can be read
and tuned without touching code:

- ``system.md`` — the main agent's base system prompt
- ``memory.md`` — the long-term-memory guide injected every session
- ``summary.md`` — the session-summarization prompt (compaction)
- ``max_steps.md`` — the text-only summary contract on the final step
- ``plan_enter.md`` — the read-only workflow instructions for plan mode
- ``agents/*.md`` — one system prompt per builtin agent (explore, build,
  plan, title, summary)

Files load through ``importlib.resources`` and are cached, so they work
from an installed wheel as well as from a source checkout.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

_PACKAGE = files(__name__)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Return the raw text of ``<name>.md`` under this package.

    ``name`` may include a subdirectory, e.g. ``"agents/explore"``.
    Trailing newlines are stripped so the text behaves exactly like the
    old module-level string constants.
    """
    return (_PACKAGE / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


def render_prompt(name: str, **values: object) -> str:
    """Load ``<name>.md`` and substitute ``{placeholder}`` values.

    The template file must only contain placeholders passed here —
    anything else in braces raises ``KeyError``.
    """
    return load_prompt(name).format(**values)
