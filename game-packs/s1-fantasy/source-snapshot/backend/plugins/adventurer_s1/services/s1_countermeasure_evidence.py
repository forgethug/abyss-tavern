"""Exact final-narrative references for learning and knowledge applications."""

from __future__ import annotations


def normalize_evidence_quotes(value: object, indexes: list[int]) -> list[str] | None:
    """Project the parallel quote array without inferring any narrative meaning."""
    if not isinstance(value, list) or len(value) != len(indexes):
        return None
    if any(not isinstance(quote, str) or not quote.strip() for quote in value):
        return None
    return [quote.strip() for quote in value]


def bind_final_event_evidence(
    indexes: list[int],
    quotes: list[str],
    events: object,
    *,
    allow_rebind: bool = False,
) -> tuple[list[int] | None, str | None]:
    """Bind literal passages to final events; only a unique exact match can rebind.

    This establishes reference identity, not observation, learning, or response.
    Those semantic judgments remain with the paired narrative reviewer.
    """
    if not isinstance(indexes, list) or any(type(index) is not int or index < 0 for index in indexes):
        return None, 'evidence_indexes_invalid'
    normalized_quotes = normalize_evidence_quotes(quotes, indexes)
    if not isinstance(events, list) or normalized_quotes is None:
        return None, 'evidence_quotes_invalid'
    bound: list[int] = []
    for index, quote in zip(indexes, normalized_quotes):
        if (
            type(index) is int and 0 <= index < len(events)
            and isinstance(events[index], str) and quote in events[index]
        ):
            resolved = index
        elif allow_rebind:
            matches = [
                candidate_index for candidate_index, event in enumerate(events)
                if isinstance(event, str) and quote in event
            ]
            if len(matches) != 1:
                return None, 'evidence_quote_not_uniquely_bound'
            resolved = matches[0]
        else:
            return None, 'evidence_quote_event_mismatch'
        if resolved in bound:
            return None, 'evidence_quote_duplicate_event'
        bound.append(resolved)
    return bound, None
