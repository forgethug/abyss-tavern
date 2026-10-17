"""占位「xxx入门」技能回收：管理员推送确认，玩家同意后删除技能并补发职业通用技能卷轴。

检测规则（机械判定，非语义）：
- 技能名以「入门」结尾；
- 排除技能书阁传授列表中的合法技能名（当前 catalog 无此类名，留作防御）。
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1PlaceholderSkillRecallOffer,
    AdventurerS1Skill,
)
from plugins.adventurer_s1.services.s1_class_identity_service import (
    CLASS_IDENTITY_SKILL_SCROLL_NAME,
)
from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService
from db_repository.decorators import with_transaction
from utils.time_utils import to_utc_iso

_ITEM_LOG_SOURCE = 'placeholder_skill_recall'
_PLACEHOLDER_SUFFIX = '入门'


class S1PlaceholderSkillRecallService:
    """占位入门技能 → 职业通用技能卷轴 的玩家确认回收。"""

    @staticmethod
    def _skill_school_names() -> set[str]:
        return {
            str(item.get('name') or '').strip()
            for item in S1SkillSchoolService.get_catalog()
            if str(item.get('name') or '').strip()
        }

    @classmethod
    def is_placeholder_entry_skill(cls, name: str, *, school_names: set[str] | None = None) -> bool:
        clean = str(name or '').strip()
        if not clean or not clean.endswith(_PLACEHOLDER_SUFFIX):
            return False
        if school_names is None:
            school_names = cls._skill_school_names()
        return clean not in school_names

    @classmethod
    def _batch_placeholder_skills_by_adv(
        cls,
        *,
        adventurer_ids: list[int] | None = None,
        school_names: set[str] | None = None,
    ) -> dict[int, list[AdventurerS1Skill]]:
        """批量拉取占位「xxx入门」技能，避免 preview 对每个冒险者单独查 skills。"""
        if school_names is None:
            school_names = cls._skill_school_names()
        q = AdventurerS1Skill.query.filter(
            AdventurerS1Skill.name.like(f'%{_PLACEHOLDER_SUFFIX}'),
        )
        if adventurer_ids:
            q = q.filter(AdventurerS1Skill.adventurer_id.in_(adventurer_ids))
        grouped: dict[int, list[AdventurerS1Skill]] = {}
        for skill in q.all():
            if not cls.is_placeholder_entry_skill(skill.name, school_names=school_names):
                continue
            grouped.setdefault(int(skill.adventurer_id), []).append(skill)
        return grouped

    @classmethod
    def _placeholder_skills_for_adv(
        cls,
        adv_id: int,
        *,
        school_names: set[str] | None = None,
    ) -> list[AdventurerS1Skill]:
        return cls._batch_placeholder_skills_by_adv(
            adventurer_ids=[adv_id],
            school_names=school_names,
        ).get(adv_id, [])

    @classmethod
    def _pending_offer_by_adv(cls, adv_ids: list[int]) -> dict[int, AdventurerS1PlaceholderSkillRecallOffer]:
        if not adv_ids:
            return {}
        rows = (
            AdventurerS1PlaceholderSkillRecallOffer.query
            .filter(
                AdventurerS1PlaceholderSkillRecallOffer.adventurer_id.in_(adv_ids),
                AdventurerS1PlaceholderSkillRecallOffer.status == 'pending',
            )
            .all()
        )
        return {int(r.adventurer_id): r for r in rows}

    @classmethod
    def preview(cls, *, adventurer_id: int | None = None) -> Tuple[Optional[dict], Optional[str]]:
        school_names = cls._skill_school_names()
        only_adv_ids = [int(adventurer_id)] if adventurer_id else None
        skills_by_adv = cls._batch_placeholder_skills_by_adv(
            adventurer_ids=only_adv_ids,
            school_names=school_names,
        )
        if not skills_by_adv:
            return {
                'suffix_rule': f'技能名以「{_PLACEHOLDER_SUFFIX}」结尾（排除技能书阁传授列表）',
                'scroll_name': CLASS_IDENTITY_SKILL_SCROLL_NAME,
                'total_adventurers': 0,
                'sendable_count': 0,
                'pending_count': 0,
                'skill_name_histogram': [],
                'items': [],
            }, None

        adv_ids = sorted(skills_by_adv.keys())
        adv_rows = (
            AdventurerS1.query
            .filter(
                AdventurerS1.id.in_(adv_ids),
                AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .order_by(AdventurerS1.id.asc())
            .all()
        )
        pending_map = cls._pending_offer_by_adv(adv_ids)

        items: list[dict] = []
        name_histogram: dict[str, int] = {}
        for adv in adv_rows:
            skills = skills_by_adv.get(adv.id) or []
            if not skills:
                continue
            for s in skills:
                name_histogram[s.name] = name_histogram.get(s.name, 0) + 1
            pending = pending_map.get(adv.id)
            can_send = pending is None
            skip_reason = None
            if pending is not None:
                skip_reason = '已有待确认邀请'
            items.append({
                'adventurer_id': adv.id,
                'adventurer_name': adv.name,
                'owner_id': adv.owner_id,
                'character_class': adv.character_class,
                'status': adv.status,
                'placeholder_skills': [
                    {'id': s.id, 'name': s.name, 'equipped': bool(s.equipped)}
                    for s in skills
                ],
                'scroll_grant_count': len(skills),
                'scroll_name': CLASS_IDENTITY_SKILL_SCROLL_NAME,
                'offer_status': pending.status if pending else 'none',
                'pending_offer_id': pending.id if pending else None,
                'can_send': can_send,
                'skip_reason': skip_reason,
            })

        return {
            'suffix_rule': f'技能名以「{_PLACEHOLDER_SUFFIX}」结尾（排除技能书阁传授列表）',
            'scroll_name': CLASS_IDENTITY_SKILL_SCROLL_NAME,
            'total_adventurers': len(items),
            'sendable_count': sum(1 for i in items if i.get('can_send')),
            'pending_count': sum(1 for i in items if i.get('offer_status') == 'pending'),
            'skill_name_histogram': [
                {'name': name, 'count': count}
                for name, count in sorted(name_histogram.items(), key=lambda x: (-x[1], x[0]))
            ],
            'items': items,
        }, None

    @classmethod
    @with_transaction(auto_commit=True)
    def send_offers(
        cls,
        *,
        admin_id: int,
        adventurer_ids: list[int] | None = None,
        admin_note: str | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        preview_data, err = cls.preview()
        if err:
            return None, err
        note = str(admin_note or '').strip() or None
        only_ids = {int(i) for i in (adventurer_ids or []) if i} or None
        sent: list[dict] = []
        skipped: list[dict] = []
        now = _portable_utc_now()

        for item in preview_data.get('items') or []:
            adv_id = int(item['adventurer_id'])
            if only_ids is not None and adv_id not in only_ids:
                continue
            if not item.get('can_send'):
                skipped.append({
                    'adventurer_id': adv_id,
                    'adventurer_name': item.get('adventurer_name'),
                    'reason': item.get('skip_reason') or '不可发送',
                })
                continue

            skills = item.get('placeholder_skills') or []
            if not skills:
                skipped.append({'adventurer_id': adv_id, 'reason': '无占位技能'})
                continue

            offer = AdventurerS1PlaceholderSkillRecallOffer(
                adventurer_id=adv_id,
                user_id=int(item['owner_id']),
                status='pending',
                skill_snapshot=[{'id': s['id'], 'name': s['name']} for s in skills if s.get('name')],
                scroll_grant_count=len(skills),
                admin_note=note,
                sent_at=now,
            )
            ok, add_err = db_manager.add(offer)
            if not ok:
                skipped.append({'adventurer_id': adv_id, 'reason': add_err or '写入失败'})
                continue
            sent.append({
                'offer_id': offer.id,
                'adventurer_id': adv_id,
                'adventurer_name': item.get('adventurer_name'),
                'scroll_grant_count': offer.scroll_grant_count,
            })

        current_app.logger.info(
            'S1 占位入门技能回收邀请：管理员=%s 发送=%s 跳过=%s',
            admin_id, len(sent), len(skipped),
        )
        return {'sent': sent, 'skipped': skipped}, None

    @staticmethod
    def list_pending_for_user(user_id: int, *, limit: int = 10) -> Tuple[Optional[list], Optional[str]]:
        limit = max(1, min(int(limit or 10), 20))
        rows = (
            AdventurerS1PlaceholderSkillRecallOffer.query
            .filter_by(user_id=user_id, status='pending')
            .order_by(AdventurerS1PlaceholderSkillRecallOffer.sent_at.desc())
            .limit(limit)
            .all()
        )
        if not rows:
            return [], None
        adv_ids = {r.adventurer_id for r in rows}
        adv_name_map = dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(adv_ids))
            .all()
        ) if adv_ids else {}
        return [
            r.to_player_dict(adventurer_name=adv_name_map.get(r.adventurer_id))
            for r in rows
        ], None

    @classmethod
    @with_transaction(auto_commit=True)
    def accept_offer(cls, offer_id: int, user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        offer = (
            db_manager.query(AdventurerS1PlaceholderSkillRecallOffer)
            .filter_by(id=offer_id, user_id=user_id)
            .with_for_update()
            .first()
        )
        if not offer:
            return None, '邀请不存在或无权操作'
        if (offer.status or 'none') != 'pending':
            return None, '该邀请已处理'

        adv = (
            AdventurerS1.query
            .filter_by(id=offer.adventurer_id, owner_id=user_id)
            .with_for_update()
            .first()
        )
        if not adv:
            return None, '冒险者不存在或无权操作'

        snapshot = offer.skill_snapshot if isinstance(offer.skill_snapshot, list) else []
        school_names = cls._skill_school_names()
        removed_names: list[str] = []
        for entry in snapshot:
            if not isinstance(entry, dict):
                continue
            skill_id = entry.get('id')
            skill_name = str(entry.get('name') or '').strip()
            if not skill_name or not cls.is_placeholder_entry_skill(skill_name, school_names=school_names):
                continue
            skill = None
            if skill_id is not None:
                skill = AdventurerS1Skill.query.filter_by(
                    id=int(skill_id), adventurer_id=adv.id,
                ).first()
            if skill is None:
                skill = AdventurerS1Skill.query.filter_by(
                    adventurer_id=adv.id, name=skill_name,
                ).first()
            if not skill or skill.name != skill_name:
                continue
            ok, del_err = db_manager.delete_instance(skill)
            if not ok:
                return None, del_err or f'删除技能「{skill_name}」失败'
            removed_names.append(skill_name)

        if not removed_names:
            return None, '占位技能已不存在，请联系管理员重新发送邀请'

        from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item

        _, grant_err = grant_catalog_item(
            adv,
            CLASS_IDENTITY_SKILL_SCROLL_NAME,
            quantity=len(removed_names),
            prefer_warehouse=True,
            source_type=_ITEM_LOG_SOURCE,
            description='玩家确认回收占位入门技能，补发职业通用技能卷轴',
            related_id=str(offer.id),
            related_type='placeholder_skill_recall',
        )
        if grant_err:
            return None, grant_err

        now = _portable_utc_now()
        offer.status = 'accepted'
        offer.responded_at = now
        offer.completed_at = now
        offer.scroll_grant_count = len(removed_names)

        return {
            'offer_id': offer.id,
            'adventurer_id': adv.id,
            'removed_skills': removed_names,
            'granted_scrolls': len(removed_names),
            'scroll_name': CLASS_IDENTITY_SKILL_SCROLL_NAME,
            'completed_at': to_utc_iso(now),
        }, None

    @classmethod
    @with_transaction(auto_commit=True)
    def decline_offer(cls, offer_id: int, user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        offer = (
            db_manager.query(AdventurerS1PlaceholderSkillRecallOffer)
            .filter_by(id=offer_id, user_id=user_id)
            .with_for_update()
            .first()
        )
        if not offer:
            return None, '邀请不存在或无权操作'
        if (offer.status or 'none') != 'pending':
            return None, '该邀请已处理'
        now = _portable_utc_now()
        offer.status = 'declined'
        offer.responded_at = now
        return {'offer_id': offer.id, 'status': 'declined'}, None
