"""幸运大转盘：消耗抽奖券抽取固定奖池物品。"""
from __future__ import annotations

from plugins.adventurer_s1.services.s1_player_error_service import player_database_result
import json
import random
from types import SimpleNamespace
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from db_repository import db_manager
from db_repository.decorators import with_transaction
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1Item,
    AdventurerS1LuckyWheelSpin,
    AdventurerS1Settings,
)
from plugins.adventurer_s1.services.s1_item_grant_service import (
    grant_catalog_item,
    prepare_warehouse_grant_batch,
    warehouse_capacity_error,
)
from plugins.adventurer_s1.services.s1_item_instance_service import (
    consume_item_stack,
    lock_warehouse_aggregate,
)
from plugins.adventurer_s1.services.s1_item_use_condition_service import (
    apply_catalog_default_uses_left,
    catalog_default_uses_left,
    catalog_has_default_uses_left,
)
from utils.cache_manager import CacheManager

_HANDLER_KEY = 'lucky_wheel'
_VALID_RARITIES = ('common', 'rare', 'epic', 'legendary')
_VALID_PAYMENT = frozenset({'ticket'})
_LEGENDARY_INDEX_KEY = 'lucky_wheel_legendary_index'
_CUSTOM_POOL_KEY = 'lucky_wheel_custom_pool'
_CUSTOM_ROTATION_KEY = 'lucky_wheel_custom_rotation'
_BASE_EXCLUDED_KEY = 'lucky_wheel_base_excluded'
_WHEEL_SETTING_KEYS = (
    _LEGENDARY_INDEX_KEY,
    _CUSTOM_POOL_KEY,
    _CUSTOM_ROTATION_KEY,
    _BASE_EXCLUDED_KEY,
)
_LUCKY_WHEEL_SHARED_CACHE_TTL_SECONDS = 60
_lucky_wheel_shared_cache = CacheManager(
    ttl_seconds=_LUCKY_WHEEL_SHARED_CACHE_TTL_SECONDS, max_size=8,
)


def invalidate_lucky_wheel_pool_cache() -> None:
    """幸运大转盘奖池 shared 缓存失效（管理员改池 / 传说推进后调用）。"""
    _lucky_wheel_shared_cache.clear()


