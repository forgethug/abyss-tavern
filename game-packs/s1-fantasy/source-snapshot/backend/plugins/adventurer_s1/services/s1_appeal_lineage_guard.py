"""申诉 patch lineage 守卫：阻止「快照已有同名物仍 create_items 拆分」及重复链路改量/补发。"""
from __future__ import annotations

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog
from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService


def _snapshot_siblings_for_guard(snapshot: dict, item_name: str) -> list[dict]:
    normalized = S1BugReclaimService._normalize_name(item_name)
    if not normalized:
        return []
    return S1BugReclaimService._snapshot_siblings(snapshot, normalized, set())


def _item_group_key(scope_key: tuple[str, int], item_name: str, item_type: str, item_rarity: str) -> tuple:
    return (
        scope_key,
        S1BugReclaimService._catalog_fuzzy_name(item_name),
        str(item_type or ''),
        str(item_rarity or 'common'),
    )


def _effective_item_desc(rec: dict) -> str:
    return str(
        rec.get('custom_desc')
        or rec.get('catalog_desc')
        or rec.get('description')
        or ''
    ).strip()


def is_distinct_desc_upgrade(item: dict, siblings: list[dict]) -> bool:
    """同名但描述与快照中全部同名条目不同 → 装备升级/进化补发，允许 create_items。"""
    if not siblings:
        return False
    new_desc = _effective_item_desc(item)
    if not new_desc:
        return False
    for sib in siblings:
        if _effective_item_desc(sib) == new_desc:
            return False
    return True


