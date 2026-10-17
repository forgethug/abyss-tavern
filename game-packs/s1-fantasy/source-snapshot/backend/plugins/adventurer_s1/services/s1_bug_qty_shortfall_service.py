"""管理员补发数据申诉历史漏写的物品数量。"""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime

from db_repository import db_manager
from models.user import User
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BugQtyShortfallBatch,
    AdventurerS1BugQtyShortfallItem,
    AdventurerS1DataAppeal,
    AdventurerS1DataAppealAuditLog,
    AdventurerS1Equipment,
    AdventurerS1ItemLog,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_bug_appeal_qty_guard import S1BugAppealQtyGuard
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import S1AdminScanSnapshotService
from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of, parse_embedded_quantity, text_has_embedded_quantity
from plugins.adventurer_s1.services.s1_item_instance_service import lock_warehouse_aggregate
from utils.time_utils import to_utc_iso


class S1BugQtyShortfallService:
    """预览并执行申诉 name/desc 已归位但 quantity 漏写的补发。"""

    @staticmethod
    def _text(value) -> str:
        return str(value or '').strip()

    @staticmethod
    def _qty(value, default: int = 1) -> int:
        try:
            return max(0, int(value if value is not None else default))
        except (TypeError, ValueError):
            return max(0, int(default or 0))

    @staticmethod
    def _item_identity_changed(before: dict, after: dict) -> bool:
        if 'name' in after:
            old_name = S1BugQtyShortfallService._text(before.get('name'))
            new_name = S1BugQtyShortfallService._text(after.get('name'))
            if new_name and new_name != old_name:
                return True
        if 'custom_desc' in after:
            old_desc = S1BugQtyShortfallService._text(before.get('custom_desc'))
            new_desc = S1BugQtyShortfallService._text(after.get('custom_desc'))
            if new_desc != old_desc:
                return True
        return False

    @staticmethod
    def _patch_sections(appeal: AdventurerS1DataAppeal) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        if isinstance(appeal.proposed_patch, dict):
            out.append(('proposed_patch', appeal.proposed_patch))
        if isinstance(appeal.review_supplement_patch, dict):
            out.append(('review_supplement_patch', appeal.review_supplement_patch))
        raw_text = getattr(appeal, 'ai_raw_response', None)
        if raw_text:
            try:
                import json
                parsed = json.loads(raw_text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get('patch'), dict):
                out.append(('ai_raw_patch', parsed['patch']))
        return out

    @staticmethod
    def _intended_update_quantity(appeal: AdventurerS1DataAppeal, section: str, target_id: int) -> dict:
        best = {'quantity': None, 'source': None, 'patch_item': None}
        for source, patch in S1BugQtyShortfallService._patch_sections(appeal):
            sec = patch.get(section) if isinstance(patch.get(section), dict) else {}
            for item in sec.get('update') or []:
                if not isinstance(item, dict) or 'quantity' not in item:
                    continue
                try:
                    if int(item.get('id')) != int(target_id):
                        continue
                except (TypeError, ValueError):
                    continue
                qty = S1BugQtyShortfallService._qty(item.get('quantity'))
                if best['quantity'] is None or qty > int(best['quantity']):
                    best = {'quantity': qty, 'source': source, 'patch_item': item}
        return best

    @staticmethod
    def _intended_create_quantity(appeal: AdventurerS1DataAppeal, after: dict) -> dict:
        name = S1BugQtyShortfallService._text(after.get('name'))
        destination = S1BugQtyShortfallService._text(after.get('requested_destination') or after.get('destination'))
        best = {'quantity': None, 'source': None, 'patch_item': None}
        for source, patch in S1BugQtyShortfallService._patch_sections(appeal):
            for item in patch.get('create_items') or []:
                if not isinstance(item, dict):
                    continue
                if S1BugQtyShortfallService._text(item.get('name')) != name:
                    continue
                if destination and S1BugQtyShortfallService._text(item.get('destination')) not in ('', destination):
                    continue
                qty = S1BugQtyShortfallService._qty(item.get('quantity'), default=1)
                if best['quantity'] is None or qty > int(best['quantity']):
                    best = {'quantity': qty, 'source': source, 'patch_item': item}
        return best

    @staticmethod
    def _parse_before_text_quantity(before: dict) -> dict:
        best = {'quantity': None, 'base_name': None, 'source_field': None}
        for field in ('name', 'custom_desc'):
            text = S1BugQtyShortfallService._text(before.get(field))
            if not text:
                continue
            base_name, parsed_qty, found = parse_embedded_quantity(text)
            if not found or parsed_qty is None or parsed_qty < 1:
                continue
            if best['quantity'] is None or parsed_qty > int(best['quantity']):
                best = {'quantity': parsed_qty, 'base_name': base_name, 'source_field': field}
        return best

    @staticmethod
    def _quantity_text_removed_on_changed_fields(before: dict, after: dict) -> bool:
        for field in ('name', 'custom_desc'):
            if field not in after:
                continue
            old_text = S1BugQtyShortfallService._text(before.get(field))
            new_text = S1BugQtyShortfallService._text(after.get(field))
            if new_text == old_text:
                continue
            if text_has_embedded_quantity(old_text) and not text_has_embedded_quantity(new_text):
                return True
        return False

    @staticmethod
    def _excluded_issue_keys() -> set[str]:
        rows = (
            db_manager.query(AdventurerS1BugQtyShortfallItem.issue_key)
            .filter(AdventurerS1BugQtyShortfallItem.status.in_(('executed', 'dismissed')))
            .all()
        )
        return {str(r[0]) for r in rows if r and r[0]}

    @staticmethod
    def _adjustments() -> dict[str, dict]:
        rows = (
            db_manager.query(AdventurerS1BugQtyShortfallItem)
            .filter(AdventurerS1BugQtyShortfallItem.status == 'adjusted')
            .all()
        )
        return {str(r.issue_key): r.result_json or {} for r in rows}

    @staticmethod
    def _fetch_rows() -> list[tuple]:
        return (
            db_manager.query(AdventurerS1DataAppealAuditLog, AdventurerS1DataAppeal)
            .join(AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id)
            .filter(
                AdventurerS1DataAppeal.status == 'approved',
                AdventurerS1DataAppealAuditLog.reverted.is_(False),
                AdventurerS1DataAppealAuditLog.op_type.in_(('update', 'create_item')),
            )
            .order_by(AdventurerS1DataAppealAuditLog.id.asc())
            .all()
        )

    @staticmethod
    def _build_candidates(excluded: set[str]) -> tuple[list[dict], dict]:
        candidates: list[dict] = []
        rows = S1BugQtyShortfallService._fetch_rows()
        applied_qty_by_target: dict[tuple[int, str, int], int] = {}
        applied_qty_by_slot = S1BugAppealQtyGuard.applied_quantity_by_slot(rows)
        reclaim_slots = S1BugAppealQtyGuard.executed_reclaim_slots()
        for audit, appeal in rows:
            if audit.op_type != 'update' or audit.target_type not in ('equipment', 'warehouse'):
                continue
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if 'quantity' not in after:
                continue
            try:
                key = (int(appeal.id), str(audit.target_type), int(audit.target_id or 0))
                qty = S1BugQtyShortfallService._qty(after.get('quantity'))
            except (TypeError, ValueError):
                continue
            applied_qty_by_target[key] = max(qty, applied_qty_by_target.get(key, 0))
        scan = {
            'audit_rows': 0,
            'identity_without_quantity': 0,
            'create_default_quantity': 0,
            'auto_quantity_rows': 0,
            'text_parsed_quantity_rows': 0,
            'manual_quantity_rows': 0,
            'skipped_reclaim_conflict': 0,
            'skipped_applied_quantity': 0,
            'skipped_shortfall_executed': 0,
        }

        for audit, appeal in rows:
            scan['audit_rows'] += 1
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if audit.op_type == 'update' and audit.target_type in ('equipment', 'warehouse'):
                if not S1BugQtyShortfallService._item_identity_changed(before, after):
                    continue
                old_qty = S1BugQtyShortfallService._qty(before.get('quantity'), default=1)
                new_qty = S1BugQtyShortfallService._qty(after.get('quantity'), default=old_qty)
                if 'quantity' in after and new_qty != old_qty:
                    continue
                intended = S1BugQtyShortfallService._intended_update_quantity(
                    appeal, str(audit.target_type), int(audit.target_id or before.get('id') or after.get('id') or 0),
                )
                intended_qty = intended['quantity']
                quantity_source = intended['source']
                reason = '申诉已修改 name/custom_desc，但 quantity 未随同写入；已按原 patch 自动推算漏给量。'
                if intended_qty is None:
                    parsed = S1BugQtyShortfallService._parse_before_text_quantity(before)
                    identity_changed = S1BugQtyShortfallService._item_identity_changed(before, after)
                    if parsed['quantity'] is None:
                        continue
                    old_qty = S1BugQtyShortfallService._qty(before.get('quantity'), default=1)
                    text_removed = S1BugQtyShortfallService._quantity_text_removed_on_changed_fields(before, after)
                    if text_removed:
                        intended_qty = parsed['quantity']
                        quantity_source = f"before_{parsed['source_field']}"
                        reason = (
                            '申诉已修改 name/custom_desc 并去掉数量文案，但 quantity 字段未随同修正；'
                            f'已按原文 {parsed["source_field"]} 数量文案推算漏给量。'
                        )
                    elif identity_changed and int(parsed['quantity']) != old_qty:
                        intended_qty = parsed['quantity']
                        quantity_source = f"before_{parsed['source_field']}"
                        reason = (
                            '申诉已修改 name/custom_desc，但 quantity 字段未随同修正；'
                            f'已按改前 {parsed["source_field"]} 数量文案推算漏给量。'
                        )
                    else:
                        continue
                target_key = (int(appeal.id), str(audit.target_type), int(audit.target_id or before.get('id') or after.get('id') or 0))
                slot_key = (
                    int(audit.adventurer_id),
                    str(audit.target_type),
                    int(audit.target_id or before.get('id') or after.get('id') or 0),
                )
                if intended_qty is not None and applied_qty_by_target.get(target_key, 0) >= int(intended_qty):
                    continue
                if intended_qty is not None and applied_qty_by_slot.get(slot_key, 0) >= int(intended_qty):
                    scan['skipped_applied_quantity'] += 1
                    continue
                actual_qty = new_qty
                shortfall = max(0, int(intended_qty or 0) - actual_qty) if intended_qty is not None else 0
                if shortfall <= 0:
                    continue
                block_reason = S1BugAppealQtyGuard.shortfall_blocked_reason(
                    adventurer_id=int(audit.adventurer_id),
                    appeal_id=int(appeal.id),
                    target_type=str(audit.target_type),
                    target_id=int(audit.target_id or before.get('id') or after.get('id') or 0),
                    intended_quantity=int(intended_qty or 0),
                    applied_by_slot=applied_qty_by_slot,
                    reclaim_slots=reclaim_slots,
                )
                if block_reason:
                    if '回收' in block_reason:
                        scan['skipped_reclaim_conflict'] += 1
                    elif '漏补' in block_reason:
                        scan['skipped_shortfall_executed'] += 1
                    else:
                        scan['skipped_applied_quantity'] += 1
                    continue
                issue_key = f'qty_shortfall:update:{audit.id}'
                if issue_key in excluded:
                    continue
                scan['identity_without_quantity'] += 1
                if quantity_source and str(quantity_source).startswith('before_'):
                    scan['text_parsed_quantity_rows'] += 1
                else:
                    scan['auto_quantity_rows'] += 1
                candidates.append({
                    'issue_key': issue_key,
                    'issue_type': 'identity_update_quantity_missing',
                    'appeal_id': appeal.id,
                    'audit_log_id': audit.id,
                    'adventurer_id': audit.adventurer_id,
                    'user_id': appeal.user_id,
                    'target_type': audit.target_type,
                    'target_id': audit.target_id,
                    'item_name': after.get('name') or before.get('name') or '未知物品',
                    'item_type': after.get('type') or before.get('type') or '',
                    'item_rarity': after.get('rarity') or before.get('rarity') or 'common',
                    'intended_quantity': int(intended_qty or 0),
                    'actual_quantity': actual_qty,
                    'shortfall_quantity': shortfall,
                    'computed_shortfall_quantity': shortfall,
                    'quantity_adjusted': False,
                    'quantity_source': quantity_source,
                    'needs_admin_quantity': False,
                    'before': before,
                    'after': after,
                    'patch_item': intended['patch_item'],
                    'reason': reason,
                    'created_at': to_utc_iso(audit.created_at) if audit.created_at else None,
                })
                continue

            if audit.op_type == 'create_item':
                actual_qty = S1BugQtyShortfallService._qty(after.get('quantity'), default=1)
                if actual_qty != 1:
                    continue
                intended = S1BugQtyShortfallService._intended_create_quantity(appeal, after)
                intended_qty = intended['quantity']
                shortfall = max(0, int(intended_qty or 0) - actual_qty) if intended_qty is not None else 0
                if shortfall <= 0:
                    continue
                issue_key = f'qty_shortfall:create:{audit.id}'
                if issue_key in excluded:
                    continue
                scan['create_default_quantity'] += 1
                scan['auto_quantity_rows'] += 1
                candidates.append({
                    'issue_key': issue_key,
                    'issue_type': 'create_item_quantity_shortfall',
                    'appeal_id': appeal.id,
                    'audit_log_id': audit.id,
                    'adventurer_id': audit.adventurer_id,
                    'user_id': appeal.user_id,
                    'target_type': after.get('destination') or 'warehouse',
                    'target_id': None,
                    'item_name': after.get('name') or '未知物品',
                    'item_type': after.get('type') or '',
                    'item_rarity': after.get('rarity') or 'common',
                    'intended_quantity': int(intended_qty or 0),
                    'actual_quantity': actual_qty,
                    'shortfall_quantity': shortfall,
                    'computed_shortfall_quantity': shortfall,
                    'quantity_adjusted': False,
                    'quantity_source': intended['source'],
                    'needs_admin_quantity': False,
                    'before': {},
                    'after': after,
                    'patch_item': intended['patch_item'],
                    'reason': 'create_item 实际只落 1 件，但原 patch 指定更多数量。',
                    'created_at': to_utc_iso(audit.created_at) if audit.created_at else None,
                })

        candidates.sort(key=lambda c: (c.get('needs_admin_quantity', False), -int(c.get('shortfall_quantity') or 0), int(c.get('audit_log_id') or 0)))
        patch_intent, patch_scan = S1BugQtyShortfallService._build_patch_intent_candidates(
            excluded,
            applied_qty_by_slot=applied_qty_by_slot,
            reclaim_slots=reclaim_slots,
            existing_slots={
                (int(c.get('appeal_id') or 0), str(c.get('target_type') or ''), int(c.get('target_id') or 0))
                for c in candidates
                if c.get('target_id') is not None
            },
        )
        for key, value in patch_scan.items():
            scan[key] = scan.get(key, 0) + value
        candidates.extend(patch_intent)
        candidates.sort(key=lambda c: (c.get('needs_admin_quantity', False), -int(c.get('shortfall_quantity') or 0), int(c.get('audit_log_id') or 0)))
        return candidates, scan

    @staticmethod
    def _build_patch_intent_candidates(
        excluded: set[str],
        *,
        applied_qty_by_slot: dict,
        reclaim_slots: set,
        existing_slots: set[tuple[int, str, int]],
    ) -> tuple[list[dict], dict]:
        """raw patch 写了 quantity 增加但未落库（normalize 丢弃 / 零 audit 等）。"""
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService

        scan = {
            'patch_intent_rows': 0,
            'patch_intent_skipped_applied': 0,
            'patch_intent_skipped_missing_target': 0,
            'patch_intent_needs_admin': 0,
            'patch_intent_expedition_backed': 0,
        }
        candidates: list[dict] = []
        appeals = (
            db_manager.query(AdventurerS1DataAppeal)
            .filter(AdventurerS1DataAppeal.status == 'approved')
            .order_by(AdventurerS1DataAppeal.id.desc())
            .all()
        )
        for appeal in appeals:
            raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
            patch_raw = raw.get('patch') if isinstance(raw.get('patch'), dict) else {}
            snap = appeal.snapshot_before if isinstance(appeal.snapshot_before, dict) else {}
            for section in ('warehouse', 'equipment'):
                sec = patch_raw.get(section) if isinstance(patch_raw.get(section), dict) else {}
                for item in sec.get('update') or []:
                    if not isinstance(item, dict) or 'quantity' not in item or 'id' not in item:
                        continue
                    try:
                        target_id = int(item['id'])
                        intended_qty = S1BugQtyShortfallService._qty(item.get('quantity'))
                    except (TypeError, ValueError):
                        continue
                    original = next(
                        (r for r in (snap.get(section) or []) if isinstance(r, dict) and int(r.get('id') or 0) == target_id),
                        None,
                    )
                    if not original:
                        continue
                    old_qty = S1BugQtyShortfallService._qty(original.get('quantity'), default=1)
                    if intended_qty <= old_qty:
                        continue
                    slot_key = (int(appeal.adventurer_id), section, target_id)
                    if slot_key in existing_slots:
                        continue
                    if applied_qty_by_slot.get(slot_key, 0) >= intended_qty:
                        scan['patch_intent_skipped_applied'] += 1
                        continue
                    model = S1BugQtyShortfallService._target_model(section)
                    row = (
                        db_manager.query(model)
                        .filter(model.id == target_id, model.adventurer_id == appeal.adventurer_id)
                        .first()
                    )
                    if row is None:
                        scan['patch_intent_skipped_missing_target'] += 1
                        continue
                    actual_qty = item_quantity_of(row)
                    if actual_qty >= intended_qty:
                        continue
                    shortfall = max(0, intended_qty - actual_qty)
                    if shortfall <= 0:
                        continue
                    block_reason = S1BugAppealQtyGuard.shortfall_blocked_reason(
                        adventurer_id=int(appeal.adventurer_id),
                        appeal_id=int(appeal.id),
                        target_type=section,
                        target_id=target_id,
                        intended_quantity=intended_qty,
                        applied_by_slot=applied_qty_by_slot,
                        reclaim_slots=reclaim_slots,
                    )
                    if block_reason:
                        if '漏补' in block_reason:
                            scan['patch_intent_skipped_applied'] += 1
                        continue
                    issue_key = f'qty_shortfall:patch_intent:{appeal.id}:{section}:{target_id}'
                    if issue_key in excluded:
                        continue
                    evidence = appeal.expedition_evidence if isinstance(appeal.expedition_evidence, dict) else None
                    expedition_backed = AdventurerS1DataAppealService._expedition_evidence_supports_item_quantity_update(
                        original,
                        item,
                        evidence,
                    )
                    needs_admin = not expedition_backed and not any(
                        k in item for k in ('name', 'custom_desc')
                    )
                    scan['patch_intent_rows'] += 1
                    if needs_admin:
                        scan['patch_intent_needs_admin'] += 1
                    else:
                        scan['patch_intent_expedition_backed'] += 1
                    reason = (
                        '申诉 raw patch 写了 quantity 增加，但 normalize/apply 未落库；'
                        '已按原 patch 推算漏给量，可在预览中一键补发。'
                    )
                    if expedition_backed:
                        reason += '（探险 member_update 可佐证该堆数量诉求）'
                    elif needs_admin:
                        reason += '（纯 quantity 增量且无探险佐证，执行前请人工确认）'
                    candidates.append({
                        'issue_key': issue_key,
                        'issue_type': 'patch_intent_quantity_not_landed',
                        'appeal_id': appeal.id,
                        'audit_log_id': None,
                        'adventurer_id': appeal.adventurer_id,
                        'user_id': appeal.user_id,
                        'target_type': section,
                        'target_id': target_id,
                        'item_name': row.name or original.get('name') or '未知物品',
                        'item_type': row.type or original.get('type') or '',
                        'item_rarity': row.rarity or original.get('rarity') or 'common',
                        'intended_quantity': intended_qty,
                        'actual_quantity': actual_qty,
                        'shortfall_quantity': shortfall,
                        'computed_shortfall_quantity': shortfall,
                        'quantity_adjusted': False,
                        'quantity_source': 'ai_raw_patch',
                        'needs_admin_quantity': needs_admin,
                        'expedition_backed': expedition_backed,
                        'before': original,
                        'after': {
                            'id': target_id,
                            'name': row.name,
                            'quantity': actual_qty,
                            'custom_desc': row.custom_desc,
                        },
                        'patch_item': item,
                        'reason': reason,
                        'created_at': to_utc_iso(appeal.applied_at or appeal.updated_at),
                    })
        return candidates, scan

    @staticmethod
    def _user_map(ids: set[int]) -> dict[int, User]:
        if not ids:
            return {}
        return {u.id: u for u in db_manager.query(User).filter(User.id.in_(ids)).all()}

    @staticmethod
    def _adventurer_map(ids: set[int]) -> dict[int, AdventurerS1]:
        if not ids:
            return {}
        return {a.id: a for a in db_manager.query(AdventurerS1).filter(AdventurerS1.id.in_(ids)).all()}

    @staticmethod
    def _apply_adjustments(item: dict, adjustments: dict[str, dict]) -> dict:
        adj = adjustments.get(str(item['issue_key']))
        if not adj:
            return item
        qty = S1BugQtyShortfallService._qty(adj.get('shortfall_quantity'), default=item.get('shortfall_quantity') or 0)
        return {
            **item,
            'shortfall_quantity': qty,
            'quantity_adjusted': qty != int(item.get('computed_shortfall_quantity') or 0),
            'adjustment_note': adj.get('note') or '',
            'needs_admin_quantity': False if qty > 0 else item.get('needs_admin_quantity'),
        }

    @staticmethod
    def _enrich(item: dict, adv_map: dict[int, AdventurerS1], user_map: dict[int, User]) -> dict:
        adv = adv_map.get(int(item['adventurer_id']))
        user = user_map.get(int(item['user_id'])) if item.get('user_id') else None
        return {
            **item,
            'adventurer_name': adv.name if adv else '未知冒险者',
            'username': user.username if user else None,
            'nickname': user.nickname if user else None,
        }

    @staticmethod
    def _summary(items: list[dict], total: int, scan: dict) -> dict:
        return {
            'total_candidates': total,
            'shown_count': len(items),
            'shortfall_quantity': sum(int(i.get('shortfall_quantity') or 0) for i in items),
            'computed_shortfall_quantity': sum(int(i.get('computed_shortfall_quantity') or 0) for i in items),
            'needs_admin_quantity': sum(1 for i in items if i.get('needs_admin_quantity')),
            'expedition_backed_rows': sum(1 for i in items if i.get('expedition_backed')),
            'auto_quantity_rows': scan.get('auto_quantity_rows') or 0,
            'text_parsed_quantity_rows': scan.get('text_parsed_quantity_rows') or 0,
            'manual_quantity_rows': scan.get('manual_quantity_rows') or 0,
            'scan': scan,
        }

    @staticmethod
    def preview(limit: int = 50, admin_user_id: int | None = None) -> tuple:
        limit = max(1, min(int(limit or 50), 200))
        candidates, scan = S1BugQtyShortfallService._build_candidates(S1BugQtyShortfallService._excluded_issue_keys())
        adjustments = S1BugQtyShortfallService._adjustments()
        items = []
        for candidate in candidates:
            item = S1BugQtyShortfallService._apply_adjustments(candidate, adjustments)
            if int(item.get('shortfall_quantity') or 0) <= 0:
                continue
            items.append(item)
            if len(items) >= limit:
                break
        adv_map = S1BugQtyShortfallService._adventurer_map({int(i['adventurer_id']) for i in items})
        user_map = S1BugQtyShortfallService._user_map({int(i['user_id']) for i in items if i.get('user_id')})
        items = [S1BugQtyShortfallService._enrich(i, adv_map, user_map) for i in items]
        result = {'items': items, 'summary': S1BugQtyShortfallService._summary(items, len(candidates), scan)}
        if admin_user_id is not None:
            result['snapshot_token'] = S1AdminScanSnapshotService.store(
                'qty_shortfalls', admin_user_id, result,
            )
        return result, None

    @staticmethod
    def _delete_existing_adjustment(issue_key: str) -> None:
        rows = (
            db_manager.query(AdventurerS1BugQtyShortfallItem)
            .filter(
                AdventurerS1BugQtyShortfallItem.issue_key == issue_key,
                AdventurerS1BugQtyShortfallItem.status == 'adjusted',
            )
            .all()
        )
        for row in rows:
            if row.batch:
                db_manager.delete_instance(row.batch)
            else:
                db_manager.delete_instance(row)

    @staticmethod
    def adjust_quantity(admin_user_id: int, issue_key: str, shortfall_quantity, item: dict | None = None, note: str = '') -> tuple:
        key = str(issue_key or '').strip()
        if not key:
            return None, 'issue_key 不能为空'
        try:
            qty = int(shortfall_quantity)
        except (TypeError, ValueError):
            return None, '补发数量必须是整数'
        if qty < 0:
            return None, '补发数量不能小于 0'

        blocked = (
            db_manager.query(AdventurerS1BugQtyShortfallItem.id)
            .filter(
                AdventurerS1BugQtyShortfallItem.issue_key == key,
                AdventurerS1BugQtyShortfallItem.status.in_(('executed', 'dismissed')),
            )
            .first()
        )
        if blocked:
            return None, '该条目已执行或已标记误报'

        payload = item if isinstance(item, dict) else {}
        computed = int(payload.get('computed_shortfall_quantity') or payload.get('shortfall_quantity') or 0)
        if qty == computed:
            try:
                with db_manager.transaction():
                    S1BugQtyShortfallService._delete_existing_adjustment(key)
            except Exception as exc:
                return None, f'恢复系统计算值失败：{exc}'
            return {'reverted': True, 'item': {**payload, 'shortfall_quantity': computed, 'quantity_adjusted': False}}, None

        now = _portable_utc_now()
        note_text = str(note or '').strip()[:200]
        result_meta = {'action': 'adjust_shortfall_quantity', 'shortfall_quantity': qty, 'computed_shortfall_quantity': computed, 'note': note_text}
        try:
            with db_manager.transaction():
                S1BugQtyShortfallService._delete_existing_adjustment(key)
                batch = AdventurerS1BugQtyShortfallBatch(
                    admin_user_id=admin_user_id,
                    status='adjusted',
                    criteria_json={'issue_key': key, 'mode': 'adjust_shortfall_quantity'},
                    summary_json={
                        'action': 'adjust_shortfall_quantity',
                        'issue_key': key,
                        'item_name': payload.get('item_name'),
                        'computed_shortfall_quantity': computed,
                        'shortfall_quantity': qty,
                    },
                    note=note_text or f'数量认定 {computed}→{qty}',
                    executed_at=now,
                )
                row = AdventurerS1BugQtyShortfallItem(
                    batch=batch,
                    status='adjusted',
                    issue_key=key,
                    issue_type=str(payload.get('issue_type') or 'quantity_shortfall')[:40],
                    appeal_id=payload.get('appeal_id'),
                    audit_log_id=payload.get('audit_log_id'),
                    adventurer_id=int(payload.get('adventurer_id') or 0),
                    user_id=payload.get('user_id'),
                    item_name=str(payload.get('item_name') or key)[:100],
                    item_type=str(payload.get('item_type') or '')[:50],
                    item_rarity=str(payload.get('item_rarity') or 'common')[:20],
                    intended_quantity=int(payload.get('intended_quantity') or 0),
                    actual_quantity=int(payload.get('actual_quantity') or 0),
                    shortfall_quantity=qty,
                    granted_quantity=0,
                    before_json=payload.get('before') or {},
                    after_json=payload.get('after') or {},
                    route_json=[],
                    result_json=result_meta,
                    executed_by=admin_user_id,
                    executed_at=now,
                )
                db_manager.add(batch)
                db_manager.add(row)
        except Exception as exc:
            return None, f'认定补发数量失败：{exc}'

        updated = {**payload, 'shortfall_quantity': qty, 'quantity_adjusted': True, 'adjustment_note': note_text, 'needs_admin_quantity': False}
        return {'reverted': False, 'item': updated}, None

    @staticmethod
    def dismiss(admin_user_id: int, issue_key: str, item: dict | None = None, note: str = '') -> tuple:
        key = str(issue_key or '').strip()
        if not key:
            return None, 'issue_key 不能为空'
        payload = item if isinstance(item, dict) else {}
        now = _portable_utc_now()
        try:
            with db_manager.transaction():
                batch = AdventurerS1BugQtyShortfallBatch(
                    admin_user_id=admin_user_id,
                    status='dismissed',
                    criteria_json={'issue_key': key},
                    summary_json={'issue_key': key, 'item_name': payload.get('item_name'), 'action': 'dismiss'},
                    note=str(note or '管理员标记误报')[:200],
                    executed_at=now,
                )
                row = AdventurerS1BugQtyShortfallItem(
                    batch=batch,
                    status='dismissed',
                    issue_key=key,
                    issue_type=str(payload.get('issue_type') or 'quantity_shortfall')[:40],
                    appeal_id=payload.get('appeal_id'),
                    audit_log_id=payload.get('audit_log_id'),
                    adventurer_id=int(payload.get('adventurer_id') or 0),
                    user_id=payload.get('user_id'),
                    item_name=str(payload.get('item_name') or key)[:100],
                    item_type=str(payload.get('item_type') or '')[:50],
                    item_rarity=str(payload.get('item_rarity') or 'common')[:20],
                    intended_quantity=int(payload.get('intended_quantity') or 0),
                    actual_quantity=int(payload.get('actual_quantity') or 0),
                    shortfall_quantity=int(payload.get('shortfall_quantity') or 0),
                    before_json=payload.get('before') or {},
                    after_json=payload.get('after') or {},
                    result_json={'action': 'dismiss', 'note': str(note or '')[:200]},
                    executed_by=admin_user_id,
                    executed_at=now,
                )
                db_manager.add(batch)
                db_manager.add(row)
        except Exception as exc:
            return None, f'标记误报失败：{exc}'
        return {'issue_key': key}, None

    @staticmethod
    def _target_model(target_type: str):
        return AdventurerS1Equipment if target_type == 'equipment' else AdventurerS1WarehouseItem

    @staticmethod
    def _execute_one(
        item: dict,
        batch: AdventurerS1BugQtyShortfallBatch,
        admin_user_id: int,
        now: datetime,
        existing_issue_keys: set[str] | None = None,
    ) -> dict:
        issue_key = str(item['issue_key'])
        if existing_issue_keys is None:
            existing_issue_keys = {
                issue_key,
            } if db_manager.query(AdventurerS1BugQtyShortfallItem.id).filter(
                AdventurerS1BugQtyShortfallItem.issue_key == issue_key,
                AdventurerS1BugQtyShortfallItem.status.in_(('executed', 'dismissed')),
            ).first() else set()
        if issue_key in existing_issue_keys:
            return {**item, 'status': 'skipped', 'result': {'message': '已处理或已标记误报，跳过'}}

        qty = int(item.get('shortfall_quantity') or 0)
        if qty <= 0:
            return {**item, 'status': 'skipped', 'result': {'message': '补发数量为 0，跳过'}}

        adv_id = int(item['adventurer_id'])
        target_type = str(item.get('target_type') or '')
        target_id = item.get('target_id')
        lock_warehouse_aggregate(adv_id)
        row = None
        if target_type in ('equipment', 'warehouse') and target_id:
            model = S1BugQtyShortfallService._target_model(target_type)
            row = (
                db_manager.query(model)
                .filter(model.id == int(target_id), model.adventurer_id == adv_id)
                .with_for_update()
                .first()
            )
        if row is None:
            for model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
                row = (
                    db_manager.query(model)
                    .filter(model.adventurer_id == adv_id, model.name == item.get('item_name'))
                    .with_for_update()
                    .first()
                )
                if row:
                    target_type = 'equipment' if model is AdventurerS1Equipment else 'warehouse'
                    break

        before_state = None
        after_state = None
        actual_destination = target_type or str(item.get('after', {}).get('destination') or 'warehouse')
        if row is not None:
            before_state = {'id': row.id, 'quantity': item_quantity_of(row), 'name': row.name, 'custom_desc': row.custom_desc}
            row.quantity = item_quantity_of(row) + qty
            after_state = {'id': row.id, 'quantity': item_quantity_of(row), 'name': row.name, 'custom_desc': row.custom_desc}
            actual_destination = target_type
            change_type = 'gain' if actual_destination == 'equipment' else 'overflow'
            db_manager.add(AdventurerS1ItemLog(
                adventurer_id=adv_id,
                item_name=row.name,
                item_type=row.type or item.get('item_type') or '',
                item_rarity=row.rarity or item.get('item_rarity') or 'common',
                change_type=change_type,
                quantity=qty,
                source_type='data_appeal_qty_shortfall',
                description=f'管理员补发申诉漏给数量「{row.name}」×{qty}（#{item.get("appeal_id") or "-"}）',
            ))
        else:
            adv = db_manager.query(AdventurerS1).filter(AdventurerS1.id == adv_id).with_for_update().first()
            if not adv:
                raise RuntimeError('冒险者不存在')
            from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
            data = {
                'type': item.get('item_type') or '',
                'rarity': item.get('item_rarity') or 'common',
                'quantity': qty,
                'desc': (item.get('after') or {}).get('custom_desc') or (item.get('before') or {}).get('custom_desc') or '',
            }
            actual_destination, err = grant_raw_item(
                adv,
                item.get('item_name') or '申诉漏给物品',
                data,
                source_type='data_appeal_qty_shortfall',
                description=f'管理员补发申诉漏给数量「{item.get("item_name") or "物品"}」×{qty}（#{item.get("appeal_id") or "-"}）',
                force_destination=actual_destination if actual_destination in ('equipment', 'warehouse') else 'warehouse',
            )
            if err:
                raise RuntimeError(err)
            after_state = {'destination': actual_destination, 'quantity': qty, 'name': item.get('item_name')}

        result_json = {
            'granted_quantity': qty,
            'destination': actual_destination,
            'target_before': before_state,
            'target_after': after_state,
        }
        db_manager.add(AdventurerS1BugQtyShortfallItem(
            batch=batch,
            status='executed',
            issue_key=issue_key,
            issue_type=item['issue_type'],
            appeal_id=item.get('appeal_id'),
            audit_log_id=item.get('audit_log_id'),
            adventurer_id=adv_id,
            user_id=item.get('user_id'),
            item_name=str(item.get('item_name') or '')[:100],
            item_type=str(item.get('item_type') or '')[:50],
            item_rarity=str(item.get('item_rarity') or 'common')[:20],
            intended_quantity=int(item.get('intended_quantity') or 0),
            actual_quantity=int(item.get('actual_quantity') or 0),
            shortfall_quantity=qty,
            granted_quantity=qty,
            before_json=item.get('before') or {},
            after_json=item.get('after') or {},
            route_json=[{'kind': 'increment_existing' if before_state else 'grant_replacement', 'quantity': qty, 'destination': actual_destination}],
            result_json=result_json,
            executed_by=admin_user_id,
            executed_at=now,
        ))
        existing_issue_keys.add(issue_key)
        return {**item, 'status': 'executed', 'result': result_json}

    @staticmethod
    def execute(
        admin_user_id: int,
        limit: int = 50,
        note: str = '',
        preview_token: str = '',
    ) -> tuple:
        preview = None
        if preview_token:
            preview = S1AdminScanSnapshotService.load('qty_shortfalls', admin_user_id, preview_token)
            if preview is None:
                return None, '预览快照已过期，请重新预览后再执行'
        if preview is None:
            preview, err = S1BugQtyShortfallService.preview(limit=limit)
            if err:
                return None, err
        items = [i for i in (preview.get('items') or []) if int(i.get('shortfall_quantity') or 0) > 0]
        if preview_token and items:
            adjustments = S1BugQtyShortfallService._adjustments()
            items = [S1BugQtyShortfallService._apply_adjustments(item, adjustments) for item in items]
            items = [item for item in items if int(item.get('shortfall_quantity') or 0) > 0]
        auto_items = [i for i in items if not i.get('needs_admin_quantity')]
        if not auto_items:
            needs_admin = [i for i in items if i.get('needs_admin_quantity')]
            if needs_admin:
                return None, (
                    f'本批 {len(needs_admin)} 项需先逐条认定补发数量或标误报，'
                    '无探险佐证且为纯 quantity 增量时不支持一键全批'
                )
            return None, '没有可执行的补发项；请先为待人工认定项填写补发数量'
        skipped_admin = len(items) - len(auto_items)
        now = _portable_utc_now()
        summary = S1BugQtyShortfallService._summary(auto_items, len(auto_items), preview.get('summary', {}).get('scan') or {})
        if skipped_admin:
            summary['skipped_needs_admin_quantity'] = skipped_admin
        try:
            with db_manager.transaction():
                adventurer_ids = sorted({int(item['adventurer_id']) for item in auto_items})
                locked_adventurers = (
                    db_manager.query(AdventurerS1)
                    .filter(AdventurerS1.id.in_(adventurer_ids))
                    .order_by(AdventurerS1.id.asc())
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                if len(locked_adventurers) != len(adventurer_ids):
                    raise RuntimeError('部分冒险者不存在，无法执行补发')
                for adventurer_id in adventurer_ids:
                    lock_warehouse_aggregate(adventurer_id)

                batch = AdventurerS1BugQtyShortfallBatch(
                    admin_user_id=admin_user_id,
                    status='executed',
                    criteria_json={'limit': limit},
                    summary_json=summary,
                    note=str(note or '')[:200],
                    executed_at=now,
                )
                db_manager.add(batch)
                issue_keys = {str(item['issue_key']) for item in auto_items}
                existing_issue_keys = {
                    str(row[0])
                    for row in db_manager.query(AdventurerS1BugQtyShortfallItem.issue_key).filter(
                        AdventurerS1BugQtyShortfallItem.issue_key.in_(issue_keys),
                        AdventurerS1BugQtyShortfallItem.status.in_(('executed', 'dismissed')),
                    ).all()
                } if issue_keys else set()
                results = [
                    S1BugQtyShortfallService._execute_one(
                        item, batch, admin_user_id, now, existing_issue_keys,
                    )
                    for item in auto_items
                ]
                summary['count'] = sum(1 for r in results if r.get('status') == 'executed')
                summary['granted_quantity'] = sum(int(r.get('result', {}).get('granted_quantity') or 0) for r in results)
                batch.summary_json = summary
        except Exception as exc:
            return None, f'执行异常数量补发失败：{exc}'
        S1AdminScanSnapshotService.delete('qty_shortfalls', admin_user_id, preview_token)
        return {'batch': batch.to_dict(), 'summary': summary, 'items': results}, None

    @staticmethod
    def history(page: int = 1, per_page: int = 20) -> tuple:
        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or 20), 100))
        q = db_manager.query(AdventurerS1BugQtyShortfallBatch).order_by(AdventurerS1BugQtyShortfallBatch.id.desc())
        total = q.count()
        rows = q.offset((page - 1) * per_page).limit(per_page).all()
        return {
            'items': [r.to_dict() for r in rows],
            'total': total,
            'page': page,
            'per_page': per_page,
        }, None
