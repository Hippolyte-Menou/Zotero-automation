"""Shared entry-point bootstrap: credentials, client construction, logging.

Every executable in this bot (`run`, `inverse_bot`, `audit_bot`, `dedup_library`)
needs the same wiring: resolve Zotero credentials, build a ZoteroGroupClient,
build an OpenAlexClient, and configure logging. That wiring used to be copied
into each `main()`, and had drifted -- `run` and `inverse_bot` read `os.environ`
directly while the others resolved credentials through `bio_toolkit.config`, so a
change to how secrets resolve silently skipped the two most important scripts.

This module is the one place that wiring lives. Credential resolution always
goes through `bio_toolkit.config` (env var or the toolkit's gitignored secret),
with a `ZOTERO_GROUP_ID` env override preserved for both poles.
"""

import datetime
import logging
import os
import sys

from bio_toolkit.clients.openalex import OpenAlexClient
from genebot.zotero_client import ZoteroGroupClient

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging(stem: str | None = None, *, level: int = logging.INFO) -> None:
    """Configure root logging: stdout stream handler, plus a dated file handler
    under ``logs/`` when ``stem`` is given (e.g. ``stem="run"`` ->
    ``logs/run_2026-07-01.log``). Same-day runs append to the same file.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if stem:
        os.makedirs("logs", exist_ok=True)
        handlers.append(
            logging.FileHandler(
                f"logs/{stem}_{datetime.date.today()}.log", encoding="utf-8"
            )
        )
    logging.basicConfig(level=level, format=_LOG_FORMAT, handlers=handlers)


def zotero_credentials() -> tuple[str, str]:
    """Return ``(group_id, api_key)`` resolved through ``bio_toolkit.config``.

    - ``group_id``: ``ZOTERO_GROUP_ID`` env override, else the toolkit constant.
    - ``api_key``: ``bio_toolkit.config.zotero_api_key()`` (env var or secret).

    Raises ``RuntimeError`` if no API key can be resolved, so callers abort with
    a clear message instead of constructing a client that 403s on every request.
    """
    from bio_toolkit.config import ZOTERO_GROUP_ID, zotero_api_key

    group_id = os.environ.get("ZOTERO_GROUP_ID") or str(ZOTERO_GROUP_ID)
    try:
        api_key = zotero_api_key()
    except Exception as e:  # toolkit may raise when neither env nor secret is set
        raise RuntimeError(f"could not resolve Zotero API key: {e}") from e
    if not api_key:
        raise RuntimeError(
            "no Zotero API key (set ZOTERO_API_KEY or the bio_toolkit secret)"
        )
    return str(group_id), api_key


def make_zotero_client(delay: float = 1.0) -> ZoteroGroupClient:
    """Construct a ZoteroGroupClient from the resolved credentials."""
    group_id, api_key = zotero_credentials()
    return ZoteroGroupClient(group_id, api_key, delay=delay)


def make_openalex_client() -> OpenAlexClient:
    """Construct an OpenAlexClient, using ``OPENALEX_API_KEY`` if present.

    Logs which mode it came up in, so every entry point reports the rate-limit
    tier identically.
    """
    key = os.environ.get("OPENALEX_API_KEY") or None
    client = OpenAlexClient(api_key=key)
    if key:
        logger.info("OpenAlex client initialized (with API key)")
    else:
        logger.info("OpenAlex client initialized (no API key -- lower rate limit)")
    return client