class S1AppealLineageGuard:
    @staticmethod
    def submission_block_reasons(
        adventurer_id: int,
        snapshot_before: dict,
        player_note: str | None,
    ) -> list[str]:
        """玩家自然语言不做后端语义拦截；收益链由结构化 patch 守卫处理。"""
        return []

    @staticmethod
    def _fetch_lineage_audit_rows(
        adventurer_id: int,
        *,
        expedition_log_id: int | None = None,
        exclude_appeal_id: int | None = None,
    ):
        q = (
            db_manager.query(AdventurerS1DataAppealAuditLog, AdventurerS1DataAppeal)
            .join(AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id)
            .filter(
                AdventurerS1DataAppeal.status == 'approved',
                AdventurerS1DataAppealAuditLog.reverted.is_(False),
                AdventurerS1DataAppealAuditLog.op_type.in_(('update', 'create_item', 'delete')),
            )
            .order_by(AdventurerS1DataAppealAuditLog.id.asc())
        )
        if expedition_log_id is not None:
            q = q.filter(AdventurerS1DataAppeal.expedition_log_id == int(expedition_log_id))
        else:
            q = q.filter(AdventurerS1DataAppealAuditLog.adventurer_id == int(adventurer_id))
        if exclude_appeal_id is not None:
            q = q.filter(AdventurerS1DataAppeal.id != int(exclude_appeal_id))
        return q.all()

    @staticmethod
    def build_state(adventurer_id: int, *, exclude_appeal_id: int | None = None) -> dict:
        """基于已通过且真实落库的申诉 audit 构建 lineage 守卫状态（不含 exclude_appeal_id 当前单）。"""
        expedition_log_id = None
        if exclude_appeal_id is not None:
            current_appeal = (
                db_manager.query(AdventurerS1DataAppeal)
                .filter(AdventurerS1DataAppeal.id == int(exclude_appeal_id))
                .first()
            )
            if current_appeal and current_appeal.expedition_log_id is not None:
                expedition_log_id = int(current_appeal.expedition_log_id)
            elif current_appeal and isinstance(current_appeal.expedition_evidence, dict):
                try:
                    evidence_log_id = int(current_appeal.expedition_evidence.get('expedition_log_id') or 0)
                    if evidence_log_id > 0:
                        expedition_log_id = evidence_log_id
                except (TypeError, ValueError):
                    pass

        scope_key = (
            ('expedition', expedition_log_id)
            if expedition_log_id is not None
            else ('adventurer', int(adventurer_id))
        )
        rows = S1AppealLineageGuard._fetch_lineage_audit_rows(
            adventurer_id,
            expedition_log_id=expedition_log_id,
            exclude_appeal_id=exclude_appeal_id,
        )
        appeal_deleted = S1BugReclaimService._appeal_deleted_slots(rows)
        quantity_net_by_appeal_slot: dict[tuple[int, tuple[int, str, int]], dict] = {}
        for audit, appeal in rows:
            if audit.op_type != 'update' or audit.target_type not in ('equipment', 'warehouse'):
                continue
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if 'quantity' not in after:
                continue
            slot = S1BugReclaimService._target_slot(audit, before, after)
            if not slot:
                continue
            try:
                old_qty = S1BugReclaimService._qty(before.get('quantity'))
                new_qty = S1BugReclaimService._qty(after.get('quantity'))
            except Exception:
                continue
            info = quantity_net_by_appeal_slot.setdefault((int(appeal.id), slot), {
                'start': old_qty,
                'end': old_qty,
            })
            info['end'] = new_qty
        lineages: dict[tuple, dict] = {}

        for audit, appeal in rows:
            if audit.op_type != 'create_item':
                continue
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            item_name = S1BugReclaimService._text(after.get('name'))
            if not item_name:
                continue
            normalized = S1BugReclaimService._normalize_name(item_name)
            snapshot = appeal.snapshot_before if isinstance(appeal.snapshot_before, dict) else {}
            siblings = S1BugReclaimService._snapshot_siblings(
                snapshot,
                normalized,
                appeal_deleted.get(int(appeal.id), set()),
            )
            if not siblings:
                continue
            if is_distinct_desc_upgrade(after, siblings):
                continue
            item_type = str(after.get('type') or '')
            rarity = str(after.get('rarity') or 'common')
            gkey = _item_group_key(scope_key, item_name, item_type, rarity)
            prev = lineages.get(gkey)
            seed_id = int(appeal.id)
            if prev:
                seed_id = min(int(prev['seed_appeal_id']), seed_id)
            lineages[gkey] = {
                'seed_appeal_id': seed_id,
                'fuzzy_name': S1BugReclaimService._catalog_fuzzy_name(item_name),
            }

        seeded_groups = set(lineages.keys())
        plain_qty_first_done: set[tuple] = set()
        split_appeal_by_group = {
            gkey: int(info['seed_appeal_id'])
            for gkey, info in lineages.items()
        }

        for audit, appeal in rows:
            if audit.op_type != 'update' or audit.target_type not in ('equipment', 'warehouse'):
                continue
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if 'quantity' not in after:
                continue
            old_qty = S1BugReclaimService._qty(before.get('quantity'))
            new_qty = S1BugReclaimService._qty(after.get('quantity'))
            if new_qty <= old_qty:
                continue
            slot = S1BugReclaimService._target_slot(audit, before, after)
            if slot:
                net = quantity_net_by_appeal_slot.get((int(appeal.id), slot)) or {}
                if int(net.get('end') or old_qty) <= int(net.get('start') or old_qty):
                    continue
            item_name = after.get('name') or before.get('name')
            if not item_name:
                continue
            item_type = str(after.get('type') or before.get('type') or '')
            rarity = str(after.get('rarity') or before.get('rarity') or 'common')
            gkey = _item_group_key(scope_key, str(item_name), item_type, rarity)
            plain_qty_first_done.add(gkey)
            previous_appeal_id = split_appeal_by_group.get(gkey)
            if previous_appeal_id is None or int(appeal.id) < previous_appeal_id:
                split_appeal_by_group[gkey] = int(appeal.id)

        split_groups = seeded_groups | plain_qty_first_done

        return {
            'scope_key': scope_key,
            'seeded_groups': seeded_groups,
            'plain_qty_first_done': plain_qty_first_done,
            'split_groups': split_groups,
            'split_appeal_by_group': split_appeal_by_group,
        }

    @staticmethod
    def sanitize_patch(
        normalized: dict,
        *,
        snapshot_before: dict,
        adventurer_id: int | None,
        appeal_id: int | None = None,
        record_maps: dict | None = None,
        quantity_norm_checker=None,
        expedition_shortfall_checker=None,
    ) -> tuple[dict, list[str]]:
        """剥离 lineage 非法 create_items / quantity 增量，返回 (patch, warnings)。"""
        if not isinstance(normalized, dict) or not adventurer_id:
            return normalized, []

        state = S1AppealLineageGuard.build_state(
            int(adventurer_id),
            exclude_appeal_id=appeal_id,
        )
        plain_qty_first_done = state['plain_qty_first_done']
        split_groups = state['split_groups']
        split_appeal_by_group = state['split_appeal_by_group']
        scope_key = state['scope_key']
        warnings: list[str] = []

        if not split_groups:
            # 仍可能拦截「快照已有同名物仍 create_items」的首次拆分
            pass

        kept_create: list[dict] = []
        for item in normalized.get('create_items') or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            item_type = str(item.get('type') or '')
            rarity = str(item.get('rarity') or 'common')
            gkey = _item_group_key(scope_key, name, item_type, rarity)

            siblings = _snapshot_siblings_for_guard(snapshot_before, name)
            if siblings and not is_distinct_desc_upgrade(item, siblings):
                warnings.append(
                    f'物品「{name}」快照中已有同名条目，禁止 create_items 拆分补发，'
                    f'请 update 原条目归位数量/名称'
                )
                continue

            if gkey in split_groups:
                seed_id = split_appeal_by_group.get(gkey)
                warnings.append(
                    f'物品「{name}」的源头已在申诉 #{seed_id} 拆分过，'
                    f'禁止再次拆分补发'
                )
                continue

            kept_create.append(item)

        out = dict(normalized)
        out['create_items'] = kept_create

        record_maps = record_maps if isinstance(record_maps, dict) else {}
        for section in ('equipment', 'warehouse'):
            sec = out.get(section) if isinstance(out.get(section), dict) else {}
            updates = list(sec.get('update') or [])
            if not updates:
                continue
            section_has_deletes = bool(sec.get('delete'))
            new_updates: list[dict] = []
            for upd in updates:
                if not isinstance(upd, dict) or upd.get('id') is None:
                    continue
                if 'quantity' not in upd:
                    new_updates.append(upd)
                    continue
                try:
                    rid = int(upd['id'])
                    new_qty = int(upd['quantity'])
                except (TypeError, ValueError):
                    new_updates.append(upd)
                    continue
                original = (record_maps.get(section) or {}).get(rid) or {}
                try:
                    old_qty = max(1, int(original.get('quantity') or 1))
                except (TypeError, ValueError):
                    old_qty = 1
                if new_qty <= old_qty:
                    new_updates.append(upd)
                    continue

                item_name = str(original.get('name') or upd.get('name') or '').strip()
                if not item_name:
                    new_updates.append(upd)
                    continue
                item_type = str(original.get('type') or upd.get('type') or '')
                rarity = str(original.get('rarity') or upd.get('rarity') or 'common')
                gkey = _item_group_key(scope_key, item_name, item_type, rarity)

                # 探险正文确认的应得总量与 member_update 实际到账不一致，属于结算漏发归位，
                # 不是把既有物品源头再次拆成收益。该语义由申诉 AI/二审裁决，后端这里只
                # 接受调用方基于已关联 expedition_evidence 做出的结构化判定。
                is_expedition_shortfall = False
                if expedition_shortfall_checker is not None:
                    is_expedition_shortfall = bool(
                        expedition_shortfall_checker(section, upd, original)
                    )
                if is_expedition_shortfall and gkey not in plain_qty_first_done:
                    new_updates.append(upd)
                    continue

                if gkey in split_groups:
                    seed_id = split_appeal_by_group.get(gkey)
                    warnings.append(
                        f'「{item_name}」的源头已在申诉 #{seed_id} 拆分过，'
                        f'禁止再次增加 quantity（+{new_qty - old_qty} 已拦截）'
                    )
                    cleaned = {k: v for k, v in upd.items() if k != 'quantity'}
                    if len(cleaned) > 1:
                        new_updates.append(cleaned)
                    elif len(cleaned) == 1 and 'id' in cleaned:
                        pass
                    continue

                # 数量归一只豁免“该模糊同名链路从未 create、也从未完成过纯改量归一”的首次修正。
                # 历史 lineage 检查必须先于此处，否则改名/描述/同段 delete 会把重复增量包装成归一并绕过。
                is_qty_norm = False
                if quantity_norm_checker is not None:
                    is_qty_norm = bool(quantity_norm_checker(upd, original, section_has_deletes))
                if is_qty_norm:
                    new_updates.append(upd)
                    continue

                new_updates.append(upd)

            sec = dict(sec)
            sec['update'] = new_updates
            out[section] = sec

        return out, warnings
