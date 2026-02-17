"""Fetch gene symbol aliases from HGNC."""

import requests
import logging

logger = logging.getLogger(__name__)

HGNC_URL = "https://rest.genenames.org/fetch/symbol/{symbol}"


def get_gene_aliases(symbol: str) -> set[str]:
    """
    Return a set of names/aliases for a gene symbol.
    Includes: approved symbol, previous symbols, alias symbols, full name.
    """
    headers = {"Accept": "application/json"}
    r = requests.get(HGNC_URL.format(symbol=symbol), headers=headers, timeout=10)
    r.raise_for_status()

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
