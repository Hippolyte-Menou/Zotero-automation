"""Small JSON read/write primitives shared across the bot's cache/ledger files.

Every entry point had its own copy of "load JSON or return a default" and
"mkdir -p then dump JSON". These two functions are that shared primitive; the
domain wrappers (citation cache, audit ledger, inverse cache) keep their own
logging and schema bookkeeping and call these for the actual I/O.
"""

import json
import os


def read_json(path: str, default=None):
    """Return the parsed JSON at ``path``, or ``default`` if it is missing or
    unreadable (not a file, bad JSON, or an OS error)."""
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(
    path: str,
    obj,
    *,
    indent: int | None = None,
    ensure_ascii: bool = False,
    make_dirs: bool = True,
) -> None:
    """Serialise ``obj`` to ``path`` as JSON, creating parent directories by
    default. ``indent``/``ensure_ascii`` pass through to ``json.dump``."""
    if make_dirs:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent, ensure_ascii=ensure_ascii)
