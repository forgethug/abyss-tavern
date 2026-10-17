"""Backend-owned retry authority for the S1 expedition Observer.

The Observer may propose regeneration, but only this module turns that
proposal into an executable retry target.  Natural-language ``reason`` is
deliberately ignored: authority comes from a closed basis plus bounded,
machine-checkable evidence against the frozen candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from plugins.adventurer_s1.services.s1_persistent_content_safety_service import (
    PERSISTENT_CONTENT_REGENERATION_BASIS,
)


class S1ObserverRegenerationTarget(str, Enum):
    KEEP_CANDIDATE = 'keep_candidate'
    MAIN = 'main'
    EVENT_RESOLUTION = 'event_resolution'


@dataclass(frozen=True)
class S1ObserverRegenerationDecision:
    requested: bool
    authorized: bool
    target: S1ObserverRegenerationTarget
    basis: str
    reason_code: str


_MAIN_REGENERATION_BASES = frozenset({
    'narrative_incomplete',
    'other_unrepairable',
    PERSISTENT_CONTENT_REGENERATION_BASIS,
})
_EVENT_RESOLUTION_REGENERATION_BASES = frozenset({
    'event_resolution_invalid',
})


def _denied(*, requested: bool, basis: str, reason_code: str) -> (
    S1ObserverRegenerationDecision
):
    return S1ObserverRegenerationDecision(
        requested=requested,
        authorized=False,
        target=S1ObserverRegenerationTarget.KEEP_CANDIDATE,
        basis=basis,
        reason_code=reason_code,
    )


def _final_events(result_data: dict | None) -> list:
    events = result_data.get('events') if isinstance(result_data, dict) else None
    return events if isinstance(events, list) else []


def _pre_narrative_event_facts(
    result_data: dict | None,
    cached_full_log: dict | None,
) -> list:
    # The persisted checkpoint is authoritative.  The candidate is only a
    # compatibility fallback for old in-process callers that lack a checkpoint.
    for source in (cached_full_log, result_data):
        if not isinstance(source, dict):
            continue
        resolution = source.get('pre_narrative_resolution')
        if not isinstance(resolution, dict):
            continue
        event_facts = resolution.get('event_facts')
        if isinstance(event_facts, list):
            return event_facts
    return []


def _frozen_member_ids(
    cached_full_log: dict | None,
) -> set[int]:
    member_ids: set[int] = set()
    rows = (
        cached_full_log.get('audit_member_snapshot')
        if isinstance(cached_full_log, dict)
        else None
    )
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        member_id = row.get('adventurer_id')
        if (
            isinstance(member_id, int)
            and not isinstance(member_id, bool)
            and member_id > 0
        ):
            member_ids.add(member_id)
    return member_ids


def _valid_main_evidence(evidence: object, *, event_count: int) -> bool:
    if not isinstance(evidence, dict):
        return False
    if evidence.get('source_layer') != 'main_narrative':
        return False
    indexes = evidence.get('main_event_indexes')
    if not isinstance(indexes, list) or not indexes:
        return False
    seen: set[int] = set()
    for index in indexes:
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= event_count
            or index in seen
        ):
            return False
        seen.add(index)
    return True


def _valid_event_resolution_evidence(
    evidence: object,
    *,
    event_fact_count: int,
    frozen_member_ids: set[int],
) -> bool:
    if not isinstance(evidence, dict):
        return False
    if evidence.get('source_layer') != 'pre_narrative_resolution':
        return False
    refs = evidence.get('pre_narrative_evidence_refs')
    if not isinstance(refs, list) or not refs:
        return False
    seen_indexes: set[int] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            return False
        event_fact_index = ref.get('event_fact_index')
        affected_member_ids = ref.get('affected_member_ids')
        if (
            not isinstance(event_fact_index, int)
            or isinstance(event_fact_index, bool)
            or event_fact_index < 0
            or event_fact_index >= event_fact_count
            or event_fact_index in seen_indexes
            or not isinstance(affected_member_ids, list)
            or not affected_member_ids
        ):
            return False
        seen_indexes.add(event_fact_index)
        seen_members: set[int] = set()
        for member_id in affected_member_ids:
            if (
                not isinstance(member_id, int)
                or isinstance(member_id, bool)
                or member_id <= 0
                or member_id in seen_members
                or member_id not in frozen_member_ids
            ):
                return False
            seen_members.add(member_id)
    return True


def decide_s1_observer_regeneration(
    verdict: dict | None,
    *,
    result_data: dict | None,
    cached_full_log: dict | None,
) -> S1ObserverRegenerationDecision:
    """Authorize only a typed, evidenced major-error regeneration proposal."""
    if not isinstance(verdict, dict):
        return _denied(
            requested=False,
            basis='none',
            reason_code='observer_verdict_not_object',
        )
    requested = verdict.get('needs_regeneration')
    basis = verdict.get('regeneration_basis')
    if requested is not True:
        return _denied(
            requested=False,
            basis=str(basis or 'none'),
            reason_code='regeneration_not_requested',
        )
    if not isinstance(basis, str):
        return _denied(
            requested=True,
            basis='none',
            reason_code='regeneration_basis_invalid',
        )
    evidence = verdict.get('regeneration_evidence')
    if basis in _MAIN_REGENERATION_BASES:
        if not _valid_main_evidence(
            evidence,
            event_count=len(_final_events(result_data)),
        ):
            return _denied(
                requested=True,
                basis=basis,
                reason_code='main_regeneration_evidence_invalid',
            )
        return S1ObserverRegenerationDecision(
            requested=True,
            authorized=True,
            target=S1ObserverRegenerationTarget.MAIN,
            basis=basis,
            reason_code=basis,
        )
    if basis in _EVENT_RESOLUTION_REGENERATION_BASES:
        if not _valid_event_resolution_evidence(
            evidence,
            event_fact_count=len(_pre_narrative_event_facts(
                result_data,
                cached_full_log,
            )),
            frozen_member_ids=_frozen_member_ids(
                cached_full_log,
            ),
        ):
            return _denied(
                requested=True,
                basis=basis,
                reason_code='event_resolution_regeneration_evidence_invalid',
            )
        return S1ObserverRegenerationDecision(
            requested=True,
            authorized=True,
            target=S1ObserverRegenerationTarget.EVENT_RESOLUTION,
            basis=basis,
            reason_code=basis,
        )
    return _denied(
        requested=True,
        basis=basis,
        reason_code='regeneration_basis_not_authorized',
    )
