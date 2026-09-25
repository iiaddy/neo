"""neo scan — bash command signature scanner for permission matching.

Turns a shell command string into a list of progressively generalized
signatures (``git checkout main`` -> ``git checkout *`` -> ``git *``)
so permission policies can match at any level of specificity.
"""

from __future__ import annotations

from .bashscan import first_word, scan

__all__ = ["first_word", "scan"]