class S1LuckyWheelService:
    """幸运大转盘业务逻辑。"""

    @staticmethod
    def _load_wheel_settings() -> dict[str, str | None]:
        """一次查询加载全部转盘 settings，避免同请求内重复单 key 查询。"""
        rows = AdventurerS1Settings.query.filter(
            AdventurerS1Settings.key.in_(_WHEEL_SETTING_KEYS),
        ).all()
        return {row.key: row.value for row in rows}

    @staticmethod
    def _refresh_catalog_name_after_commit(name: str) -> None:
        invalidate_lucky_wheel_pool_cache()
        catalog = AdventurerS1Item.query.filter_by(name=str(name or '').strip()).first()
        if catalog is None:
            return
        from plugins.adventurer_s1.services.s1_item_catalog_service import (
            S1ItemCatalogService,
        )

        S1ItemCatalogService.refresh_after_catalog_change(int(catalog.id))

    @staticmethod
    def _settings_value(settings: dict[str, str | None] | None, key: str) -> str | None:
        if settings is None:
            rec = AdventurerS1Settings.query.filter_by(key=key).first()
            return rec.value if rec else None
        return settings.get(key)

    @staticmethod
    def _ticket_item_name() -> str:
        return str(
            current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_TICKET_ITEM', '大转盘抽奖券')
        ).strip() or '大转盘抽奖券'

    @staticmethod
    def _admin_pool_item_payload(
        name: str,
        row: AdventurerS1Item | None,
        rarity: str,
        **extra,
    ) -> dict:
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            is_catalog_text_locked,
            is_hidden_boss_clue_item,
        )

        payload = {
            'name': name,
            'type': row.type if row else '',
            'description': (row.description or '') if row else '',
            'rarity': rarity,
            'world_unique': bool(row.world_unique) if row else False,
            'text_locked': is_catalog_text_locked(row.use_condition) if row else False,
            'system_locked': is_hidden_boss_clue_item(row.use_condition) if row else False,
        }
        if row and catalog_has_default_uses_left(row.use_condition):
            payload['default_uses_left'] = catalog_default_uses_left(row.use_condition)
        payload.update(extra)
        return payload

    @staticmethod
    def _set_catalog_text_locked(catalog: AdventurerS1Item, text_locked: bool) -> Optional[str]:
        """设置图鉴不可变标记（use_condition.text_locked）。"""
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            is_hidden_boss_clue_item,
            normalize_use_condition,
        )

        if is_hidden_boss_clue_item(catalog.use_condition):
            if text_locked:
                return None
            return '追查线索等系统锁定物品不可取消不可变'

        cond = dict(normalize_use_condition(catalog.use_condition) or {})
        if text_locked:
            cond['text_locked'] = True
            catalog.use_condition = normalize_use_condition(cond)
            return None

        if 'text_locked' not in cond:
            return None
        cond.pop('text_locked', None)
        catalog.use_condition = normalize_use_condition(cond) if cond else None
        return None

    @staticmethod
    def _updated_catalog_use_condition(
        catalog: AdventurerS1Item,
        *,
        text_locked: Optional[bool],
        uses_left: int | None,
        uses_left_provided: bool,
    ) -> Tuple[Optional[dict], Optional[str]]:
        holder = SimpleNamespace(use_condition=catalog.use_condition)
        if text_locked is not None:
            lock_err = S1LuckyWheelService._set_catalog_text_locked(
                holder,
                bool(text_locked),
            )
            if lock_err:
                return None, lock_err
        uses_err = apply_catalog_default_uses_left(
            holder,
            uses_left,
            provided=uses_left_provided,
        )
        if uses_err:
            return None, uses_err
        return holder.use_condition, None

    @staticmethod
    def _add_name_to_pool(
        name: str,
        rarity: str,
        legendary_position: str = 'next',
    ) -> Tuple[Optional[dict], Optional[str]]:
        """将已有图鉴物品名加入奖池（传说进轮换，其余进自定义池）。"""
        if rarity == 'legendary':
            rotation = S1LuckyWheelService._get_custom_rotation()
            if not rotation:
                raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_LEGENDARY_ROTATION')
                if isinstance(raw, list) and raw:
                    rotation = [str(n).strip() for n in raw if str(n).strip()]
                else:
                    from plugins.adventurer_s1.seed import LUCKY_WHEEL_LEGENDARY_ROTATION
                    rotation = list(LUCKY_WHEEL_LEGENDARY_ROTATION)

            full_rotation = S1LuckyWheelService._legendary_rotation()
            if name in full_rotation:
                return None, f'「{name}」已在传说轮换队列中'

            pos = (legendary_position or 'next').strip().lower()
            current_index = S1LuckyWheelService._get_legendary_index()
            if pos == 'current':
                insert_at = min(current_index + 1, len(rotation))
                rotation.insert(insert_at, name)
            else:
                rotation.append(name)

            S1LuckyWheelService._save_custom_rotation(rotation)
            return {'message': f'已添加「{name}」到传说轮换队列（{pos}）', 'name': name}, None

        raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_POOL') or {}
        base_names = [str(n).strip() for n in (raw.get(rarity) or []) if str(n).strip()]
        if name in base_names and name not in S1LuckyWheelService._get_base_excluded():
            return None, f'「{name}」已在 {rarity} 基础奖池中'

        custom = S1LuckyWheelService._get_custom_pool()
        rarity_list = custom.get(rarity) or []
        if name in rarity_list:
            return None, f'「{name}」已在 {rarity} 奖池中'

        rarity_list.append(name)
        custom[rarity] = rarity_list
        S1LuckyWheelService._save_custom_pool(custom)
        return {'message': f'已添加「{name}」到 {rarity} 奖池', 'name': name}, None

    @staticmethod
    def _get_custom_rotation(settings: dict[str, str | None] | None = None) -> list[str]:
        """读取管理员自定义传说轮换队列（持久化在 settings 表）。"""
        raw = S1LuckyWheelService._settings_value(settings, _CUSTOM_ROTATION_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(n).strip() for n in data if str(n).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
        return []

    @staticmethod
    def _save_custom_rotation(rotation: list[str]) -> None:
        """保存管理员自定义传说轮换队列到 settings 表。"""
        rec = AdventurerS1Settings.query.filter_by(key=_CUSTOM_ROTATION_KEY).first()
        if rec is None:
            db_manager.add(AdventurerS1Settings(
                key=_CUSTOM_ROTATION_KEY,
                value=json.dumps(rotation, ensure_ascii=False),
                description='幸运大转盘传说奖品轮换队列（管理员自定义）',
            ))
        else:
            rec.value = json.dumps(rotation, ensure_ascii=False)
        invalidate_lucky_wheel_pool_cache()

    @staticmethod
    def _legendary_rotation(settings: dict[str, str | None] | None = None) -> list[str]:
        """传说轮换队列：优先读管理员自定义（settings），回退到配置。"""
        custom = S1LuckyWheelService._get_custom_rotation(settings)
        if custom:
            return custom
        raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_LEGENDARY_ROTATION')
        if isinstance(raw, list) and raw:
            return [str(n).strip() for n in raw if str(n).strip()]
        from plugins.adventurer_s1.seed import LUCKY_WHEEL_LEGENDARY_ROTATION
        return list(LUCKY_WHEEL_LEGENDARY_ROTATION)

    @staticmethod
    def _legendary_fallback_name(settings: dict[str, str | None] | None = None) -> Optional[str]:
        """传说轮换抽完后的可重复保底奖品（非全服唯一）。"""
        raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_LEGENDARY_FALLBACK')
        if raw is None:
            from plugins.adventurer_s1.seed import LUCKY_WHEEL_LEGENDARY_FALLBACK
            raw = LUCKY_WHEEL_LEGENDARY_FALLBACK
        name = str(raw or '').strip()
        if not name:
            return None
        if name in S1LuckyWheelService._get_base_excluded(settings):
            return None
        return name

    @staticmethod
    def _is_legendary_fallback_name(
        name: str | None,
        settings: dict[str, str | None] | None = None,
    ) -> bool:
        fallback = S1LuckyWheelService._legendary_fallback_name(settings)
        return bool(name and fallback and name == fallback)

    @staticmethod
    def _legendary_meta(name: str) -> dict:
        cfg = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_LEGENDARY_META')
        if isinstance(cfg, dict) and name in cfg:
            return dict(cfg[name])
        from plugins.adventurer_s1.seed import LUCKY_WHEEL_LEGENDARY_META
        return dict(LUCKY_WHEEL_LEGENDARY_META.get(name) or {})

    @staticmethod
    def _get_legendary_index(settings: dict[str, str | None] | None = None) -> int:
        raw = S1LuckyWheelService._settings_value(settings, _LEGENDARY_INDEX_KEY)
        if raw is None:
            return 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _lock_legendary_index_row() -> None:
        """Serialize spins that can read or advance the legendary rotation."""
        rec = (
            AdventurerS1Settings.query
            .filter_by(key=_LEGENDARY_INDEX_KEY)
            .with_for_update()
            .first()
        )
        if rec is not None:
            return

        ok, err = db_manager.add(AdventurerS1Settings(
            key=_LEGENDARY_INDEX_KEY,
            value='0',
            description='Lucky wheel legendary rotation lock',
        ))
        if not ok:
            raise RuntimeError(err or 'lucky wheel rotation lock init failed')
        ok, err = db_manager.flush()
        if not ok:
            raise RuntimeError(err or 'lucky wheel rotation lock init failed')

    @staticmethod
    def _set_legendary_index(index: int) -> None:
        index = max(0, int(index))
        rec = AdventurerS1Settings.query.filter_by(key=_LEGENDARY_INDEX_KEY).first()
        if rec is None:
            db_manager.add(AdventurerS1Settings(
                key=_LEGENDARY_INDEX_KEY,
                value=str(index),
                description='幸运大转盘当前传说奖池轮换下标',
            ))
        else:
            rec.value = str(index)
        invalidate_lucky_wheel_pool_cache()

    @staticmethod
    def _legendary_name_at(index: int, settings: dict[str, str | None] | None = None) -> Optional[str]:
        rotation = S1LuckyWheelService._legendary_rotation(settings)
        excluded = S1LuckyWheelService._get_base_excluded(settings)
        filtered = [n for n in rotation if n not in excluded]
        if not filtered or index < 0 or index >= len(filtered):
            return None
        return filtered[index]

    @staticmethod
    def _current_rotation_legendary_name(
        settings: dict[str, str | None] | None = None,
    ) -> Optional[str]:
        """当前轮换队列中的传说（不含保底）。"""
        return S1LuckyWheelService._legendary_name_at(
            S1LuckyWheelService._get_legendary_index(settings),
            settings,
        )

    @staticmethod
    def _current_legendary_name(settings: dict[str, str | None] | None = None) -> Optional[str]:
        rotation_name = S1LuckyWheelService._current_rotation_legendary_name(settings)
        if rotation_name:
            return rotation_name
        return S1LuckyWheelService._legendary_fallback_name(settings)

    @staticmethod
    def _next_legendary_name(settings: dict[str, str | None] | None = None) -> Optional[str]:
        if S1LuckyWheelService._current_rotation_legendary_name(settings) is None:
            return None
        next_rotation = S1LuckyWheelService._legendary_name_at(
            S1LuckyWheelService._get_legendary_index(settings) + 1,
            settings,
        )
        if next_rotation:
            return next_rotation
        return S1LuckyWheelService._legendary_fallback_name(settings)

    @staticmethod
    def _advance_legendary_index() -> None:
        S1LuckyWheelService._set_legendary_index(
            S1LuckyWheelService._get_legendary_index() + 1,
        )

    @staticmethod
    def _serialize_pool_item(
        name: str,
        row: Optional[AdventurerS1Item],
        *,
        rarity: str,
        force_world_unique: bool = False,
    ) -> dict:
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            is_catalog_text_locked,
        )

        meta = S1LuckyWheelService._legendary_meta(name) if rarity == 'legendary' else {}
        world_unique = force_world_unique or bool(getattr(row, 'world_unique', False))
        payload = {
            'name': name,
            'type': row.type if row else '',
            'description': (row.description or '') if row else '',
            'rarity': row.rarity if row else rarity,
            'world_unique': world_unique,
            'text_locked': is_catalog_text_locked(row.use_condition) if row else False,
        }
        if meta.get('item_kind'):
            payload['item_kind'] = meta['item_kind']
        if meta.get('grants_skill'):
            payload['grants_skill'] = meta['grants_skill']
        if force_world_unique:
            payload['is_rotation_legendary'] = True
        return payload

    @staticmethod
    def _serialize_player_pool_item(
        name: str,
        row: Optional[AdventurerS1Item],
        *,
        rarity: str,
        force_world_unique: bool = False,
    ) -> dict:
        payload = S1LuckyWheelService._serialize_pool_item(
            name,
            row,
            rarity=rarity,
            force_world_unique=force_world_unique,
        )
        if row is None:
            return payload
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            build_clue_attach_meta,
            sanitize_player_catalog_item_dict,
        )

        sanitize_player_catalog_item_dict(
            payload,
            build_clue_attach_meta(row),
            name_key='name',
            desc_key='description',
            type_key='type',
        )
        return payload

    @staticmethod
    def _legendary_preview_payload(
        catalog: dict[str, AdventurerS1Item],
        settings: dict[str, str | None] | None = None,
    ) -> dict:
        current_name = S1LuckyWheelService._current_legendary_name(settings)
        next_name = S1LuckyWheelService._next_legendary_name(settings)
        on_fallback = S1LuckyWheelService._is_legendary_fallback_name(current_name, settings)
        current_item = None
        next_item = None
        if current_name:
            row = catalog.get(current_name)
            current_item = S1LuckyWheelService._serialize_player_pool_item(
                current_name,
                row,
                rarity='legendary',
                force_world_unique=not on_fallback,
            )
            if on_fallback:
                current_item['is_fallback'] = True
        if next_name:
            row = catalog.get(next_name)
            next_is_fallback = S1LuckyWheelService._is_legendary_fallback_name(next_name, settings)
            next_item = S1LuckyWheelService._serialize_player_pool_item(
                next_name,
                row,
                rarity='legendary',
                force_world_unique=not next_is_fallback,
            )
            if next_is_fallback:
                next_item['is_fallback'] = True
        fallback_name = S1LuckyWheelService._legendary_fallback_name(settings)
        fallback_item = None
        if fallback_name:
            row = catalog.get(fallback_name)
            fallback_item = S1LuckyWheelService._serialize_player_pool_item(
                fallback_name, row, rarity='legendary', force_world_unique=False,
            )
            fallback_item['is_fallback'] = True
        return {
            'current': current_item,
            'next': next_item,
            'fallback': fallback_item,
            'on_fallback': on_fallback,
            'rotation_index': S1LuckyWheelService._get_legendary_index(settings),
            'rotation_total': len(S1LuckyWheelService._legendary_rotation(settings)),
        }

    @staticmethod
    def _rarity_weights() -> list[tuple[str, float]]:
        raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_RARITY_WEIGHTS') or {}
        weights: list[tuple[str, float]] = []
        for rarity in _VALID_RARITIES:
            try:
                w = float(raw.get(rarity, 0))
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                weights.append((rarity, w))
        if not weights:
            weights = [
                ('common', 0.6),
                ('rare', 0.35),
                ('epic', 0.045),
                ('legendary', 0.005),
            ]
        return weights

    # ── 自定义奖池管理（管理员持久化） ────────────────────────────────────

    @staticmethod
    def _get_custom_pool(settings: dict[str, str | None] | None = None) -> dict[str, list[str]]:
        """读取管理员自定义奖池（持久化在 settings 表）。"""
        raw = S1LuckyWheelService._settings_value(settings, _CUSTOM_POOL_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
        return {}

    @staticmethod
    def _save_custom_pool(pool: dict[str, list[str]]) -> None:
        """保存管理员自定义奖池到 settings 表。"""
        rec = AdventurerS1Settings.query.filter_by(key=_CUSTOM_POOL_KEY).first()
        if rec is None:
            db_manager.add(AdventurerS1Settings(
                key=_CUSTOM_POOL_KEY,
                value=json.dumps(pool, ensure_ascii=False),
                description='幸运大转盘管理员自定义奖池',
            ))
        else:
            rec.value = json.dumps(pool, ensure_ascii=False)
        invalidate_lucky_wheel_pool_cache()

    @staticmethod
    def _immutable_catalog_reference_names() -> set[str]:
        configured_pool = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_POOL') or {}
        names = {
            str(name).strip()
            for entries in configured_pool.values()
            for name in (entries or [])
            if str(name).strip()
        }
        configured_rotation = current_app.config.get(
            'ADVENTURER_S1_LUCKY_WHEEL_LEGENDARY_ROTATION',
        ) or []
        names.update(
            str(name).strip()
            for name in configured_rotation
            if str(name).strip()
        )
        names.add(S1LuckyWheelService._ticket_item_name())
        fallback = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_LEGENDARY_FALLBACK')
        if fallback is None:
            from plugins.adventurer_s1.seed import LUCKY_WHEEL_LEGENDARY_FALLBACK

            fallback = LUCKY_WHEEL_LEGENDARY_FALLBACK
        if str(fallback or '').strip():
            names.add(str(fallback).strip())
        return names

    @staticmethod
    def sync_catalog_rename(old_name: str, new_name: str) -> Optional[str]:
        """Update mutable wheel references in the caller-owned catalog transaction."""
        old_name = str(old_name or '').strip()
        new_name = str(new_name or '').strip()
        if not old_name or old_name == new_name:
            return None

        base_names = S1LuckyWheelService._immutable_catalog_reference_names()
        if old_name in base_names:
            return '该名称由幸运转盘基础配置引用，不能直接改名'

        rotation = S1LuckyWheelService._get_custom_rotation()
        custom_pool = S1LuckyWheelService._get_custom_pool()
        if old_name in rotation and new_name in rotation:
            return f'幸运转盘传说轮换已包含「{new_name}」'
        for names in custom_pool.values():
            if old_name in names and new_name in names:
                return f'幸运转盘奖池已包含「{new_name}」'

        rotation_changed = old_name in rotation
        if rotation_changed:
            rotation = [new_name if name == old_name else name for name in rotation]
        pool_changed = False
        for rarity, names in list(custom_pool.items()):
            if old_name not in names:
                continue
            custom_pool[rarity] = [
                new_name if name == old_name else name
                for name in names
            ]
            pool_changed = True
        if rotation_changed:
            S1LuckyWheelService._save_custom_rotation(rotation)
        if pool_changed:
            S1LuckyWheelService._save_custom_pool(custom_pool)
        return None

    @staticmethod
    def catalog_name_is_referenced(name: str) -> bool:
        clean_name = str(name or '').strip()
        if not clean_name:
            return False
        if clean_name in S1LuckyWheelService._immutable_catalog_reference_names():
            return True
        if clean_name in S1LuckyWheelService._get_custom_rotation():
            return True
        return any(
            clean_name in names
            for names in S1LuckyWheelService._get_custom_pool().values()
        )

    @staticmethod
    def _get_base_excluded(settings: dict[str, str | None] | None = None) -> set[str]:
        """读取管理员排除的基础物品名单。"""
        raw = S1LuckyWheelService._settings_value(settings, _BASE_EXCLUDED_KEY)
        if not raw:
            return set()
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return {str(n).strip() for n in data if str(n).strip()}
        except (json.JSONDecodeError, TypeError):
            pass
        return set()

    @staticmethod
    def _save_base_excluded(excluded: set[str]) -> None:
        """保存管理员排除的基础物品名单（含传说物品）。"""
        from extensions import db
        from datetime import datetime, timezone
        val = json.dumps(sorted(excluded), ensure_ascii=False)
        now = datetime.now(timezone.utc)
        existing = db.session.execute(
            db.text("SELECT id FROM adventurer_s1_settings WHERE key=:k"),
            {"k": _BASE_EXCLUDED_KEY},
        ).fetchone()
        if existing:
            db.session.execute(
                db.text("UPDATE adventurer_s1_settings SET value=:v, updated_at=:t WHERE key=:k"),
                {"v": val, "k": _BASE_EXCLUDED_KEY, "t": now},
            )
        else:
            db.session.execute(
                db.text("INSERT INTO adventurer_s1_settings (key, value, description, created_at, updated_at) VALUES (:k, :v, :d, :t, :t)"),
                {"k": _BASE_EXCLUDED_KEY, "v": val, "d": "幸运大转盘被排除的基础物品", "t": now},
            )
        db_manager.flush()
        invalidate_lucky_wheel_pool_cache()

    @staticmethod
    def _claimed_world_unique_names(
        names: set[str],
        catalog: dict[str, AdventurerS1Item],
    ) -> set[str]:
        """已有持有者的全服唯一物品（应从奖池展示/抽取中剔除）。"""
        from plugins.adventurer_s1.services.s1_item_world_unique_service import (
            load_item_holders_map,
        )

        world_unique_names = [
            name for name in names
            if name
            and catalog.get(name)
            and bool(getattr(catalog[name], 'world_unique', False))
        ]
        if not world_unique_names:
            return set()
        holders_map = load_item_holders_map(world_unique_names)
        return {name for name in world_unique_names if holders_map.get(name)}

    @staticmethod
    def _retire_world_unique_pool_item(
        name: str, rarity: str, settings: dict[str, str | None] | None = None,
    ) -> None:
        """全服唯一物品被抽走后从奖池退役：自定义池删除，基础池写入排除名单。"""
        name = str(name or '').strip()
        rarity = str(rarity or '').strip().lower()
        if not name or rarity not in _VALID_RARITIES or rarity == 'legendary':
            return

        custom = S1LuckyWheelService._get_custom_pool(settings)
        rarity_list = custom.get(rarity) or []
        if name in rarity_list:
            rarity_list.remove(name)
            if rarity_list:
                custom[rarity] = rarity_list
            else:
                custom.pop(rarity, None)
            if settings is None:
                S1LuckyWheelService._save_custom_pool(custom)
            else:
                settings[_CUSTOM_POOL_KEY] = json.dumps(custom, ensure_ascii=False)
            return

        raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_POOL') or {}
        base_names = [str(n).strip() for n in (raw.get(rarity) or []) if str(n).strip()]
        if name in base_names:
            excluded = S1LuckyWheelService._get_base_excluded(settings)
            if name in excluded:
                return
            excluded.add(name)
            if settings is None:
                S1LuckyWheelService._save_base_excluded(excluded)
            else:
                settings[_BASE_EXCLUDED_KEY] = json.dumps(sorted(excluded), ensure_ascii=False)

    @staticmethod
    def _pool_by_rarity(
        settings: dict[str, str | None] | None = None, *, filter_claimed: bool = True,
    ) -> dict[str, list[str]]:
        raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_POOL') or {}
        custom = S1LuckyWheelService._get_custom_pool(settings)
        excluded = S1LuckyWheelService._get_base_excluded(settings)
        pool: dict[str, list[str]] = {}
        for rarity in _VALID_RARITIES:
            if rarity == 'legendary':
                current = S1LuckyWheelService._current_legendary_name(settings)
                if current:
                    pool[rarity] = [current]
                continue
            base_names = [n for n in (raw.get(rarity) or []) if n not in excluded]
            custom_names = custom.get(rarity) or []
            all_names = list(base_names) + [
                n for n in custom_names if n not in base_names and n not in excluded
            ]
            cleaned = [str(n).strip() for n in all_names if str(n).strip()]
            if cleaned:
                pool[rarity] = cleaned

        all_names = {n for names in pool.values() for n in names}
        if not all_names or not filter_claimed:
            return pool
        catalog = S1LuckyWheelService._catalog_map(all_names)
        claimed = S1LuckyWheelService._claimed_world_unique_names(all_names, catalog)
        if not claimed:
            return pool
        filtered: dict[str, list[str]] = {}
        for rarity, names in pool.items():
            kept = [n for n in names if n not in claimed]
            if kept:
                filtered[rarity] = kept
        return filtered

    @staticmethod
    def _get_building_instance(building_instance_id: int) -> Optional[AdventurerS1BuildingInstance]:
        return (
            AdventurerS1BuildingInstance.query
            .options(joinedload(AdventurerS1BuildingInstance.template))
            .filter_by(id=building_instance_id)
            .first()
        )

    @staticmethod
    def _validate_building(
        inst: Optional[AdventurerS1BuildingInstance],
    ) -> Tuple[Optional[AdventurerS1BuildingInstance], Optional[str]]:
        if not inst:
            return None, '建筑不存在'
        if inst.status != 'built':
            return None, '建筑尚未建成'
        template = inst.template
        if not template or template.handler_key != _HANDLER_KEY:
            return None, '该建筑不是幸运大转盘'
        return inst, None

    @staticmethod
    def _spin_block_reason(adv: AdventurerS1) -> Optional[str]:
        """抽奖前状态校验：占用中或仓库已满时禁止抽奖。"""
        can, _ = adv.can_dispatch()
        if can:
            return warehouse_capacity_error(adv.id)

        if adv.status == 'building':
            label = '建筑'
            inst = adv.building_instance
            if inst and inst.template:
                label = inst.template.name or label
            return f'「{adv.name}」正在{label}中，请等待结束后再抽奖'

        if adv.status == 'in_party':
            return f'「{adv.name}」正在小队中，请等待结束后再抽奖'

        if adv.building_instance_id:
            return f'「{adv.name}」正在帮工建筑，请等待结束后再抽奖'

        if adv.status == 'exploring':
            return f'「{adv.name}」正在探险中，请等待结束后再抽奖'

        if adv.status == 'dead':
            return '冒险者已死亡'
        if adv.status == 'archived':
            return '冒险者已归档'

        return '当前无法进行抽奖'

    @staticmethod
    def _item_grant_destination(adventurer_id: int, item_name: str) -> Optional[str]:
        from plugins.adventurer_s1.models import AdventurerS1Equipment, AdventurerS1WarehouseItem

        if AdventurerS1Equipment.query.filter_by(
            adventurer_id=adventurer_id, name=item_name,
        ).first():
            return 'equipment'
        if AdventurerS1WarehouseItem.query.filter_by(
            adventurer_id=adventurer_id, name=item_name,
        ).first():
            return 'warehouse'
        return None

    @staticmethod
    def _get_owned_adventurer(
        adventurer_id: int,
        user_id: int,
        *,
        is_admin: bool = False,
        for_update: bool = False,
    ) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        query = AdventurerS1.query.filter_by(id=adventurer_id)
        if for_update:
            query = query.populate_existing().with_for_update()
        adv = query.first()
        if not adv:
            return None, '冒险者不存在'
        if not is_admin and adv.owner_id != user_id:
            return None, '无权操作该冒险者'
        return adv, None

    @staticmethod
    def _catalog_map(names: set[str]) -> dict[str, AdventurerS1Item]:
        if not names:
            return {}
        rows = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(list(names))).all()
        return {row.name: row for row in rows}

    @staticmethod
    def _ticket_count(adventurer_id: int, ticket_name: str) -> int:
        from plugins.adventurer_s1.models import AdventurerS1Equipment, AdventurerS1WarehouseItem

        total = 0
        for model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
            qty_col = getattr(model, 'quantity', None)
            if qty_col is not None:
                row_total = (
                    model.query.with_entities(func.coalesce(func.sum(qty_col), 0))
                    .filter_by(adventurer_id=adventurer_id, name=ticket_name)
                    .scalar()
                )
                total += int(row_total or 0)
            else:
                total += model.query.filter_by(
                    adventurer_id=adventurer_id, name=ticket_name,
                ).count()
        return total

    @staticmethod
    def _find_ticket_row(adventurer_id: int, ticket_name: str):
        from plugins.adventurer_s1.models import AdventurerS1Equipment, AdventurerS1WarehouseItem
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            lock_warehouse_aggregate,
        )

        lock_warehouse_aggregate(adventurer_id)

        item = (
            AdventurerS1Equipment.query
            .filter_by(adventurer_id=adventurer_id, name=ticket_name)
            .with_for_update()
            .first()
        )
        if item:
            return item, 'carry'
        item = (
            AdventurerS1WarehouseItem.query
            .filter_by(adventurer_id=adventurer_id, name=ticket_name)
            .with_for_update()
            .first()
        )
        if item:
            return item, 'warehouse'
        return None, None

    @staticmethod
    def _consume_ticket(adventurer_id: int, ticket_name: str) -> Optional[str]:
        item, _source = S1LuckyWheelService._find_ticket_row(adventurer_id, ticket_name)
        if not item:
            return f'没有「{ticket_name}」'
        _, err = consume_item_stack(
            item, 1,
            source_type='lucky_wheel', description=f'转盘抽奖消耗「{ticket_name}」',
        )
        return err

    @staticmethod
    def _roll_rarity() -> str:
        weights = S1LuckyWheelService._rarity_weights()
        rarities = [r for r, _ in weights]
        probs = [w for _, w in weights]
        return random.choices(rarities, weights=probs, k=1)[0]

    @staticmethod
    def _roll_item_name(
        rarity: str,
        pool: dict[str, list[str]],
        settings: dict[str, str | None] | None = None,
    ) -> Optional[str]:
        if rarity == 'legendary':
            return S1LuckyWheelService._current_legendary_name(settings)
        names = pool.get(rarity) or []
        if not names:
            for fallback in _VALID_RARITIES:
                if fallback == 'legendary':
                    continue
                if pool.get(fallback):
                    names = pool[fallback]
                    break
        if not names:
            return None
        return random.choice(names)

    @staticmethod
    def _latest_spin_id(adventurer_id: int) -> int:
        return int(AdventurerS1LuckyWheelSpin.query.with_entities(
            func.max(AdventurerS1LuckyWheelSpin.id),
        ).filter_by(adventurer_id=adventurer_id).scalar() or 0)

    @staticmethod
    def _batch_size() -> int:
        return max(1, int(current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_BATCH_SIZE', 1)))

    @staticmethod
    def _build_adventurer_wallet_payload(
        adv: AdventurerS1,
        ticket_name: str,
        *,
        gold_balance: float | None = None,
    ) -> dict:
        spin_block = S1LuckyWheelService._spin_block_reason(adv)
        return {
            'id': adv.id,
            'name': adv.name,
            'gold': round(float(gold_balance if gold_balance is not None else adv.gold or 0), 2),
            'ticket_count': S1LuckyWheelService._ticket_count(adv.id, ticket_name),
            'last_spin_id': S1LuckyWheelService._latest_spin_id(adv.id),
            'can_spin': spin_block is None,
            'spin_block_reason': spin_block,
        }

    @staticmethod
    def _shared_pool_cache_key(settings: dict[str, str | None]) -> str:
        index = S1LuckyWheelService._get_legendary_index(settings)
        excluded = S1LuckyWheelService._get_base_excluded(settings)
        custom_pool = S1LuckyWheelService._settings_value(settings, _CUSTOM_POOL_KEY) or ''
        custom_rotation = S1LuckyWheelService._settings_value(settings, _CUSTOM_ROTATION_KEY) or ''
        pool = S1LuckyWheelService._pool_by_rarity(settings)
        all_names = {n for names in pool.values() for n in names}
        claimed = frozenset()
        if all_names:
            catalog = S1LuckyWheelService._catalog_map(all_names)
            claimed = frozenset(S1LuckyWheelService._claimed_world_unique_names(all_names, catalog))
        return (
            f'{index}:{hash(frozenset(excluded))}:'
            f'{hash(custom_pool)}:{hash(custom_rotation)}:{hash(claimed)}'
        )

    @staticmethod
    def _build_shared_pool_body(settings: dict[str, str | None]) -> dict:
        cache_key = S1LuckyWheelService._shared_pool_cache_key(settings)
        cached = _lucky_wheel_shared_cache.get(cache_key)
        if cached is not None:
            return cached

        pool = S1LuckyWheelService._pool_by_rarity(settings)
        rotation = S1LuckyWheelService._legendary_rotation(settings)
        all_names = {n for names in pool.values() for n in names}
        all_names.update(rotation)
        fallback_name = S1LuckyWheelService._legendary_fallback_name(settings)
        if fallback_name:
            all_names.add(fallback_name)
        ticket_name = S1LuckyWheelService._ticket_item_name()
        all_names.add(ticket_name)
        catalog = S1LuckyWheelService._catalog_map(all_names)
        legendary_preview = S1LuckyWheelService._legendary_preview_payload(catalog, settings)

        tiers: list[dict] = []
        for rarity in _VALID_RARITIES:
            if rarity == 'legendary':
                current = legendary_preview.get('current')
                if not current:
                    continue
                items = [current]
            else:
                item_names = pool.get(rarity) or []
                if not item_names:
                    continue
                items = [
                    S1LuckyWheelService._serialize_player_pool_item(
                        name, catalog.get(name), rarity=rarity,
                    )
                    for name in item_names
                ]
            weight = next((w for r, w in S1LuckyWheelService._rarity_weights() if r == rarity), 0)
            tiers.append({
                'rarity': rarity,
                'probability': round(weight, 4),
                'probability_percent': round(weight * 100, 2),
                'items': items,
            })

        payload = {
            'ticket_item_name': ticket_name,
            'tiers': tiers,
            'legendary_rotation': legendary_preview,
        }
        _lucky_wheel_shared_cache.set(cache_key, payload)
        return payload

    @staticmethod
    def _resolve_adventurer_payload(
        user_id: int,
        ticket_name: str,
        *,
        adventurer_id: int | None = None,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        if adventurer_id:
            adv, adv_err = S1LuckyWheelService._get_owned_adventurer(
                adventurer_id, user_id, is_admin=is_admin,
            )
            if adv_err:
                return None, adv_err
        else:
            adv = (
                AdventurerS1.query.filter_by(owner_id=user_id)
                .filter(AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES))
                .order_by(AdventurerS1.id.asc())
                .first()
            )
            if not adv:
                return None, None
        return S1LuckyWheelService._build_adventurer_wallet_payload(adv, ticket_name), None

    @staticmethod
    def _build_pool_payload(
        user_id: int,
        *,
        adventurer_id: int | None = None,
        is_admin: bool = False,
        settings: dict[str, str | None] | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        if settings is None:
            settings = S1LuckyWheelService._load_wheel_settings()

        shared = S1LuckyWheelService._build_shared_pool_body(settings)
        adv_payload, adv_err = S1LuckyWheelService._resolve_adventurer_payload(
            user_id, shared['ticket_item_name'],
            adventurer_id=adventurer_id, is_admin=is_admin,
        )
        if adv_err:
            return None, adv_err

        return {
            'ticket_item_name': shared['ticket_item_name'],
            'batch_size': S1LuckyWheelService._batch_size(),
            'tiers': shared['tiers'],
            'legendary_rotation': shared['legendary_rotation'],
            'adventurer': adv_payload,
        }, None

    @staticmethod
    def get_pool_preview(
        building_instance_id: int,
        user_id: int,
        *,
        adventurer_id: int | None = None,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        inst, err = S1LuckyWheelService._validate_building(
            S1LuckyWheelService._get_building_instance(building_instance_id),
        )
        if err:
            return None, err

        return S1LuckyWheelService._build_pool_payload(
            user_id, adventurer_id=adventurer_id, is_admin=is_admin,
        )

    @staticmethod
    @with_transaction(auto_commit=True)
    def spin(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        payment: str,
        *,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        inst, err = S1LuckyWheelService._validate_building(
            S1LuckyWheelService._get_building_instance(building_instance_id),
        )
        if err:
            return None, err

        pay = str(payment or '').strip().lower()
        if pay == 'gold':
            return None, '大转盘仅支持抽奖券'
        if pay not in _VALID_PAYMENT:
            return None, 'payment 须为 ticket'

        adv, err = S1LuckyWheelService._get_owned_adventurer(
            adventurer_id, user_id, is_admin=is_admin, for_update=True,
        )
        if err:
            return None, err
        # 与所有仓库写入口共用聚合锁，锁后复查容量，避免检查与发奖之间被并发填满。
        lock_warehouse_aggregate(adv.id)
        spin_block = S1LuckyWheelService._spin_block_reason(adv)
        if spin_block:
            return None, spin_block

        S1LuckyWheelService._lock_legendary_index_row()
        settings = S1LuckyWheelService._load_wheel_settings()
        pool = S1LuckyWheelService._pool_by_rarity(settings)
        if not pool:
            return None, '奖池未配置'

        ticket_name = S1LuckyWheelService._ticket_item_name()

        rarity = S1LuckyWheelService._roll_rarity()
        if rarity == 'legendary' and not S1LuckyWheelService._current_legendary_name(settings):
            rarity = 'epic'

        rotation_legendary_name = S1LuckyWheelService._current_rotation_legendary_name(settings)
        item_name = S1LuckyWheelService._roll_item_name(rarity, pool, settings)
        if not item_name:
            return None, '奖池为空，请稍后再试'
        catalog = AdventurerS1Item.query.filter_by(name=item_name).first()
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            build_clue_attach_meta,
            sanitize_player_clue_error,
        )
        clue_fields = build_clue_attach_meta(catalog) if catalog is not None else None

        ticket_err = S1LuckyWheelService._consume_ticket(adv.id, ticket_name)
        if ticket_err:
            return None, ticket_err

        _, grant_err = grant_catalog_item(
            adv, item_name, quantity=1, source_type='lucky_wheel',
        )
        if grant_err:
            db_manager.rollback()
            if clue_fields:
                grant_err = sanitize_player_clue_error(grant_err, item_name)
            return None, grant_err

        grant_destination = S1LuckyWheelService._item_grant_destination(adv.id, item_name)

        rotation_advanced = (
            rarity == 'legendary'
            and rotation_legendary_name is not None
            and item_name == rotation_legendary_name
        )
        if rotation_advanced:
            S1LuckyWheelService._advance_legendary_index()
            settings = S1LuckyWheelService._load_wheel_settings()

        is_fallback_win = S1LuckyWheelService._is_legendary_fallback_name(item_name, settings)

        if (
            catalog
            and bool(getattr(catalog, 'world_unique', False))
            and not is_fallback_win
            and not rotation_advanced
        ):
            S1LuckyWheelService._retire_world_unique_pool_item(item_name, rarity)
            settings = S1LuckyWheelService._load_wheel_settings()

        item_payload = S1LuckyWheelService._serialize_player_pool_item(
            item_name,
            catalog,
            rarity=catalog.rarity if catalog else rarity,
            force_world_unique=(rarity == 'legendary' and not is_fallback_win),
        )
        if is_fallback_win:
            item_payload['is_fallback'] = True

        ok, add_err = db_manager.add(AdventurerS1LuckyWheelSpin(
            adventurer_id=adv.id,
            building_instance_id=building_instance_id,
            source_currency_log_id=None,
            item_name=item_name,
            item_rarity=catalog.rarity if catalog else rarity,
            payment=pay,
            gold_cost=0,
        ))
        if not ok:
            db_manager.rollback()
            return None, add_err or '抽奖记录写入失败'

        adventurer_payload = S1LuckyWheelService._build_adventurer_wallet_payload(
            adv, ticket_name,
        )

        result: dict = {
            'rarity': rarity,
            'item_name': item_name,
            'item_type': catalog.type if catalog else '',
            'item_description': (catalog.description or '') if catalog else '',
            'item_rarity': catalog.rarity if catalog else rarity,
            'item': item_payload,
            'grant_destination': grant_destination,
            'payment': pay,
            'gold_cost': 0,
            'ticket_item_name': ticket_name,
            'adventurer': adventurer_payload,
        }
        if catalog is not None:
            from plugins.adventurer_s1.services.s1_item_use_condition_service import (
                sanitize_player_catalog_item_dict,
            )

            sanitize_player_catalog_item_dict(
                result,
                clue_fields,
                name_key='item_name',
                desc_key='item_description',
                type_key='item_type',
            )

        if rotation_advanced or (rarity == 'legendary' and is_fallback_win):
            shared = S1LuckyWheelService._build_shared_pool_body(settings)
            result['legendary_rotation'] = shared['legendary_rotation']
            legendary_tier = next(
                (tier for tier in shared['tiers'] if tier.get('rarity') == 'legendary'),
                None,
            )
            if legendary_tier:
                result['legendary_tier'] = legendary_tier

        return result, None

    @staticmethod
    @with_transaction(auto_commit=True)
    def spin_batch(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        count: int,
        expected_spin_id: int,
        *,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """One bounded SQL transaction; draw order is evaluated against an in-memory inventory."""
        if type(count) is not int or not 1 <= count <= S1LuckyWheelService._batch_size():
            return None, f'每批抽取次数须为 1 至 {S1LuckyWheelService._batch_size()} 的整数'
        if type(expected_spin_id) is not int or expected_spin_id < 0:
            return None, '请刷新抽奖面板后重试'
        _, err = S1LuckyWheelService._validate_building(
            S1LuckyWheelService._get_building_instance(building_instance_id),
        )
        if err:
            return None, err
        adv, err = S1LuckyWheelService._get_owned_adventurer(
            adventurer_id, user_id, is_admin=is_admin, for_update=True,
        )
        if err:
            return None, err
        # The adventurer lock also serializes legacy single-spin requests.
        if S1LuckyWheelService._latest_spin_id(adv.id) != expected_spin_id:
            return None, '抽奖记录已更新，本批未重复抽取，请刷新后查看'
        lock_warehouse_aggregate(adv.id)
        spin_block = S1LuckyWheelService._spin_block_reason(adv)
        if spin_block:
            return None, spin_block
        S1LuckyWheelService._lock_legendary_index_row()
        settings = S1LuckyWheelService._load_wheel_settings()
        original_settings = dict(settings)
        pool = S1LuckyWheelService._pool_by_rarity(settings, filter_claimed=False)
        if not pool:
            return None, '奖池未配置'
        ticket_name = S1LuckyWheelService._ticket_item_name()
        names = {name for tier in pool.values() for name in tier}
        names.update(S1LuckyWheelService._legendary_rotation(settings))
        fallback = S1LuckyWheelService._legendary_fallback_name(settings)
        if fallback:
            names.add(fallback)
        names.add(ticket_name)
        batch = prepare_warehouse_grant_batch(adv, names, preload_world_unique=True)

        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            build_clue_attach_meta, sanitize_player_catalog_item_dict, sanitize_player_clue_error,
        )

        results = []
        history_rows = []
        stop_reason = None
        capacity = int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100))
        for _ in range(count):
            # Check before choosing a prize or consuming a ticket, including stackable prizes.
            if batch.warehouse_count >= capacity:
                stop_reason = f'仓库已满（{capacity} 件），请先清理仓库'
                break
            ticket = next((
                row for row in [*batch.equipment_rows, *batch.warehouse_rows]
                if row.name == ticket_name
            ), None)
            if ticket is None:
                stop_reason = f'没有「{ticket_name}」'
                break
            pool = S1LuckyWheelService._pool_by_rarity(settings, filter_claimed=False)
            claimed = {
                name for name, occupancy in (batch.world_unique_occupancy or {}).items()
                if occupancy['holders']
            }
            pool = {rarity: [name for name in tier if name not in claimed] for rarity, tier in pool.items()}
            rarity = S1LuckyWheelService._roll_rarity()
            if rarity == 'legendary' and not S1LuckyWheelService._current_legendary_name(settings):
                rarity = 'epic'
            rotation_name = S1LuckyWheelService._current_rotation_legendary_name(settings)
            item_name = S1LuckyWheelService._roll_item_name(rarity, pool, settings)
            if not item_name:
                stop_reason = '奖池为空，请稍后再试'
                break
            catalog = batch.catalog_by_name.get(item_name)
            clue_fields = build_clue_attach_meta(catalog) if catalog is not None else None
            deleted, consume_err = consume_item_stack(
                ticket, 1, source_type='lucky_wheel',
                description=f'转盘抽奖消耗「{ticket_name}」', flush=False,
            )
            if consume_err:
                db_manager.rollback()
                return None, '本批抽奖未完成，未扣除本批抽奖券'
            batch.record_consumption(ticket, 1, deleted=deleted)
            receipt = {}
            _, grant_err = grant_catalog_item(
                adv, item_name, quantity=1, source_type='lucky_wheel',
                warehouse_batch_context=batch, granted_item_out=receipt,
            )
            if grant_err:
                # Grant failures and write failures roll back every draw in this request.
                db_manager.rollback()
                if clue_fields:
                    grant_err = sanitize_player_clue_error(grant_err, item_name)
                return None, f'本批未完成，未扣除本批抽奖券：{grant_err}'
            advanced = rarity == 'legendary' and rotation_name is not None and item_name == rotation_name
            if advanced:
                settings[_LEGENDARY_INDEX_KEY] = str(S1LuckyWheelService._get_legendary_index(settings) + 1)
            is_fallback = S1LuckyWheelService._is_legendary_fallback_name(item_name, settings)
            if catalog is not None and catalog.world_unique and not is_fallback and not advanced:
                S1LuckyWheelService._retire_world_unique_pool_item(item_name, rarity, settings)
            item_payload = S1LuckyWheelService._serialize_player_pool_item(
                item_name, catalog, rarity=catalog.rarity if catalog else rarity,
                force_world_unique=(rarity == 'legendary' and not is_fallback),
            )
            if is_fallback:
                item_payload['is_fallback'] = True
            result = {
                'rarity': rarity,
                'item_name': item_name,
                'item_type': catalog.type if catalog else '',
                'item_description': (catalog.description or '') if catalog else '',
                'item_rarity': catalog.rarity if catalog else rarity,
                'item': item_payload,
                'grant_destination': receipt.get('destination'),
                'payment': 'ticket',
                'gold_cost': 0,
                'ticket_item_name': ticket_name,
            }
            sanitize_player_catalog_item_dict(
                result, clue_fields, name_key='item_name',
                desc_key='item_description', type_key='item_type',
            )
            results.append(result)
            history_rows.append(AdventurerS1LuckyWheelSpin(
                adventurer_id=adv.id, building_instance_id=building_instance_id,
                source_currency_log_id=None, item_name=item_name,
                item_rarity=catalog.rarity if catalog else rarity, payment='ticket', gold_cost=0,
            ))

        changed_settings = {key: value for key, value in settings.items() if original_settings.get(key) != value}
        if changed_settings:
            # Only one settings read/write phase, regardless of the number of unique wins.
            rows = AdventurerS1Settings.query.filter(AdventurerS1Settings.key.in_(changed_settings)).all()
            by_key = {row.key: row for row in rows}
            new_rows = []
            for key, value in changed_settings.items():
                if key in by_key:
                    by_key[key].value = value
                else:
                    new_rows.append(AdventurerS1Settings(key=key, value=value, description='幸运大转盘奖池状态'))
            ok, err = player_database_result(db_manager.add_all(new_rows, flush=False))
            if not ok:
                db_manager.rollback()
                return None, '本批抽奖未完成，未扣除本批抽奖券'
            invalidate_lucky_wheel_pool_cache()
        ok, err = player_database_result(db_manager.add_all(history_rows, flush=False))
        if ok:
            ok, err = player_database_result(db_manager.flush())
        if not ok:
            db_manager.rollback()
            return None, '本批抽奖未完成，未扣除本批抽奖券'
        final_pool, err = S1LuckyWheelService._build_pool_payload(
            user_id, adventurer_id=adv.id, is_admin=is_admin, settings=settings,
        )
        if err:
            db_manager.rollback()
            return None, err
        return {
            'results': results, 'requested_count': count, 'completed_count': len(results),
            'stop_reason': stop_reason, 'pool': final_pool,
        }, None

    @staticmethod
    def get_spin_history(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        *,
        page: int = 1,
        page_size: int = 10,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """分页读取当前冒险者在该大转盘的成功抽取历史。"""
        _, err = S1LuckyWheelService._validate_building(
            S1LuckyWheelService._get_building_instance(building_instance_id),
        )
        if err:
            return None, err

        _, err = S1LuckyWheelService._get_owned_adventurer(
            adventurer_id, user_id, is_admin=is_admin,
        )
        if err:
            return None, err

        page = max(1, int(page or 1))
        page_size = min(50, max(1, int(page_size or 10)))
        query = AdventurerS1LuckyWheelSpin.query.filter_by(
            building_instance_id=building_instance_id,
            adventurer_id=adventurer_id,
        )
        total = query.count()
        rows = (
            query
            .order_by(
                AdventurerS1LuckyWheelSpin.created_at.desc(),
                AdventurerS1LuckyWheelSpin.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items = [row.to_dict() for row in rows]
        if items:
            from plugins.adventurer_s1.services.s1_item_use_condition_service import (
                catalog_clue_meta_by_item_names,
                sanitize_player_catalog_item_dict,
            )

            clue_meta = catalog_clue_meta_by_item_names({row.item_name for row in rows})
            for item, row in zip(items, rows):
                sanitize_player_catalog_item_dict(
                    item,
                    clue_meta.get(row.item_name),
                    name_key='item_name',
                )
        return {
            'items': items,
            'total': total,
            'page': page,
            'page_size': page_size,
            'has_more': page * page_size < total,
        }, None

    # ── 管理员：奖池管理 ─────────────────────────────────────────────────

    @staticmethod
    def get_admin_pool_overview() -> Tuple[Optional[dict], Optional[str]]:
        """管理员查看完整奖池（含基础 + 自定义 + 已排除），返回按稀有度分组的物品列表。"""
        raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_POOL') or {}
        custom = S1LuckyWheelService._get_custom_pool()
        rotation = S1LuckyWheelService._legendary_rotation()
        excluded = S1LuckyWheelService._get_base_excluded()

        # 收集所有物品名（含已排除的基础物品）
        all_names: set[str] = set()
        fallback_name = S1LuckyWheelService._legendary_fallback_name()
        if fallback_name:
            all_names.add(fallback_name)
        for rarity in _VALID_RARITIES:
            if rarity == 'legendary':
                all_names.update(rotation)
                all_names.update(excluded)
                continue
            for source in (raw.get(rarity) or [], custom.get(rarity) or []):
                all_names.update(str(n).strip() for n in source if str(n).strip())
        catalog_map = S1LuckyWheelService._catalog_map(all_names)

        tiers: list[dict] = []
        legendary_index = S1LuckyWheelService._get_legendary_index()
        custom_rotation = S1LuckyWheelService._get_custom_rotation()
        for rarity in _VALID_RARITIES:
            if rarity == 'legendary':
                items = []
                active_idx = 0  # 只计算未排除物品的下标
                for name in rotation:
                    is_excluded = name in excluded
                    row = catalog_map.get(name)
                    item_data = S1LuckyWheelService._admin_pool_item_payload(
                        name,
                        row,
                        'legendary',
                        world_unique=True,
                        source='custom' if name in custom_rotation else 'base',
                        excluded=is_excluded,
                    )
                    if not is_excluded:
                        item_data['current'] = active_idx == legendary_index
                        active_idx += 1
                    items.append(item_data)
                fallback_name = S1LuckyWheelService._legendary_fallback_name()
                if fallback_name and fallback_name not in {i['name'] for i in items}:
                    row = catalog_map.get(fallback_name)
                    items.append(S1LuckyWheelService._admin_pool_item_payload(
                        fallback_name,
                        row,
                        'legendary',
                        world_unique=False,
                        source='fallback',
                        excluded=fallback_name in excluded,
                        is_fallback=True,
                    ))
                tiers.append({'rarity': rarity, 'items': items})
                continue

            base_names = [str(n).strip() for n in (raw.get(rarity) or []) if str(n).strip()]
            custom_names = [str(n).strip() for n in (custom.get(rarity) or []) if str(n).strip()]
            items = []
            for name in base_names:
                row = catalog_map.get(name)
                items.append(S1LuckyWheelService._admin_pool_item_payload(
                    name,
                    row,
                    rarity,
                    source='base',
                    excluded=name in excluded,
                ))
            for name in custom_names:
                if name in base_names:
                    continue
                row = catalog_map.get(name)
                items.append(S1LuckyWheelService._admin_pool_item_payload(
                    name,
                    row,
                    rarity,
                    source='custom',
                ))
            tiers.append({'rarity': rarity, 'items': items})

        weights = S1LuckyWheelService._rarity_weights()
        weight_map = {r: w for r, w in weights}
        for tier in tiers:
            tier['probability_percent'] = round(weight_map.get(tier['rarity'], 0) * 100, 2)

        return {
            'tiers': tiers,
            'custom_pool_exists': bool(custom),
        }, None

    @staticmethod
    def add_custom_pool_item(
        name: str,
        description: str,
        rarity: str,
        item_type: str = 'special',
        world_unique: bool | None = None,
        legendary_position: str = 'next',
        text_locked: bool = False,
        uses_left: int | None = None,
        uses_left_provided: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        result, err = S1LuckyWheelService._add_custom_pool_item_transaction(
            name=name,
            description=description,
            rarity=rarity,
            item_type=item_type,
            world_unique=world_unique,
            legendary_position=legendary_position,
            text_locked=text_locked,
            uses_left=uses_left,
            uses_left_provided=uses_left_provided,
        )
        if err is None:
            S1LuckyWheelService._refresh_catalog_name_after_commit(name)
        return result, err

    @staticmethod
    @with_transaction(auto_commit=True)
    def _add_custom_pool_item_transaction(
        name: str,
        description: str,
        rarity: str,
        item_type: str = 'special',
        world_unique: bool | None = None,
        legendary_position: str = 'next',
        text_locked: bool = False,
        uses_left: int | None = None,
        uses_left_provided: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """管理员添加自定义奖池物品：创建图鉴条目 + 加入奖池。

        legendary_position: 'current'（当期可抽）/ 'next'（排在当前之后）。
        仅对 legendary 稀有度生效。
        """
        name = (name or '').strip()
        if not name:
            return None, '物品名称不能为空'
        rarity = (rarity or '').strip().lower()
        if rarity not in _VALID_RARITIES:
            return None, f'稀有度须为 {"/".join(_VALID_RARITIES)}'

        # 创建或更新图鉴条目
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )

        lock_catalog_names([name], shared=False, wait=True)
        catalog = (
            AdventurerS1Item.query
            .filter_by(name=name)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if catalog is None:
            from plugins.adventurer_s1.services.s1_item_use_condition_service import (
                normalize_use_condition,
            )
            from plugins.adventurer_s1.services.s1_item_world_unique_service import (
                resolve_world_unique_flag,
                world_unique_promotion_error,
            )
            resolved_type = (item_type or 'special').strip()
            resolved_description = (description or '').strip()
            requested_world_unique = True if rarity == 'legendary' else world_unique
            effective_world_unique = resolve_world_unique_flag(
                name,
                item_type=resolved_type,
                desc=resolved_description,
                catalog_flag=requested_world_unique,
            )
            if effective_world_unique:
                promotion_error = world_unique_promotion_error(
                    name,
                    item_type=resolved_type,
                )
                if promotion_error:
                    return None, promotion_error
            use_condition = normalize_use_condition(
                {'text_locked': True} if text_locked else None,
            )
            catalog = AdventurerS1Item(
                name=name,
                type=resolved_type,
                description=resolved_description,
                rarity=rarity,
                world_unique=effective_world_unique,
                use_condition=use_condition,
            )
            condition, condition_err = S1LuckyWheelService._updated_catalog_use_condition(
                catalog,
                text_locked=True if text_locked else None,
                uses_left=uses_left,
                uses_left_provided=uses_left_provided,
            )
            if condition_err:
                return None, condition_err
            catalog.use_condition = condition
            ok, add_err = db_manager.add(catalog)
            if not ok:
                db_manager.rollback()
                return None, add_err or '创建图鉴失败'
            pool_result, pool_err = S1LuckyWheelService._add_name_to_pool(
                name,
                rarity,
                legendary_position,
            )
            if pool_err:
                db_manager.rollback()
                return None, pool_err
            return pool_result, None

        condition, condition_err = S1LuckyWheelService._updated_catalog_use_condition(
            catalog,
            text_locked=True if text_locked else None,
            uses_left=uses_left,
            uses_left_provided=uses_left_provided,
        )
        if condition_err:
            return None, condition_err
        pool_result, pool_err = S1LuckyWheelService._add_name_to_pool(
            name,
            rarity,
            legendary_position,
        )
        if pool_err:
            return None, pool_err
        update_body = {'rarity': rarity}
        if rarity == 'legendary':
            update_body['world_unique'] = True
        elif world_unique is not None:
            update_body['world_unique'] = bool(world_unique)
        if description:
            update_body['description'] = description.strip()
        if item_type:
            update_body['type'] = item_type.strip()
        if condition != catalog.use_condition:
            update_body['use_condition'] = condition
        from plugins.adventurer_s1.services.s1_item_catalog_service import (
            S1ItemCatalogService,
        )

        _, update_err = S1ItemCatalogService.update_catalog_item(
            catalog.id,
            update_body,
            auto_commit=False,
        )
        if update_err:
            db_manager.rollback()
            return None, update_err
        return pool_result, None

    @staticmethod
    def add_catalog_pool_item(
        name: str,
        rarity: str,
        legendary_position: str = 'next',
        text_locked: bool = False,
        uses_left: int | None = None,
        uses_left_provided: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        result, err = S1LuckyWheelService._add_catalog_pool_item_transaction(
            name=name,
            rarity=rarity,
            legendary_position=legendary_position,
            text_locked=text_locked,
            uses_left=uses_left,
            uses_left_provided=uses_left_provided,
        )
        if err is None:
            S1LuckyWheelService._refresh_catalog_name_after_commit(name)
        return result, err

    @staticmethod
    @with_transaction(auto_commit=True)
    def _add_catalog_pool_item_transaction(
        name: str,
        rarity: str,
        legendary_position: str = 'next',
        text_locked: bool = False,
        uses_left: int | None = None,
        uses_left_provided: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """管理员将已有图鉴物品加入奖池（不覆盖图鉴名称/描述/类型）。"""
        name = (name or '').strip()
        if not name:
            return None, '物品名称不能为空'
        rarity = (rarity or '').strip().lower()
        if rarity not in _VALID_RARITIES:
            return None, f'稀有度须为 {"/".join(_VALID_RARITIES)}'

        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )

        lock_catalog_names([name], shared=False, wait=True)
        catalog = (
            AdventurerS1Item.query
            .filter_by(name=name)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if catalog is None:
            return None, f'图鉴中不存在「{name}」'

        condition, condition_err = S1LuckyWheelService._updated_catalog_use_condition(
            catalog,
            text_locked=True if text_locked else None,
            uses_left=uses_left,
            uses_left_provided=uses_left_provided,
        )
        if condition_err:
            return None, condition_err
        pool_result, pool_err = S1LuckyWheelService._add_name_to_pool(
            name,
            rarity,
            legendary_position,
        )
        if pool_err:
            return None, pool_err
        if condition != catalog.use_condition:
            from plugins.adventurer_s1.services.s1_item_catalog_service import (
                S1ItemCatalogService,
            )

            _, update_err = S1ItemCatalogService.update_catalog_item(
                catalog.id,
                {'use_condition': condition},
                auto_commit=False,
            )
            if update_err:
                db_manager.rollback()
                return None, update_err
        return pool_result, None

    @staticmethod
    @with_transaction(auto_commit=True)
    def remove_custom_pool_item(name: str, rarity: str) -> Tuple[Optional[dict], Optional[str]]:
        """管理员从奖池移除物品（不删图鉴）。自定义物品直接删除，基础物品加入排除名单。"""
        name = (name or '').strip()
        rarity = (rarity or '').strip().lower()
        if not name:
            return None, '物品名称不能为空'
        if rarity not in _VALID_RARITIES:
            return None, f'稀有度须为 {"/".join(_VALID_RARITIES)}'

        if rarity == 'legendary':
            # 检查自定义轮换
            rotation = S1LuckyWheelService._get_custom_rotation()
            if name in rotation:
                rotation.remove(name)
                S1LuckyWheelService._save_custom_rotation(rotation)
                return {'message': f'已从传说轮换队列移除「{name}」'}, None

            # 检查基础传说物品（config 或 seed）
            full_rotation = S1LuckyWheelService._legendary_rotation()
            if name in full_rotation:
                excluded = S1LuckyWheelService._get_base_excluded()
                if name in excluded:
                    return None, f'「{name}」已被排除'
                excluded.add(name)
                S1LuckyWheelService._save_base_excluded(excluded)
                return {'message': f'已从传说轮换排除「{name}」'}, None

            return None, f'「{name}」不在传说轮换队列中'

        # 先检查是否为自定义物品
        custom = S1LuckyWheelService._get_custom_pool()
        rarity_list = custom.get(rarity) or []
        if name in rarity_list:
            rarity_list.remove(name)
            if rarity_list:
                custom[rarity] = rarity_list
            else:
                custom.pop(rarity, None)
            S1LuckyWheelService._save_custom_pool(custom)
            return {'message': f'已从 {rarity} 奖池移除「{name}」'}, None

        # 检查是否为基础物品
        raw = current_app.config.get('ADVENTURER_S1_LUCKY_WHEEL_POOL') or {}
        base_names = [str(n).strip() for n in (raw.get(rarity) or []) if str(n).strip()]
        if name in base_names:
            excluded = S1LuckyWheelService._get_base_excluded()
            if name in excluded:
                return None, f'「{name}」已被排除'
            excluded.add(name)
            S1LuckyWheelService._save_base_excluded(excluded)
            return {'message': f'已从 {rarity} 奖池排除基础物品「{name}」'}, None

        return None, f'「{name}」不在 {rarity} 奖池中'

    @staticmethod
    @with_transaction(auto_commit=True)
    def restore_base_pool_item(name: str, rarity: str) -> Tuple[Optional[dict], Optional[str]]:
        """管理员恢复被排除的基础物品（普通/传说共用同一排除名单）。"""
        name = (name or '').strip()
        rarity = (rarity or '').strip().lower()
        if not name:
            return None, '物品名称不能为空'

        excluded = S1LuckyWheelService._get_base_excluded()
        if name not in excluded:
            return None, f'「{name}」不在排除名单中'
        excluded.discard(name)
        S1LuckyWheelService._save_base_excluded(excluded)
        return {'message': f'已恢复「{name}」到奖池'}, None

    @staticmethod
    def edit_pool_item(
        old_name: str,
        old_rarity: str,
        new_name: str = '',
        description: Optional[str] = None,
        item_type: Optional[str] = None,
        world_unique: Optional[bool] = None,
        text_locked: Optional[bool] = None,
        uses_left: int | None = None,
        uses_left_provided: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        result, err = S1LuckyWheelService._edit_pool_item_transaction(
            old_name=old_name,
            old_rarity=old_rarity,
            new_name=new_name,
            description=description,
            item_type=item_type,
            world_unique=world_unique,
            text_locked=text_locked,
            uses_left=uses_left,
            uses_left_provided=uses_left_provided,
        )
        if err is None:
            final_name = new_name.strip() if new_name and new_name.strip() else old_name
            S1LuckyWheelService._refresh_catalog_name_after_commit(final_name)
        return result, err

    @staticmethod
    @with_transaction(auto_commit=True)
    def _edit_pool_item_transaction(
        old_name: str,
        old_rarity: str,
        new_name: str = '',
        description: Optional[str] = None,
        item_type: Optional[str] = None,
        world_unique: Optional[bool] = None,
        text_locked: Optional[bool] = None,
        uses_left: int | None = None,
        uses_left_provided: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """管理员编辑奖池物品属性（同步更新图鉴条目 + 奖池引用）。

        支持修改名称、描述、类型、全服唯一。如需改稀有度，应先移除再重新添加。
        """
        old_name = (old_name or '').strip()
        old_rarity = (old_rarity or '').strip().lower()
        if not old_name:
            return None, '物品名称不能为空'
        if old_rarity not in _VALID_RARITIES:
            return None, f'稀有度须为 {"/".join(_VALID_RARITIES)}'

        final_name = (
            new_name.strip()
            if new_name and new_name.strip()
            else old_name
        )
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )

        lock_catalog_names(
            [old_name, final_name],
            shared=False,
            wait=True,
        )

        if final_name != old_name and AdventurerS1Item.query.filter_by(name=final_name).first():
            return None, f'物品「{final_name}」已存在'

        # 查找图鉴条目（基础物品可能不在图鉴中，校验通过后自动创建）
        catalog = (
            AdventurerS1Item.query
            .filter_by(name=old_name)
            .populate_existing()
            .with_for_update()
            .first()
        )
        is_new_catalog = catalog is None
        if not catalog:
            from plugins.adventurer_s1.services.s1_item_world_unique_service import (
                resolve_world_unique_flag,
                world_unique_promotion_error,
            )
            resolved_type = item_type.strip() if item_type is not None else 'special'
            resolved_description = description.strip() if description is not None else ''
            requested_world_unique = (
                True if old_rarity == 'legendary' else world_unique
            )
            effective_world_unique = resolve_world_unique_flag(
                old_name,
                item_type=resolved_type,
                desc=resolved_description,
                catalog_flag=requested_world_unique,
            )
            if effective_world_unique:
                promotion_error = world_unique_promotion_error(
                    old_name,
                    item_type=resolved_type,
                )
                if promotion_error:
                    return None, promotion_error
            catalog = AdventurerS1Item(
                name=old_name,
                type=resolved_type,
                description=resolved_description,
                rarity=old_rarity,
                world_unique=effective_world_unique,
            )

        condition, condition_err = S1LuckyWheelService._updated_catalog_use_condition(
            catalog,
            text_locked=text_locked,
            uses_left=uses_left,
            uses_left_provided=uses_left_provided,
        )
        if condition_err:
            return None, condition_err
        if is_new_catalog:
            ok, add_err = db_manager.add(catalog)
            if not ok:
                db_manager.rollback()
                return None, add_err or '创建图鉴失败'
        update_body = {'name': final_name}
        if description is not None:
            update_body['description'] = description.strip()
        if item_type is not None:
            update_body['type'] = item_type.strip()
        if world_unique is not None:
            update_body['world_unique'] = bool(world_unique)
        if condition != catalog.use_condition:
            update_body['use_condition'] = condition
        from plugins.adventurer_s1.services.s1_item_catalog_service import (
            S1ItemCatalogService,
        )

        updated, update_err = S1ItemCatalogService.update_catalog_item(
            catalog.id,
            update_body,
            auto_commit=False,
        )
        if update_err:
            db_manager.rollback()
            return None, update_err

        return {
            'message': f'已更新「{final_name}」',
            'item': updated,
        }, None
