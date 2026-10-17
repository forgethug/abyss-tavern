"""S1 纯正面特质装卸的精确哈希投影。"""

from collections.abc import Iterable

from plugins.adventurer_s1.models import AdventurerS1DescriptionAiCache
from plugins.adventurer_s1.services.s1_description_ai_service import (
    description_source_hash,
)
from plugins.adventurer_s1.services.s1_trait_context_selection_service import (
    TRAIT_EFFECT_POLARITY_PURE_POSITIVE,
    project_reviewed_trait_effect_polarity,
)


def _active_traits(traits: Iterable[object] | None) -> list[object]:
    return [
        trait
        for trait in (traits or [])
        if not bool(getattr(trait, 'is_archived', False))
        and int(getattr(trait, 'id', 0) or 0) > 0
    ]


def _source_hash_for_trait(trait: object) -> str:
    return description_source_hash(
        getattr(trait, 'description', None),
        kind='trait',
    )


def _reviewed_polarity_from_row(
    row: AdventurerS1DescriptionAiCache | None,
    *,
    source_hash: str,
) -> str | None:
    if row is None or str(row.description_kind or '') != 'trait':
        return None
    return project_reviewed_trait_effect_polarity(
        row.trait_context_rule,
        classification_source_hash=source_hash,
        audit_status=row.trait_effect_polarity_audit_status,
    )


def reviewed_trait_effect_polarity(trait: object) -> str | None:
    """读取一条当前特质的独立审核结果；缺失、过期或拒绝均返回 None。"""
    source_hash = _source_hash_for_trait(trait)
    row = AdventurerS1DescriptionAiCache.query.filter_by(
        source_hash=source_hash,
    ).first()
    return _reviewed_polarity_from_row(row, source_hash=source_hash)


def build_card_trait_loadout_states(
    traits: Iterable[object] | None,
    *, session,
) -> dict[int, dict[str, bool]]:
    """Only verify unequipped choices for name-only list cards.

    Equipped traits are visible regardless of classification. Missing states
    already mean visible to the list serializer; detail and write paths still
    use the complete projection below. Never trust a stored summary hash in
    place of hashing the current description for an unequipped choice.
    """
    from db_repository.s1_tavern_read_repository import load_card_unequipped_traits

    trait_ids = [
        int(trait.id) for trait in _active_traits(traits)
        if getattr(trait, 'equipped', True) is False
    ]
    if not trait_ids:
        return {}
    rows = load_card_unequipped_traits(session, trait_ids)
    return build_trait_loadout_states(rows, session=session)


def build_trait_loadout_states(
    traits: Iterable[object] | None,
    *, session=None,
) -> dict[int, dict[str, bool]]:
    """批量构建玩家白名单状态；非纯正面或未确认特质一律视为已携带。"""
    active_traits = _active_traits(traits)
    hash_by_id = {
        int(getattr(trait, 'id')): _source_hash_for_trait(trait)
        for trait in active_traits
    }
    source_hashes = set(hash_by_id.values())
    if not source_hashes:
        return {}
    query = AdventurerS1DescriptionAiCache.query.with_entities(
        AdventurerS1DescriptionAiCache.source_hash,
        AdventurerS1DescriptionAiCache.description_kind,
        AdventurerS1DescriptionAiCache.trait_context_rule,
        AdventurerS1DescriptionAiCache.trait_effect_polarity_audit_status,
    ).filter(AdventurerS1DescriptionAiCache.source_hash.in_(source_hashes))
    if session is not None:
        query = query.with_session(session)
    rows_by_hash = {
        row.source_hash: row
        for row in query.all()
    }

    result: dict[int, dict[str, bool]] = {}
    for trait in active_traits:
        trait_id = int(getattr(trait, 'id'))
        source_hash = hash_by_id[trait_id]
        polarity = _reviewed_polarity_from_row(
            rows_by_hash.get(source_hash),
            source_hash=source_hash,
        )
        is_pure_positive = (
            polarity == TRAIT_EFFECT_POLARITY_PURE_POSITIVE
        )
        result[trait_id] = {
            'is_pure_positive': is_pure_positive,
            'equipped': (
                getattr(trait, 'equipped', True) is not False
                if is_pure_positive else True
            ),
        }
    return result
