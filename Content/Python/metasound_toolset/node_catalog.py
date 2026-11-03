"""Curated MetaSound node-class catalog + keyword search.

Agents fail at MetaSounds mostly by hallucinating node class names and by
wiring the wrong type variant (the Audio-vs-Float rule). This module ships
a small curated catalog of UE 5.8 node classes with per-entry verification
tags, and a keyword search over it.

The catalog is a convenience index, not ground truth: ground truth is the
live engine via the ``resolve_node_class`` tool, which probes the actual
node registry and returns real pins. Every search result says whether its
entry was verified and from what source.

Catalog data lives in ``data/node_catalog_ue58.json`` beside this module.
"""

import json
from functools import lru_cache
from pathlib import Path

_CATALOG_PATH = Path(__file__).parent / 'data' / 'node_catalog_ue58.json'


@lru_cache(maxsize=1)
def load_catalog() -> list[dict]:
    """Load and cache the node catalog entries."""
    with open(_CATALOG_PATH, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    return data['nodes']


def search(query: str, max_results: int = 10) -> list[dict]:
    """Keyword-score catalog entries against a free-text query.

    Args:
        query: Free text, e.g. "random pitch", "gain db", "low pass filter".
        max_results: Maximum entries to return, best first.

    Returns:
        Matching catalog entries (possibly empty), each a dict with
        namespace/name/variant/display/category/summary/keywords and a
        verification dict (status, source, date).
    """
    if not query or not query.strip():
        raise ValueError('query must not be empty.')
    if max_results < 1:
        raise ValueError('max_results must be at least 1.')

    terms = [t for t in query.lower().replace('(', ' ').replace(')', ' ')
             .split() if t]
    scored: list[tuple[float, dict]] = []
    for entry in load_catalog():
        haystacks = (
            (entry['display'].lower(), 3.0),
            (entry['name'].lower(), 3.0),
            (' '.join(entry.get('keywords', [])).lower(), 2.0),
            (entry.get('summary', '').lower(), 1.0),
            (entry.get('category', '').lower(), 1.0),
            (entry.get('variant', '').lower(), 1.0),
        )
        score = 0.0
        for term in terms:
            for text, weight in haystacks:
                if term in text:
                    score += weight
                    break
        if score > 0.0:
            # Prefer verified entries on ties.
            if entry['verification']['status'] == 'verified':
                score += 0.5
            scored.append((score, entry))

    scored.sort(key=lambda pair: -pair[0])
    return [entry for _, entry in scored[:max_results]]
