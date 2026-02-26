"""Fetch gene symbol aliases from HGNC."""

import requests
import logging
import time

logger = logging.getLogger(__name__)

HGNC_URL = "https://rest.genenames.org/fetch/symbol/{symbol}"


def get_gene_aliases(symbol: str) -> set[str]:
    """
    Return a set of names/aliases for a gene symbol.
    Includes: approved symbol, previous symbols, alias symbols, full name.
    """
    headers = {"Accept": "application/json"}
    for attempt in range(3):
        try:
            r = requests.get(HGNC_URL.format(symbol=symbol), headers=headers, timeout=10)
            r.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                logger.warning(f"HGNC request failed for {symbol} ({e}), retrying in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"HGNC request failed for {symbol} after 3 attempts: {e}")
                return {symbol}

    docs = r.json().get("response", {}).get("docs", [])
    if not docs:
        logger.warning(f"No HGNC entry found for '{symbol}', using symbol as-is")
        return {symbol}

    doc = docs[0]
    aliases = set()
    aliases.add(doc.get("symbol", symbol))
    aliases.update(doc.get("alias_symbol", []))
    aliases.update(doc.get("prev_symbol", []))

    name = doc.get("name")
    if name:
        aliases.add(name)

    logger.info(f"HGNC aliases for {symbol}: {aliases}")
    return aliases
