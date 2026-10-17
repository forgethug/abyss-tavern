"""S1 物品图鉴历史版本对当前资产快照的管理员修复。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from flask import current_app, has_app_context

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1Item, AdventurerS1Settings
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_item_catalog_service import (
    S1ItemCatalogService,
)
from plugins.adventurer_s1.services.s1_item_use_condition_service import (
    catalog_default_uses_left,
    catalog_has_default_uses_left,
    is_catalog_immutable_item,
    is_hidden_boss_clue_item,
)


class S1ItemCatalogHistoryRepairService:
    """按管理员提供的旧图鉴快照，修复仍未定制的当前资产。"""

    SNAPSHOT_OPERATION = 'catalog_history_sync_repair'
    BACKUP_KEY_PREFIX = 'catalog_history_sync_backup:'
    BACKUP_RUN_LIMIT = 5
    SAMPLE_LIMIT = 24

    @staticmethod
    def _text(value: object) -> str:
        return str(value or '').strip()

    @classmethod
    def _backup_key(cls, item_id: int) -> str:
        return f'{cls.BACKUP_KEY_PREFIX}{int(item_id)}'

    @staticmethod
    def _finalize_counts(counts: dict) -> dict:
        counts['market_total'] = (
            counts['market_listings']
            + counts['market_offers']
            + counts['market_buy_offers']
            + counts['secret_auction_lots']
        )
        counts['total'] = (
            counts['equipment']
            + counts['warehouse']
            + counts['market_total']
        )
        return counts

    @staticmethod
    def _candidate_limit() -> int:
        return max(1, int(current_app.config.get(
            'ADVENTURER_S1_CATALOG_HISTORY_REPAIR_MAX_ENTRIES',
            2000,
        )))

    @classmethod
    def _catalog_snapshot(cls, item: AdventurerS1Item) -> dict:
        has_default = catalog_has_default_uses_left(item.use_condition)
        return {
            'id': int(item.id),
            'name': cls._text(item.name),
            'type': cls._text(item.type),
            'description': cls._text(item.description),
            'rarity': cls._text(item.rarity),
            'is_immutable': is_catalog_immutable_item(item.use_condition),
            'has_default_uses_left': has_default,
            'default_uses_left': (
                catalog_default_uses_left(item.use_condition)
                if has_default
                else None
            ),
        }

    @classmethod
    def _parse_old_snapshot(cls, body: dict) -> tuple[Optional[dict], Optional[str]]:
        raw = body.get('old_snapshot')
        if not isinstance(raw, dict):
            return None, '请填写修改前的完整图鉴值'
        required = {'name', 'type', 'description', 'rarity'}
        if not required.issubset(raw):
            return None, '修改前图鉴必须包含名称、类型、描述和稀有度'

        name = cls._text(raw.get('name'))
        item_type = cls._text(raw.get('type'))
        rarity = cls._text(raw.get('rarity'))
        if not name:
            return None, '修改前物品名称不能为空'
        if not item_type:
            return None, '修改前物品类型不能为空'
        if not rarity:
            return None, '修改前稀有度不能为空'

        has_immutable = 'is_immutable' in raw
        old_immutable = None
        if has_immutable:
            old_immutable = raw.get('is_immutable')
            if not isinstance(old_immutable, bool):
                return None, '修改前不可变属性必须是布尔值'

        has_default = 'default_uses_left' in raw
        default_uses = None
        if has_default:
            value = raw.get('default_uses_left')
            if isinstance(value, bool) or (
                isinstance(value, float)
                and not value.is_integer()
            ):
                return None, '修改前默认使用次数无效'
            try:
                default_uses = int(value)
            except (TypeError, ValueError):
                return None, '修改前默认使用次数无效'
            if default_uses < 0:
                return None, '修改前默认使用次数不能为负'

        return {
            'name': name,
            'type': item_type,
            'description': cls._text(raw.get('description')),
            'rarity': rarity,
            'has_is_immutable': has_immutable,
            'is_immutable': old_immutable,
            'has_default_uses_left': has_default,
            'default_uses_left': default_uses,
        }, None

    @staticmethod
    def _catalog_snapshot_matches(actual: dict, expected: object) -> bool:
        """兼容不可变字段上线前创建、但仍可还原的备份快照。"""
        if not isinstance(expected, dict):
            return False
        comparable_expected = dict(expected)
        if 'is_immutable' not in comparable_expected:
            comparable_expected['is_immutable'] = actual.get('is_immutable')
        return actual == comparable_expected

    @staticmethod
    def _target_fields(target) -> tuple:
        (
            model,
            name_field,
            type_field,
            rarity_field,
            desc_field,
            uses_field,
            status_filter,
            count_key,
        ) = target
        return (
            model,
            name_field,
            type_field,
            rarity_field,
            desc_field,
            uses_field,
            status_filter,
            count_key,
        )

    @classmethod
    def _row_state(cls, row, target) -> dict:
        (
            _model,
            name_field,
            type_field,
            rarity_field,
            desc_field,
            uses_field,
            _status_filter,
            _count_key,
        ) = cls._target_fields(target)
        return {
            'name': getattr(row, name_field),
            'type': getattr(row, type_field),
            'rarity': getattr(row, rarity_field),
            'description': getattr(row, desc_field),
            'uses_left': getattr(row, uses_field),
        }

    @classmethod
    def _repair_state(cls, row, target, current: dict, old: dict) -> tuple[dict, bool, bool]:
        model = target[0]
        before = cls._row_state(row, target)
        desired_description = (
            current['description']
            if model in S1ItemCatalogService._PERSISTED_DESCRIPTION_TARGETS
            else None
        )
        after = {
            'name': current['name'],
            'type': current['type'],
            'rarity': current['rarity'],
            'description': desired_description,
            'uses_left': before['uses_left'],
        }
        content_changed = any(
            before[key] != after[key]
            for key in ('name', 'type', 'rarity', 'description')
        )
        default_uses_changed = bool(
            old['has_default_uses_left']
            and before['uses_left'] == old['default_uses_left']
            and before['uses_left'] != current['default_uses_left']
        )
        if default_uses_changed:
            after['uses_left'] = current['default_uses_left']
        return after, content_changed, default_uses_changed

    @classmethod
    def _candidate_groups(
        cls,
        old: dict,
        current: dict,
        *,
        lock: bool,
    ) -> tuple[dict[str, list[dict]], dict]:
        groups: dict[str, list[dict]] = {}
        candidate_limit = cls._candidate_limit()
        locked_rows = (
            S1ItemCatalogService._lock_current_asset_rows(
                old=old,
                max_rows=candidate_limit,
            )
            if lock
            else None
        )
        scan = {
            'exact_old_rows': 0,
            'already_current_rows': 0,
        }
        for target in S1ItemCatalogService._CURRENT_TARGETS:
            model = target[0]
            count_key = target[-1]
            if locked_rows is not None:
                rows = locked_rows.get(count_key, [])
            else:
                remaining = candidate_limit - scan['exact_old_rows']
                rows = (
                    S1ItemCatalogService._matching_query(target, old)
                    .order_by(model.id.asc())
                    .limit(remaining + 1)
                    .all()
                )
                if len(rows) > remaining:
                    raise RuntimeError(
                        f'待修复资产超过单次安全上限 {candidate_limit} 条，'
                        '请使用分批修复工具'
                    )
            scan['exact_old_rows'] += len(rows)
            entries = []
            for row in rows:
                after, content_changed, default_uses_changed = cls._repair_state(
                    row,
                    target,
                    current,
                    old,
                )
                if not content_changed and not default_uses_changed:
                    scan['already_current_rows'] += 1
                    continue
                before = cls._row_state(row, target)
                immutable_changed = bool(
                    old.get('has_is_immutable')
                    and old.get('is_immutable') != current['is_immutable']
                )
                before['is_immutable'] = (
                    old.get('is_immutable')
                    if old.get('has_is_immutable')
                    else None
                )
                after['is_immutable'] = current['is_immutable']
                entries.append({
                    'target': count_key,
                    'target_id': int(row.id),
                    'before': before,
                    'after': after,
                    'content_changed': content_changed,
                    'default_uses_changed': default_uses_changed,
                    'immutable_changed': immutable_changed,
                })
            groups[count_key] = entries
        return groups, scan

    @classmethod
    def _counts(cls, groups: dict[str, list[dict]], flag: Optional[str] = None) -> dict:
        counts = S1ItemCatalogService._empty_counts()
        for key, entries in groups.items():
            counts[key] = sum(
                1 for entry in entries
                if flag is None or bool(entry.get(flag))
            )
        return cls._finalize_counts(counts)

    @staticmethod
    def _candidate_digest(groups: dict[str, list[dict]]) -> str:
        digest = hashlib.sha256()
        for target, entries in groups.items():
            for entry in entries:
                digest.update(json.dumps(
                    {
                        'target': target,
                        'target_id': entry['target_id'],
                        'before': entry['before'],
                        'after': entry['after'],
                        'content_changed': entry['content_changed'],
                        'default_uses_changed': entry['default_uses_changed'],
                        'immutable_changed': entry.get('immutable_changed', False),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                ).encode('utf-8'))
        return digest.hexdigest()

    @classmethod
    def _samples(cls, groups: dict[str, list[dict]]) -> list[dict]:
        samples = []
        for entries in groups.values():
            for entry in entries:
                samples.append(entry)
                if len(samples) >= cls.SAMPLE_LIMIT:
                    return samples
        return samples

    @staticmethod
    def _load_backup_runs(
        setting: Optional[AdventurerS1Settings],
        *,
        expected_item_id: int,
    ) -> tuple[list[dict], Optional[str]]:
        if setting is None or not setting.value:
            return [], None
        try:
            payload = json.loads(setting.value)
        except (TypeError, json.JSONDecodeError):
            return [], (
                '该图鉴的历史同步备份已损坏，请先人工核验；'
                '系统未执行任何写入'
            )
        if (
            not isinstance(payload, dict)
            or payload.get('version') != 1
        ):
            return [], (
                '该图鉴的历史同步备份格式无效，请先人工核验；'
                '系统未执行任何写入'
            )
        runs = payload.get('runs')
        if (
            not isinstance(runs, list)
            or any(not isinstance(run, dict) for run in runs)
        ):
            return [], (
                '该图鉴的历史同步备份批次无效，请先人工核验；'
                '系统未执行任何写入'
            )
        valid_statuses = {
            'active',
            'partially_restored',
            'restored',
            'restored_with_skips',
        }
        valid_targets = {
            target[-1]
            for target in S1ItemCatalogService._CURRENT_TARGETS
        }
        for run in runs:
            entries = run.get('entries')
            pending_entries = run.get('pending_entries')
            status = str(run.get('status') or 'active')
            try:
                run_item_id = int(run.get('catalog_item_id') or 0)
            except (TypeError, ValueError):
                run_item_id = 0
            if (
                run.get('version') != 1
                or run_item_id != int(expected_item_id)
                or not str(run.get('run_id') or '').strip()
                or status not in valid_statuses
                or not isinstance(entries, list)
                or any(not isinstance(entry, dict) for entry in entries)
                or not isinstance(run.get('old_snapshot'), dict)
                or not isinstance(run.get('current_snapshot'), dict)
                or not isinstance(run.get('result'), dict)
                or (
                    pending_entries is not None
                    and (
                        not isinstance(pending_entries, list)
                        or any(
                            not isinstance(entry, dict)
                            for entry in pending_entries
                        )
                    )
                )
            ):
                return [], (
                    '该图鉴的历史同步备份内容无效，请先人工核验；'
                    '系统未执行任何写入'
                )
            checked_entries = list(entries)
            if isinstance(pending_entries, list):
                checked_entries.extend(pending_entries)
            for entry in checked_entries:
                try:
                    target_id = int(entry.get('target_id') or 0)
                except (TypeError, ValueError):
                    target_id = 0
                if (
                    entry.get('target') not in valid_targets
                    or target_id <= 0
                    or not isinstance(entry.get('before'), dict)
                    or not isinstance(entry.get('after'), dict)
                ):
                    return [], (
                        '该图鉴的历史同步备份目标无效，请先人工核验；'
                        '系统未执行任何写入'
                    )
        return runs, None

    @staticmethod
    def _pending_entries(run: dict) -> list[dict]:
        entries = (
            run.get('pending_entries')
            if 'pending_entries' in run
            else run.get('entries')
        )
        return entries if isinstance(entries, list) else []

    @classmethod
    def _is_restorable_run(cls, run: dict) -> bool:
        return (
            str(run.get('status') or 'active')
            in {'active', 'partially_restored'}
            and bool(cls._pending_entries(run))
        )

    @classmethod
    def _prune_backup_runs(cls, runs: list[dict]) -> list[dict]:
        """Keep every live restore run plus recent terminal replay tombstones."""
        terminal_indexes = [
            index
            for index, run in enumerate(runs)
            if str(run.get('status') or 'active') in {
                'restored',
                'restored_with_skips',
            }
        ]
        ttl_seconds = (
            S1AdminScanSnapshotService._ttl_seconds()
            if has_app_context()
            else 600
        )
        replay_cutoff = _portable_utc_now() - timedelta(seconds=ttl_seconds)
        recent_terminal_indexes = set()
        for index in terminal_indexes:
            created_text = cls._text(runs[index].get('created_at')).removesuffix('Z')
            try:
                created_at = datetime.fromisoformat(created_text) if created_text else None
            except ValueError:
                created_at = None
            if created_at is None or created_at >= replay_cutoff:
                recent_terminal_indexes.add(index)
        keep_indexes = set(terminal_indexes[-cls.BACKUP_RUN_LIMIT:])
        keep_indexes.update(recent_terminal_indexes)
        keep_indexes.update(
            index
            for index, run in enumerate(runs)
            if cls._is_restorable_run(run)
        )
        return [run for index, run in enumerate(runs) if index in keep_indexes]

    @classmethod
    def _latest_restorable_run(
        cls,
        runs: list[dict],
    ) -> tuple[Optional[int], Optional[dict]]:
        for index in range(len(runs) - 1, -1, -1):
            run = runs[index]
            if cls._is_restorable_run(run):
                return index, run
        return None, None

    @classmethod
    def _backup_summary(cls, run: Optional[dict]) -> Optional[dict]:
        if not isinstance(run, dict):
            return None
        result = run.get('result')
        return {
            'run_id': cls._text(run.get('run_id')),
            'status': cls._text(run.get('status') or 'active'),
            'created_at': cls._text(run.get('created_at')),
            'note': cls._text(run.get('note')),
            'remaining_entries': len(cls._pending_entries(run)),
            'old_snapshot': (
                run.get('old_snapshot')
                if isinstance(run.get('old_snapshot'), dict)
                else None
            ),
            'current_snapshot': (
                run.get('current_snapshot')
                if isinstance(run.get('current_snapshot'), dict)
                else None
            ),
            'repaired': (
                result.get('repaired')
                if isinstance(result, dict)
                and isinstance(result.get('repaired'), dict)
                else cls._finalize_counts(S1ItemCatalogService._empty_counts())
            ),
        }

    @classmethod
    def preview(
        cls,
        item_id: int,
        body: dict,
        admin_user_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        old, err = cls._parse_old_snapshot(body)
        if err:
            return None, err
        try:
            item = db_manager.query(AdventurerS1Item).filter_by(id=int(item_id)).first()
            if not item:
                return None, '物品不存在'
            if is_hidden_boss_clue_item(item.use_condition):
                return None, '追查线索的历史同步请走线索追查处专用流程'
            name_conflict = (
                db_manager.query(AdventurerS1Item.id)
                .filter(
                    AdventurerS1Item.name == old['name'],
                    AdventurerS1Item.id != item.id,
                )
                .first()
            )
            if name_conflict:
                return None, (
                    f'修改前名称「{old["name"]}」已被另一条当前图鉴占用，'
                    '无法自动判断旧实例归属'
                )

            current = cls._catalog_snapshot(item)
            groups, scan = cls._candidate_groups(old, current, lock=False)
            counts = cls._counts(groups)
            content_counts = cls._counts(groups, 'content_changed')
            uses_counts = cls._counts(groups, 'default_uses_changed')
            market_conflicts = (
                counts['market_total']
                if old['rarity'] != current['rarity']
                else 0
            )
            blocked_reason = (
                '修改前后稀有度不同，且仍有进行中的市场合同；'
                '请先结束或取消这些交易后重新预览'
                if market_conflicts
                else None
            )
            preview_id = uuid.uuid4().hex
            snapshot = {
                'version': 1,
                'preview_id': preview_id,
                'catalog_item_id': current['id'],
                'old_snapshot': old,
                'current_snapshot': current,
                'candidate_digest': cls._candidate_digest(groups),
                'counts': counts,
            }
            token = S1AdminScanSnapshotService.store(
                cls.SNAPSHOT_OPERATION,
                admin_user_id,
                snapshot,
                allow_signed_fallback=True,
            )
            if not token:
                return None, '无法保存预览快照，请稍后重试'
            backup_runs, backup_err = cls._load_backup_runs(
                db_manager.query(AdventurerS1Settings).filter_by(
                    key=cls._backup_key(current['id']),
                ).first(),
                expected_item_id=current['id'],
            )
            if backup_err:
                return None, backup_err
            _backup_index, latest_backup = cls._latest_restorable_run(
                backup_runs,
            )
            return {
                'preview_token': token,
                'preview_id': preview_id,
                'catalog_item': current,
                'old_snapshot': old,
                'counts': counts,
                'content_counts': content_counts,
                'default_uses_counts': uses_counts,
                'scan': scan,
                'samples': cls._samples(groups),
                'market_rarity_conflicts': market_conflicts,
                'blocked_reason': blocked_reason,
                'has_backup': latest_backup is not None,
                'latest_backup_run_id': (
                    latest_backup.get('run_id')
                    if latest_backup
                    else None
                ),
                'latest_backup_summary': cls._backup_summary(latest_backup),
                'historical_market_rows_updated': 0,
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception(
                'S1 图鉴历史同步预览失败 item_id=%s',
                item_id,
            )
            return None, f'预览失败：{exc}'

    @classmethod
    def _find_target(cls, count_key: str):
        return next(
            (
                target
                for target in S1ItemCatalogService._CURRENT_TARGETS
                if target[-1] == count_key
            ),
            None,
        )

    @classmethod
    def _bulk_update_entries(
        cls,
        target,
        entries: list[dict],
        *,
        now: datetime,
    ) -> None:
        (
            model,
            name_field,
            type_field,
            rarity_field,
            desc_field,
            uses_field,
            status_filter,
            _count_key,
        ) = cls._target_fields(target)
        content_ids = [
            entry['target_id']
            for entry in entries
            if entry['content_changed']
        ]
        uses_ids = [
            entry['target_id']
            for entry in entries
            if entry['default_uses_changed']
        ]
        if content_ids:
            values = {
                getattr(model, name_field): entries[0]['after']['name'],
                getattr(model, type_field): entries[0]['after']['type'],
                getattr(model, rarity_field): entries[0]['after']['rarity'],
                getattr(model, desc_field): entries[0]['after']['description'],
                model.updated_at: now,
            }
            query = db_manager.query(model).filter(model.id.in_(content_ids))
            if status_filter:
                status_field, status_value = status_filter
                query = query.filter(getattr(model, status_field) == status_value)
            affected = int(query.update(values, synchronize_session=False))
            if affected != len(content_ids):
                raise RuntimeError(f'{target[-1]} 内容同步数量已变化，请重新预览')
        if uses_ids:
            values = {
                getattr(model, uses_field): next(
                    entry['after']['uses_left']
                    for entry in entries
                    if entry['default_uses_changed']
                ),
                model.updated_at: now,
            }
            query = db_manager.query(model).filter(model.id.in_(uses_ids))
            if status_filter:
                status_field, status_value = status_filter
                query = query.filter(getattr(model, status_field) == status_value)
            affected = int(query.update(values, synchronize_session=False))
            if affected != len(uses_ids):
                raise RuntimeError(f'{target[-1]} 默认次数同步数量已变化，请重新预览')

    @classmethod
    def execute(
        cls,
        item_id: int,
        admin_user_id: int,
        preview_token: str,
        note: str = '',
    ) -> Tuple[Optional[dict], Optional[str]]:
        preview = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_OPERATION,
            admin_user_id,
            preview_token,
            allow_signed_fallback=True,
        )
        if preview is None:
            return None, '预览快照已过期，请重新预览后再执行'

        item_id = int(item_id)
        preview_item_id = int(preview.get('catalog_item_id') or 0)
        old = preview.get('old_snapshot')
        expected_current = preview.get('current_snapshot')
        if (
            not item_id
            or preview_item_id != item_id
            or not isinstance(old, dict)
            or not isinstance(expected_current, dict)
        ):
            return None, '预览快照与当前图鉴不匹配，请重新预览'

        result = None
        try:
            with db_manager.transaction():
                from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
                    lock_catalog_names,
                )

                lock_catalog_names(
                    [old.get('name'), expected_current.get('name')],
                    shared=False,
                    wait=True,
                )
                item = (
                    db_manager.query(AdventurerS1Item)
                    .filter(AdventurerS1Item.id == item_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not item:
                    return None, '物品不存在'
                backup_key = cls._backup_key(item_id)
                setting = (
                    db_manager.query(AdventurerS1Settings)
                    .filter_by(key=backup_key)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                runs, backup_err = cls._load_backup_runs(
                    setting,
                    expected_item_id=item_id,
                )
                if backup_err:
                    return None, backup_err
                previous_run = next(
                    (
                        run
                        for run in runs
                        if run.get('source_preview_id') == preview.get('preview_id')
                    ),
                    None,
                )
                if previous_run and isinstance(previous_run.get('result'), dict):
                    if str(previous_run.get('status') or 'active') != 'active':
                        return None, (
                            '该预览批次已经执行并发生过还原，'
                            '不能再次执行；请重新预览'
                        )
                    result = dict(previous_run['result'])
                    result['already_applied'] = True
                    if result.get('repaired', {}).get('total'):
                        S1ItemCatalogService.refresh_after_catalog_change(item_id)
                    return result, None

                if is_hidden_boss_clue_item(item.use_condition):
                    return None, '追查线索的历史同步请走线索追查处专用流程'
                current = cls._catalog_snapshot(item)
                if not cls._catalog_snapshot_matches(current, expected_current):
                    return None, '图鉴在预览后已发生变化，请重新预览'
                name_conflict = (
                    db_manager.query(AdventurerS1Item.id)
                    .filter(
                        AdventurerS1Item.name == old.get('name'),
                        AdventurerS1Item.id != item.id,
                    )
                    .first()
                )
                if name_conflict:
                    return None, (
                        f'修改前名称「{old.get("name")}」已被另一条当前图鉴占用，'
                        '无法自动判断旧实例归属'
                    )

                groups, _scan = cls._candidate_groups(old, current, lock=True)
                digest = cls._candidate_digest(groups)
                if digest != preview.get('candidate_digest'):
                    return None, '待修复实例在预览后已发生变化，请重新预览'

                counts = cls._counts(groups)
                if old.get('rarity') != current['rarity'] and counts['market_total']:
                    return None, (
                        '修改前后稀有度不同，仍有进行中的市场合同；'
                        '请先结束或取消这些交易后重新预览'
                    )
                if counts['total'] == 0:
                    return {
                        'run_id': None,
                        'catalog_item_id': item_id,
                        'catalog_name': current['name'],
                        'repaired': counts,
                        'content_updated': cls._counts(groups, 'content_changed'),
                        'default_uses_updated': cls._counts(
                            groups,
                            'default_uses_changed',
                        ),
                        'already_applied': False,
                    }, None

                run_id = uuid.uuid4().hex
                backup_entries = [
                    entry
                    for entries in groups.values()
                    for entry in entries
                ]
                now = _portable_utc_now()
                for target in S1ItemCatalogService._CURRENT_TARGETS:
                    cls._bulk_update_entries(
                        target,
                        groups.get(target[-1]) or [],
                        now=now,
                    )

                result = {
                    'run_id': run_id,
                    'catalog_item_id': item_id,
                    'catalog_name': current['name'],
                    'repaired': counts,
                    'content_updated': cls._counts(groups, 'content_changed'),
                    'default_uses_updated': cls._counts(
                        groups,
                        'default_uses_changed',
                    ),
                    'already_applied': False,
                }
                runs.append({
                    'version': 1,
                    'run_id': run_id,
                    'source_preview_id': preview.get('preview_id'),
                    'status': 'active',
                    'created_at': now.isoformat() + 'Z',
                    'note': cls._text(note)[:200],
                    'catalog_item_id': item_id,
                    'old_snapshot': old,
                    'current_snapshot': current,
                    'entries': backup_entries,
                    'result': result,
                })
                runs = cls._prune_backup_runs(runs)
                payload = json.dumps({'version': 1, 'runs': runs}, ensure_ascii=False)
                if setting is None:
                    added, add_err = db_manager.add(AdventurerS1Settings(
                        key=backup_key,
                        value=payload,
                        description='物品图鉴历史同步修复备份（按图鉴保留最近批次）',
                        updated_by_user_id=admin_user_id,
                    ))
                    if not added:
                        raise RuntimeError(f'历史同步备份保存失败：{add_err}')
                else:
                    setting.value = payload
                    setting.updated_by_user_id = admin_user_id
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception(
                'S1 图鉴历史同步执行失败 item_id=%s',
                item_id,
            )
            return None, f'修复失败：{exc}'

        if result and result['repaired']['total']:
            S1ItemCatalogService.refresh_after_catalog_change(item_id)
        return result, None

    @staticmethod
    def _state_matches(actual: dict, expected: dict) -> bool:
        return all(
            actual.get(key) == expected.get(key)
            for key in ('name', 'type', 'rarity', 'description', 'uses_left')
        )

    @classmethod
    def _restore_group(
        cls,
        target,
        entries: list[dict],
        *,
        now: datetime,
        locked_rows: list,
    ) -> tuple[int, int, int, list[dict]]:
        if not entries:
            return 0, 0, 0, []
        (
            model,
            name_field,
            type_field,
            rarity_field,
            desc_field,
            uses_field,
            status_filter,
            _count_key,
        ) = cls._target_fields(target)
        row_map = {int(row.id): row for row in locked_rows}
        grouped: dict[tuple, list[int]] = defaultdict(list)
        remaining = []
        frozen_history = 0
        missing = 0
        for entry in entries:
            row = row_map.get(int(entry['target_id']))
            if row is None:
                missing += 1
                continue
            if status_filter:
                status_field, status_value = status_filter
                if getattr(row, status_field) != status_value:
                    frozen_history += 1
                    continue
            if not cls._state_matches(cls._row_state(row, target), entry['after']):
                remaining.append(entry)
                continue
            before = entry['before']
            grouped[(
                before.get('name'),
                before.get('type'),
                before.get('rarity'),
                before.get('description'),
                before.get('uses_left'),
            )].append(int(row.id))

        restored = 0
        for state, state_ids in grouped.items():
            values = {
                getattr(model, name_field): state[0],
                getattr(model, type_field): state[1],
                getattr(model, rarity_field): state[2],
                getattr(model, desc_field): state[3],
                getattr(model, uses_field): state[4],
                model.updated_at: now,
            }
            restored += int(
                db_manager.query(model)
                .filter(model.id.in_(state_ids))
                .update(values, synchronize_session=False)
            )
        return restored, frozen_history, missing, remaining

    @classmethod
    def restore(
        cls,
        item_id: int,
        admin_user_id: int,
        run_id: str,
    ) -> Tuple[Optional[dict], Optional[str]]:
        item_id = int(item_id)
        run_id = cls._text(run_id)
        if not run_id:
            return None, '请选择要还原的历史同步批次'
        preliminary_names = set()
        try:
            preliminary_item = (
                db_manager.query(AdventurerS1Item.name)
                .filter(AdventurerS1Item.id == item_id)
                .first()
            )
            if preliminary_item and preliminary_item.name:
                preliminary_names.add(cls._text(preliminary_item.name))
            preliminary_setting = (
                db_manager.query(AdventurerS1Settings)
                .filter_by(key=cls._backup_key(item_id))
                .first()
            )
            preliminary_runs, preliminary_err = cls._load_backup_runs(
                preliminary_setting,
                expected_item_id=item_id,
            )
            if preliminary_err:
                return None, preliminary_err
            preliminary_run = next(
                (
                    candidate
                    for candidate in preliminary_runs
                    if candidate.get('run_id') == run_id
                ),
                None,
            )
            if isinstance(preliminary_run, dict):
                for snapshot_key in ('old_snapshot', 'current_snapshot'):
                    snapshot = preliminary_run.get(snapshot_key)
                    if isinstance(snapshot, dict) and cls._text(snapshot.get('name')):
                        preliminary_names.add(cls._text(snapshot.get('name')))
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception(
                'S1 图鉴历史同步还原预锁读取失败 item_id=%s',
                item_id,
            )
            return None, f'还原失败：{exc}'
        result = None
        try:
            with db_manager.transaction():
                from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
                    lock_catalog_names,
                )

                lock_catalog_names(
                    preliminary_names,
                    shared=False,
                    wait=True,
                )
                item = (
                    db_manager.query(AdventurerS1Item)
                    .filter(AdventurerS1Item.id == item_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not item:
                    return None, '物品不存在'
                setting = (
                    db_manager.query(AdventurerS1Settings)
                    .filter_by(key=cls._backup_key(item_id))
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                runs, backup_err = cls._load_backup_runs(
                    setting,
                    expected_item_id=item_id,
                )
                if backup_err:
                    return None, backup_err
                requested_run = next(
                    (
                        candidate
                        for candidate in runs
                        if candidate.get('run_id') == run_id
                    ),
                    None,
                )
                if (
                    isinstance(requested_run, dict)
                    and requested_run.get('status') in {'restored', 'restored_with_skips'}
                    and isinstance(requested_run.get('restore_result'), dict)
                ):
                    repeated_result = dict(requested_run['restore_result'])
                    repeated_result['has_backup'] = any(
                        cls._is_restorable_run(candidate)
                        for candidate in runs
                    )
                    repeated_result['already_restored'] = True
                    if repeated_result.get('restored', {}).get('total'):
                        S1ItemCatalogService.refresh_after_catalog_change(item_id)
                    return repeated_result, None
                if is_hidden_boss_clue_item(item.use_condition):
                    return None, '追查线索的历史同步请走线索追查处专用流程'
                run_index, run = cls._latest_restorable_run(runs)
                if run is None or run_index is None:
                    return None, '该图鉴没有可还原的历史同步备份'
                if run.get('run_id') != run_id:
                    return None, '可还原的历史同步批次已变化，请重新预览'
                if not cls._catalog_snapshot_matches(
                    cls._catalog_snapshot(item),
                    run.get('current_snapshot'),
                ):
                    return None, '图鉴在修复后又被修改，不能自动还原旧实例'
                old_snapshot = run.get('old_snapshot')
                old_name = (
                    cls._text(old_snapshot.get('name'))
                    if isinstance(old_snapshot, dict)
                    else ''
                )
                if not old_name:
                    return None, '历史同步备份缺少修改前名称，不能自动还原'
                required_names = {
                    cls._text(item.name),
                    old_name,
                }
                if not required_names.issubset(preliminary_names):
                    return None, '可还原批次在加锁前已变化，请重新预览'
                name_conflict = (
                    db_manager.query(AdventurerS1Item.id)
                    .filter(
                        AdventurerS1Item.name == old_name,
                        AdventurerS1Item.id != item.id,
                    )
                    .first()
                )
                if name_conflict:
                    return None, (
                        f'修改前名称「{old_name}」已被另一条当前图鉴占用，'
                        '不能自动还原旧实例'
                    )

                entries_by_target: dict[str, list[dict]] = defaultdict(list)
                for entry in cls._pending_entries(run):
                    if isinstance(entry, dict):
                        entries_by_target[str(entry.get('target') or '')].append(entry)

                target_ids = {
                    count_key: [
                        int(entry['target_id'])
                        for entry in entries
                    ]
                    for count_key, entries in entries_by_target.items()
                }
                locked_rows = S1ItemCatalogService._lock_current_asset_rows(
                    target_ids=target_ids,
                    max_rows=cls._candidate_limit(),
                )

                counts = S1ItemCatalogService._empty_counts()
                frozen_history = 0
                missing = 0
                remaining = []
                now = _portable_utc_now()
                for target in S1ItemCatalogService._CURRENT_TARGETS:
                    count_key = target[-1]
                    restored, frozen, absent, target_remaining = cls._restore_group(
                        target,
                        entries_by_target.get(count_key) or [],
                        now=now,
                        locked_rows=locked_rows.get(count_key, []),
                    )
                    counts[count_key] = restored
                    frozen_history += frozen
                    missing += absent
                    remaining.extend(target_remaining)
                cls._finalize_counts(counts)

                previous_restore = (
                    run.get('restore_result')
                    if isinstance(run.get('restore_result'), dict)
                    else {}
                )
                previous_counts = (
                    previous_restore.get('restored')
                    if isinstance(previous_restore.get('restored'), dict)
                    else {}
                )
                cumulative_counts = S1ItemCatalogService._empty_counts()
                for count_key in (
                    'equipment',
                    'warehouse',
                    'market_listings',
                    'market_offers',
                    'market_buy_offers',
                    'secret_auction_lots',
                ):
                    cumulative_counts[count_key] = int(previous_counts.get(count_key) or 0) + int(
                        counts.get(count_key) or 0
                    )
                cls._finalize_counts(cumulative_counts)
                cumulative_frozen = int(
                    previous_restore.get('skipped_frozen_history') or 0
                ) + frozen_history
                cumulative_missing = int(
                    previous_restore.get('skipped_missing') or 0
                ) + missing

                if remaining:
                    run['pending_entries'] = remaining
                    run['status'] = 'partially_restored'
                    run['restore_attempted_at'] = now.isoformat() + 'Z'
                    runs[run_index] = run
                else:
                    run['pending_entries'] = []
                    run['status'] = (
                        'restored_with_skips'
                        if cumulative_frozen or cumulative_missing
                        else 'restored'
                    )
                    run['restored_at'] = now.isoformat() + 'Z'
                    runs[run_index] = run
                has_backup = any(
                    cls._is_restorable_run(candidate)
                    for candidate in runs
                )
                result = {
                    'run_id': run.get('run_id'),
                    'catalog_item_id': item_id,
                    'restored': cumulative_counts,
                    'skipped_changed': len(remaining),
                    'skipped_frozen_history': cumulative_frozen,
                    'skipped_missing': cumulative_missing,
                    'has_backup': has_backup,
                }
                run['restore_result'] = result
                runs[run_index] = run
                setting.value = json.dumps(
                    {'version': 1, 'runs': runs},
                    ensure_ascii=False,
                )
                setting.updated_by_user_id = admin_user_id
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception(
                'S1 图鉴历史同步还原失败 item_id=%s',
                item_id,
            )
            return None, f'还原失败：{exc}'

        if result and result['restored']['total']:
            S1ItemCatalogService.refresh_after_catalog_change(item_id)
        return result, None
