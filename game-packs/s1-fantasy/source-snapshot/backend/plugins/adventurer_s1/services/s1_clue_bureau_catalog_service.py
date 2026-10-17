"""线索追查处独立 Boss 图鉴（与任务栏 hidden_boss_hunt 的固定 Boss 分离）。

同一地牢可有多名隐藏 Boss（boss_id 唯一）；地牢名必须来自 S1_DUNGEONS。
状态（available/active/defeated）仅存在于本 catalog，不读写 adventurer_s1_hidden_boss_states。
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional

from flask import current_app, has_app_context

from db_repository import db_manager
from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_ACTIVE_DUNGEONS,
    S1_ACTIVE_DUNGEON_NAMES,
    S1_DUNGEONS,
)
from plugins.adventurer_s1.services.s1_hidden_boss_service import (
    HIDDEN_BOSS_CLUE_ITEM_KIND,
    HIDDEN_BOSS_CLUE_RARITY_ACTIVE,
    HIDDEN_BOSS_CLUE_RARITY_DEFEATED,
    HiddenBossClueDefinition,
    HiddenBossDefinition,
)

CATALOG_SETTINGS_KEY = 'clue_bureau_boss_catalog'
_CATALOG_SETTINGS_LOCK_NAME = '__s1_clue_bureau_catalog_settings__'
TRACE_POOL_SETTINGS_KEY = 'clue_bureau_trace_pool'
TRACE_POOL_SETTINGS_LOCK_NAME = '__s1_clue_bureau_trace_pool_settings__'
VALID_DUNGEON_NAMES = frozenset(d['name'] for d in S1_DUNGEONS)
BOSS_STATUS_AVAILABLE = 'available'
BOSS_STATUS_ACTIVE = 'active'
BOSS_STATUS_DEFEATED = 'defeated'
DEFAULT_BOSS_BACKGROUND_MAX_LENGTH = 600
DEFAULT_CLUE_DESCRIPTION_MAX_LENGTH = 200
DEFAULT_CLUE_TEXT_MAX_LENGTH = 300


def clue_bureau_text_limits() -> dict[str, int]:
    defaults = {
        'background': DEFAULT_BOSS_BACKGROUND_MAX_LENGTH,
        'description': DEFAULT_CLUE_DESCRIPTION_MAX_LENGTH,
        'clue_text': DEFAULT_CLUE_TEXT_MAX_LENGTH,
    }
    if not has_app_context():
        return defaults
    keys = {
        'background': 'ADVENTURER_S1_CLUE_BUREAU_BOSS_BACKGROUND_MAX_LENGTH',
        'description': 'ADVENTURER_S1_CLUE_BUREAU_CLUE_DESCRIPTION_MAX_LENGTH',
        'clue_text': 'ADVENTURER_S1_CLUE_BUREAU_CLUE_TEXT_MAX_LENGTH',
    }
    limits = {}
    for field, default in defaults.items():
        try:
            limits[field] = max(1, int(current_app.config.get(keys[field], default)))
        except (TypeError, ValueError):
            limits[field] = default
    return limits


@dataclass(frozen=True)
class ClueBureauClue:
    index: int
    item_name: str
    description: str
    clue_text: str


@dataclass(frozen=True)
class ClueBureauBoss:
    boss_id: str
    dungeon: str
    name: str
    background: str
    clues: tuple[ClueBureauClue, ClueBureauClue, ClueBureauClue, ClueBureauClue]
    status: str = BOSS_STATUS_AVAILABLE
    active_party_id: Optional[int] = None
    active_expedition_log_id: Optional[int] = None
    defeated_at: Optional[str] = None

    def as_hidden_boss_definition(self) -> HiddenBossDefinition:
        return HiddenBossDefinition(
            dungeon=self.dungeon,
            name=self.name,
            background=self.background,
            clues=tuple(
                HiddenBossClueDefinition(c.index, c.item_name, c.description, c.clue_text)
                for c in self.clues
            ),
        )


class S1ClueBureauCatalogService:
    @staticmethod
    def valid_dungeon_names() -> list[str]:
        return [d['name'] for d in S1_ACTIVE_DUNGEONS]

    @staticmethod
    def _new_boss_id() -> str:
        return f'cb_{uuid.uuid4().hex[:12]}'

    @staticmethod
    def _decode_raw_catalog_value(value) -> list[dict]:
        if not value:
            return []
        try:
            data = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def _load_raw_catalog(*, populate_existing: bool = False, session=None) -> list[dict]:
        from plugins.adventurer_s1.models import AdventurerS1Settings

        query = AdventurerS1Settings.query.filter_by(key=CATALOG_SETTINGS_KEY)
        if session is not None:
            query = query.with_session(session)
        if populate_existing:
            query = query.populate_existing()
        rec = query.first()
        return S1ClueBureauCatalogService._decode_raw_catalog_value(rec.value if rec else None)

    @staticmethod
    def _load_locked_catalog_record():
        from plugins.adventurer_s1.models import AdventurerS1Settings

        return (
            AdventurerS1Settings.query
            .filter_by(key=CATALOG_SETTINGS_KEY)
            .populate_existing()
            .with_for_update()
            .first()
        )

    @staticmethod
    def _write_catalog_record(rec, rows: list[dict], updated_by_user_id: Optional[int] = None) -> None:
        rec.value = json.dumps(rows, ensure_ascii=False)
        if updated_by_user_id is not None:
            rec.updated_by_user_id = updated_by_user_id

    @staticmethod
    def _save_raw_catalog(rows: list[dict], updated_by_user_id: Optional[int] = None) -> None:
        from plugins.adventurer_s1.models import AdventurerS1Settings

        payload = json.dumps(rows, ensure_ascii=False)
        rec = AdventurerS1Settings.query.filter_by(key=CATALOG_SETTINGS_KEY).first()
        if rec is None:
            db_manager.add(AdventurerS1Settings(
                key=CATALOG_SETTINGS_KEY,
                value=payload,
                description='线索追查处独立 Boss 图鉴（AI/GM 补充，与任务栏讨伐分离）',
                updated_by_user_id=updated_by_user_id,
            ))
        else:
            rec.value = payload
            if updated_by_user_id is not None:
                rec.updated_by_user_id = updated_by_user_id
        ok, err = db_manager.commit()
        if not ok:
            raise RuntimeError(err or '保存追查 Boss 图鉴失败')

    @staticmethod
    def _parse_clues(
        raw_clues,
        *,
        strict_types: bool = False,
    ) -> tuple[ClueBureauClue, ...] | None:
        if not isinstance(raw_clues, list) or len(raw_clues) != 4:
            return None
        limits = clue_bureau_text_limits()
        parsed: list[ClueBureauClue] = []
        seen_idx: set[int] = set()
        seen_item_names: set[str] = set()
        for item in raw_clues:
            if not isinstance(item, dict):
                return None
            raw_index = item.get('index')
            if strict_types:
                if type(raw_index) is not int:
                    return None
                idx = raw_index
            else:
                try:
                    idx = int(raw_index)
                except (TypeError, ValueError):
                    return None
            if idx not in (1, 2, 3, 4) or idx in seen_idx:
                return None
            if strict_types and not all(
                isinstance(item.get(key), str)
                for key in ('item_name', 'description', 'clue_text')
            ):
                return None
            item_name = str(item.get('item_name') or '').strip()
            description = str(item.get('description') or '').strip()
            clue_text = str(item.get('clue_text') or '').strip()
            if (
                not item_name
                or (strict_types and len(item_name) > 100)
                or item_name in seen_item_names
                or not description
                or not clue_text
                or (strict_types and len(description) > limits['description'])
                or (strict_types and len(clue_text) > limits['clue_text'])
            ):
                return None
            seen_idx.add(idx)
            seen_item_names.add(item_name)
            parsed.append(ClueBureauClue(idx, item_name, description, clue_text))
        if seen_idx != {1, 2, 3, 4}:
            return None
        parsed.sort(key=lambda c: c.index)
        return tuple(parsed)

    @staticmethod
    def _row_to_boss(row: dict) -> ClueBureauBoss | None:
        if not isinstance(row, dict):
            return None
        boss_id = str(row.get('boss_id') or '').strip()
        dungeon = str(row.get('dungeon') or '').strip()
        name = str(row.get('name') or '').strip()
        background = str(row.get('background') or '').strip()
        if not boss_id or dungeon not in VALID_DUNGEON_NAMES or not name or not background:
            return None
        clues = S1ClueBureauCatalogService._parse_clues(row.get('clues'))
        if not clues:
            return None
        status = str(row.get('status') or BOSS_STATUS_AVAILABLE).strip()
        if status not in (BOSS_STATUS_AVAILABLE, BOSS_STATUS_ACTIVE, BOSS_STATUS_DEFEATED):
            status = BOSS_STATUS_AVAILABLE
        return ClueBureauBoss(
            boss_id=boss_id,
            dungeon=dungeon,
            name=name,
            background=background,
            clues=clues,  # type: ignore[arg-type]
            status=status,
            active_party_id=row.get('active_party_id'),
            active_expedition_log_id=row.get('active_expedition_log_id'),
            defeated_at=row.get('defeated_at'),
        )

    @staticmethod
    def _boss_to_row(boss: ClueBureauBoss) -> dict:
        return {
            'boss_id': boss.boss_id,
            'dungeon': boss.dungeon,
            'name': boss.name,
            'background': boss.background,
            'clues': [
                {
                    'index': c.index,
                    'item_name': c.item_name,
                    'description': c.description,
                    'clue_text': c.clue_text,
                }
                for c in boss.clues
            ],
            'status': boss.status,
            'active_party_id': boss.active_party_id,
            'active_expedition_log_id': boss.active_expedition_log_id,
            'defeated_at': boss.defeated_at,
        }

    @staticmethod
    def list_bosses(*, populate_existing: bool = False, session=None) -> list[ClueBureauBoss]:
        out: list[ClueBureauBoss] = []
        for row in S1ClueBureauCatalogService._load_raw_catalog(
            populate_existing=populate_existing,
            **({'session': session} if session is not None else {}),
        ):
            boss = S1ClueBureauCatalogService._row_to_boss(row)
            if boss:
                out.append(boss)
        return out

    @staticmethod
    def get_boss(boss_id: str) -> ClueBureauBoss | None:
        boss_id = str(boss_id or '').strip()
        if not boss_id:
            return None
        for boss in S1ClueBureauCatalogService.list_bosses():
            if boss.boss_id == boss_id:
                return boss
        return None

    @staticmethod
    def get_boss_by_clue_item_name(item_name: str) -> tuple[ClueBureauBoss, ClueBureauClue] | None:
        name = str(item_name or '').strip()
        if not name:
            return None
        for boss in S1ClueBureauCatalogService.list_bosses():
            for clue in boss.clues:
                if clue.item_name == name:
                    return boss, clue
        return None

    @staticmethod
    def lock_grantable_clue(
        boss_id: str,
        clue_index: int,
        expected_item_name: str,
    ) -> tuple[ClueBureauBoss, ClueBureauClue] | None:
        """Lock and re-read one pool clue immediately before granting it."""
        from plugins.adventurer_s1.models import AdventurerS1Settings
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )

        boss_id = str(boss_id or '').strip()
        expected_item_name = str(expected_item_name or '').strip()
        if not boss_id or not expected_item_name:
            return None
        lock_catalog_names(
            [
                _CATALOG_SETTINGS_LOCK_NAME,
                TRACE_POOL_SETTINGS_LOCK_NAME,
                expected_item_name,
            ],
            shared=False,
            wait=True,
        )
        catalog_rec = S1ClueBureauCatalogService._load_locked_catalog_record()
        pool_rec = (
            AdventurerS1Settings.query
            .filter_by(key=TRACE_POOL_SETTINGS_KEY)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if catalog_rec is None or pool_rec is None:
            return None
        pool_ids = {
            str(value or '').strip()
            for value in S1ClueBureauCatalogService._decode_raw_catalog_value(pool_rec.value)
            if str(value or '').strip()
        }
        if boss_id not in pool_ids:
            return None
        for row in S1ClueBureauCatalogService._decode_raw_catalog_value(catalog_rec.value):
            boss = S1ClueBureauCatalogService._row_to_boss(row)
            if (
                boss is None
                or boss.boss_id != boss_id
                or boss.status != BOSS_STATUS_AVAILABLE
                or boss.dungeon not in S1_ACTIVE_DUNGEON_NAMES
            ):
                continue
            clue = next(
                (
                    candidate
                    for candidate in boss.clues
                    if candidate.index == int(clue_index)
                    and candidate.item_name == expected_item_name
                ),
                None,
            )
            return (boss, clue) if clue is not None else None
        return None

    @staticmethod
    def existing_boss_names() -> set[str]:
        return {b.name for b in S1ClueBureauCatalogService.list_bosses()}

    @staticmethod
    def existing_clue_item_names() -> set[str]:
        names: set[str] = set()
        for boss in S1ClueBureauCatalogService.list_bosses():
            for clue in boss.clues:
                names.add(clue.item_name)
        return names

    @staticmethod
    def build_clue_use_condition(boss: ClueBureauBoss, clue: ClueBureauClue) -> dict:
        return {
            'item_kind': HIDDEN_BOSS_CLUE_ITEM_KIND,
            'hidden_boss_dungeon': boss.dungeon,
            'boss_name': boss.name,
            'clue_bureau_boss_id': boss.boss_id,
            'clue_index': int(clue.index),
            'clue_text': clue.clue_text,
            'consume_on_trigger': True,
            'immutable': True,
            'text_locked': True,
        }

    @staticmethod
    def resync_all_catalog_clue_items() -> int:
        """幂等刷新追查图鉴全部线索物品的 use_condition / 描述 / 稀有度。"""
        count = 0
        for boss in S1ClueBureauCatalogService.list_bosses():
            S1ClueBureauCatalogService.sync_clue_items_for_boss(boss)
            count += 1
        return count

    @staticmethod
    def expected_clue_rarity(boss_id: str) -> str:
        boss = S1ClueBureauCatalogService.get_boss(boss_id)
        if boss and boss.status == BOSS_STATUS_DEFEATED:
            return HIDDEN_BOSS_CLUE_RARITY_DEFEATED
        return HIDDEN_BOSS_CLUE_RARITY_ACTIVE

    @staticmethod
    def iter_catalog_clue_items() -> Iterable[tuple[str, str, str, str, dict]]:
        for boss in S1ClueBureauCatalogService.list_bosses():
            rarity = S1ClueBureauCatalogService.expected_clue_rarity(boss.boss_id)
            for clue in boss.clues:
                yield (
                    clue.item_name,
                    'clue',
                    clue.description,
                    rarity,
                    S1ClueBureauCatalogService.build_clue_use_condition(boss, clue),
                )

    @staticmethod
    def _sync_clue_items_locked(bosses: list[ClueBureauBoss]) -> list:
        from plugins.adventurer_s1.models import AdventurerS1Item

        names = sorted({
            clue.item_name
            for boss in bosses
            for clue in boss.clues
        })
        changed_rows = []
        existing = (
            AdventurerS1Item.query
            .filter(AdventurerS1Item.name.in_(names))
            .order_by(AdventurerS1Item.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        ) if names else []
        by_name = {row.name: row for row in existing}
        market_rarity_by_name: dict[str, str] = {}
        for boss in bosses:
            rarity = (
                HIDDEN_BOSS_CLUE_RARITY_DEFEATED
                if boss.status == BOSS_STATUS_DEFEATED
                else HIDDEN_BOSS_CLUE_RARITY_ACTIVE
            )
            for clue in boss.clues:
                market_rarity_by_name[clue.item_name] = rarity
                cond = S1ClueBureauCatalogService.build_clue_use_condition(boss, clue)
                row = by_name.get(clue.item_name)
                if row is None:
                    row = AdventurerS1Item(
                        name=clue.item_name,
                        type='clue',
                        description=clue.description,
                        rarity=rarity,
                        is_starter=False,
                        use_condition=cond,
                    )
                    ok, add_err = db_manager.add(row, flush=False)
                    if not ok:
                        raise RuntimeError(add_err or '同步追查线索图鉴失败')
                    by_name[clue.item_name] = row
                else:
                    existing_condition = (
                        row.use_condition
                        if isinstance(row.use_condition, dict)
                        else {}
                    )
                    if existing_condition.get('item_kind') != HIDDEN_BOSS_CLUE_ITEM_KIND:
                        raise RuntimeError(
                            f'线索物品名「{clue.item_name}」已被普通图鉴占用'
                        )
                    existing_boss_id = str(
                        existing_condition.get('clue_bureau_boss_id') or ''
                    ).strip()
                    if existing_boss_id and existing_boss_id != boss.boss_id:
                        raise RuntimeError(
                            f'线索物品名「{clue.item_name}」已属于其他追查 Boss'
                        )
                    row.type = 'clue'
                    row.description = clue.description
                    row.rarity = rarity
                    row.use_condition = cond
                changed_rows.append(row)

        if market_rarity_by_name:
            from plugins.adventurer_s1.services.adventurer_s1_market_service import (
                S1MarketService,
            )

            S1MarketService.sync_hidden_boss_clue_market_state(
                market_rarity_by_name,
            )
        return changed_rows

    @staticmethod
    def sync_clue_items_for_boss(boss: ClueBureauBoss) -> None:
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )
        from plugins.adventurer_s1.services.s1_item_catalog_service import (
            S1ItemCatalogService,
        )

        names = [clue.item_name for clue in boss.clues]
        if len(names) != len(set(names)):
            raise RuntimeError('同一追查 Boss 的四条线索物品名必须互不相同')
        changed_rows = []
        with db_manager.transaction():
            lock_catalog_names(
                [_CATALOG_SETTINGS_LOCK_NAME, *names],
                shared=False,
                wait=True,
            )
            rec = S1ClueBureauCatalogService._load_locked_catalog_record()
            rows = S1ClueBureauCatalogService._decode_raw_catalog_value(
                rec.value if rec else None,
            )
            authoritative_boss = next(
                (
                    parsed
                    for row in rows
                    if (parsed := S1ClueBureauCatalogService._row_to_boss(row)) is not None
                    and parsed.boss_id == boss.boss_id
                ),
                None,
            )
            if authoritative_boss is None:
                raise RuntimeError('追查 Boss 已不存在，无法同步线索图鉴')
            authoritative_names = [clue.item_name for clue in authoritative_boss.clues]
            if set(authoritative_names) != set(names):
                raise RuntimeError('追查 Boss 线索定义已变化，请重试')
            changed_rows = S1ClueBureauCatalogService._sync_clue_items_locked(
                [authoritative_boss],
            )

        for row in changed_rows:
            if row.id is not None:
                S1ItemCatalogService.refresh_after_catalog_change(int(row.id))

    @staticmethod
    def _prepare_ai_bosses(boss_payloads: list[dict]):
        if not isinstance(boss_payloads, list) or not boss_payloads:
            return None

        limits = clue_bureau_text_limits()
        prepared = []
        for raw in boss_payloads:
            if not isinstance(raw, dict):
                return None
            if not all(isinstance(raw.get(key), str) for key in ('dungeon', 'name', 'background')):
                return None
            dungeon = raw['dungeon'].strip()
            name = raw['name'].strip()
            background = raw['background'].strip()
            if (
                dungeon not in S1_ACTIVE_DUNGEON_NAMES
                or not 2 <= len(name) <= 12
                or not background
                or len(background) > limits['background']
            ):
                return None
            clues = S1ClueBureauCatalogService._parse_clues(
                raw.get('clues'),
                strict_types=True,
            )
            if not clues:
                return None
            if any(
                exact_identity in clue_field
                for exact_identity in (name, dungeon)
                for clue in clues
                for clue_field in (
                    clue.item_name,
                    clue.description,
                    clue.clue_text,
                )
            ):
                return None
            prepared.append((dungeon, name, background, clues))

        candidate_boss_names = [name for _dungeon, name, _background, _clues in prepared]
        candidate_clue_name_list = [
            clue.item_name
            for _dungeon, _name, _background, clues in prepared
            for clue in clues
        ]
        if (
            len(candidate_boss_names) != len(set(candidate_boss_names))
            or len(candidate_clue_name_list) != len(set(candidate_clue_name_list))
        ):
            return None
        return prepared

    @staticmethod
    def _candidate_clue_names(prepared) -> set[str]:
        return {
            clue.item_name
            for _dungeon, _name, _background, clues in prepared
            for clue in clues
        }

    @staticmethod
    def _add_prepared_bosses_locked(
        prepared,
        *,
        catalog_record,
        catalog_rows: list[dict],
        updated_by_user_id: Optional[int] = None,
    ) -> tuple[list[ClueBureauBoss], list]:
        from plugins.adventurer_s1.models import AdventurerS1Item, AdventurerS1Settings

        candidate_boss_names = {
            name for _dungeon, name, _background, _clues in prepared
        }
        candidate_clue_names = S1ClueBureauCatalogService._candidate_clue_names(prepared)
        existing_bosses = [
            boss
            for row in catalog_rows
            if (boss := S1ClueBureauCatalogService._row_to_boss(row)) is not None
        ]
        existing_ids = {boss.boss_id for boss in existing_bosses}
        existing_names = {boss.name for boss in existing_bosses}
        existing_clue_names = {
            clue.item_name
            for boss in existing_bosses
            for clue in boss.clues
        }
        occupied_catalog_names = {
            str(row.name)
            for row in (
                AdventurerS1Item.query
                .with_entities(AdventurerS1Item.name)
                .filter(AdventurerS1Item.name.in_(candidate_clue_names))
                .all()
            )
        }
        if (
            candidate_boss_names & existing_names
            or candidate_clue_names & existing_clue_names
            or candidate_clue_names & occupied_catalog_names
        ):
            return [], []

        added: list[ClueBureauBoss] = []
        for dungeon, name, background, clues in prepared:
            boss_id = S1ClueBureauCatalogService._new_boss_id()
            while boss_id in existing_ids:
                boss_id = S1ClueBureauCatalogService._new_boss_id()
            boss = ClueBureauBoss(
                boss_id=boss_id,
                dungeon=dungeon,
                name=name,
                background=background,
                clues=clues,  # type: ignore[arg-type]
            )
            catalog_rows.append(S1ClueBureauCatalogService._boss_to_row(boss))
            existing_ids.add(boss_id)
            existing_names.add(name)
            existing_clue_names.update(c.item_name for c in clues)
            added.append(boss)

        if catalog_record is None:
            catalog_record = AdventurerS1Settings(
                key=CATALOG_SETTINGS_KEY,
                value='[]',
                description='线索追查处独立 Boss 图鉴（AI/GM 补充，与任务栏讨伐分离）',
                updated_by_user_id=updated_by_user_id,
            )
            ok, add_err = db_manager.add(catalog_record, flush=False)
            if not ok:
                raise RuntimeError(add_err or '保存追查 Boss 图鉴失败')
        S1ClueBureauCatalogService._write_catalog_record(
            catalog_record,
            catalog_rows,
            updated_by_user_id,
        )
        changed_rows = S1ClueBureauCatalogService._sync_clue_items_locked(added)
        return added, changed_rows

    @staticmethod
    def _refresh_catalog_rows(changed_rows: list) -> None:
        from plugins.adventurer_s1.services.s1_item_catalog_service import (
            S1ItemCatalogService,
        )

        for row in changed_rows:
            if row.id is not None:
                S1ItemCatalogService.refresh_after_catalog_change(int(row.id))

    @staticmethod
    def add_bosses_from_ai(
        boss_payloads: list[dict],
        updated_by_user_id: Optional[int] = None,
    ) -> list[ClueBureauBoss]:
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )

        prepared = S1ClueBureauCatalogService._prepare_ai_bosses(boss_payloads)
        if not prepared:
            return []

        changed_rows = []
        with db_manager.transaction():
            lock_catalog_names(
                [
                    _CATALOG_SETTINGS_LOCK_NAME,
                    *S1ClueBureauCatalogService._candidate_clue_names(prepared),
                ],
                shared=False,
                wait=True,
            )
            rec = S1ClueBureauCatalogService._load_locked_catalog_record()
            rows = S1ClueBureauCatalogService._decode_raw_catalog_value(
                rec.value if rec else None,
            )
            added, changed_rows = S1ClueBureauCatalogService._add_prepared_bosses_locked(
                prepared,
                catalog_record=rec,
                catalog_rows=rows,
                updated_by_user_id=updated_by_user_id,
            )

        S1ClueBureauCatalogService._refresh_catalog_rows(changed_rows)
        return added

    @staticmethod
    def add_bosses_from_ai_to_trace_pool(
        boss_payloads: list[dict],
        *,
        pool_cap: int,
        updated_by_user_id: Optional[int] = None,
    ) -> tuple[list[ClueBureauBoss], list[str], Optional[str]]:
        from plugins.adventurer_s1.models import AdventurerS1Settings
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )

        prepared = S1ClueBureauCatalogService._prepare_ai_bosses(boss_payloads)
        if not prepared:
            return [], [], 'invalid_candidate_batch'

        changed_rows = []
        pool: list[str] = []
        with db_manager.transaction():
            lock_catalog_names(
                [
                    _CATALOG_SETTINGS_LOCK_NAME,
                    TRACE_POOL_SETTINGS_LOCK_NAME,
                    *S1ClueBureauCatalogService._candidate_clue_names(prepared),
                ],
                shared=False,
                wait=True,
            )
            catalog_rec = S1ClueBureauCatalogService._load_locked_catalog_record()
            catalog_rows = S1ClueBureauCatalogService._decode_raw_catalog_value(
                catalog_rec.value if catalog_rec else None,
            )
            current_bosses = [
                boss
                for row in catalog_rows
                if (boss := S1ClueBureauCatalogService._row_to_boss(row)) is not None
            ]
            eligible_ids = {
                boss.boss_id
                for boss in current_bosses
                if (
                    boss.dungeon in S1_ACTIVE_DUNGEON_NAMES
                    and boss.status != BOSS_STATUS_DEFEATED
                )
            }
            pool_rec = (
                AdventurerS1Settings.query
                .filter_by(key=TRACE_POOL_SETTINGS_KEY)
                .populate_existing()
                .with_for_update()
                .first()
            )
            raw_pool = S1ClueBureauCatalogService._decode_raw_catalog_value(
                pool_rec.value if pool_rec else None,
            )
            seen_pool_ids: set[str] = set()
            for raw_id in raw_pool:
                boss_id = str(raw_id or '').strip()
                if boss_id in eligible_ids and boss_id not in seen_pool_ids:
                    seen_pool_ids.add(boss_id)
                    pool.append(boss_id)

            if max(0, int(pool_cap)) - len(pool) != len(prepared):
                return [], pool, 'trace_pool_changed'

            added, changed_rows = S1ClueBureauCatalogService._add_prepared_bosses_locked(
                prepared,
                catalog_record=catalog_rec,
                catalog_rows=catalog_rows,
                updated_by_user_id=updated_by_user_id,
            )
            if len(added) != len(prepared):
                return [], pool, 'catalog_conflict'

            pool.extend(boss.boss_id for boss in added)
            payload = json.dumps(pool, ensure_ascii=False)
            if pool_rec is None:
                pool_rec = AdventurerS1Settings(
                    key=TRACE_POOL_SETTINGS_KEY,
                    value=payload,
                    description='线索追查处追查池（boss_id 列表，独立于任务栏讨伐）',
                    updated_by_user_id=updated_by_user_id,
                )
                ok, add_err = db_manager.add(pool_rec, flush=False)
                if not ok:
                    raise RuntimeError(add_err or '保存线索追查池失败')
            else:
                pool_rec.value = payload
                if updated_by_user_id is not None:
                    pool_rec.updated_by_user_id = updated_by_user_id

        S1ClueBureauCatalogService._refresh_catalog_rows(changed_rows)
        return added, pool, None

    @staticmethod
    def _active_lock_is_stale(row: dict) -> bool:
        """追查 Boss 被 active 锁定但关联探险/小队已结束时视为过期锁。"""
        party_id = int(row.get('active_party_id') or 0)
        log_id_raw = row.get('active_expedition_log_id')
        if not party_id:
            return True
        from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog, AdventurerS1Party

        party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
        if not party or party.status == 'disbanded':
            return True
        if log_id_raw:
            log = AdventurerS1ExpeditionLog.query.session.get(AdventurerS1ExpeditionLog, int(log_id_raw))
            if not log or log.status in ('failed', 'completed'):
                return True
        elif party.status != 'exploring':
            return True
        return False

    @staticmethod
    def _release_active_row(row: dict) -> None:
        row['status'] = BOSS_STATUS_AVAILABLE
        row['active_party_id'] = None
        row['active_expedition_log_id'] = None

    @staticmethod
    def release_stale_active_lock(boss_id: str) -> bool:
        """过期 active 锁恢复为 available；返回是否执行了解锁。"""
        boss_id = str(boss_id or '').strip()
        if not boss_id:
            return False
        released = False
        with db_manager.transaction():
            rec = S1ClueBureauCatalogService._load_locked_catalog_record()
            if not rec:
                return False
            rows = S1ClueBureauCatalogService._decode_raw_catalog_value(rec.value)
            for row in rows:
                if str(row.get('boss_id') or '').strip() != boss_id:
                    continue
                if str(row.get('status') or '') != BOSS_STATUS_ACTIVE:
                    return False
                if not S1ClueBureauCatalogService._active_lock_is_stale(row):
                    return False
                S1ClueBureauCatalogService._release_active_row(row)
                S1ClueBureauCatalogService._write_catalog_record(rec, rows)
                released = True
                break
        return released

    @staticmethod
    def admin_force_release_active_lock(boss_id: str) -> tuple[dict | None, str | None]:
        """管理员强制释放 active 锁（探险失败等遗留）。"""
        boss_id = str(boss_id or '').strip()
        if not boss_id:
            return None, '缺少 boss_id'
        released_name = ''
        with db_manager.transaction():
            rec = S1ClueBureauCatalogService._load_locked_catalog_record()
            if not rec:
                return None, '追查图鉴未初始化'
            rows = S1ClueBureauCatalogService._decode_raw_catalog_value(rec.value)
            for row in rows:
                if str(row.get('boss_id') or '').strip() != boss_id:
                    continue
                if str(row.get('status') or '') != BOSS_STATUS_ACTIVE:
                    return None, '该 Boss 当前不在讨伐中，无需释放'
                released_name = str(row.get('name') or '').strip()
                S1ClueBureauCatalogService._release_active_row(row)
                S1ClueBureauCatalogService._write_catalog_record(rec, rows)
                return {
                    'boss_id': boss_id,
                    'boss_name': released_name,
                    'status': BOSS_STATUS_AVAILABLE,
                }, None
        return None, '未找到该追查 Boss'

    @staticmethod
    def mark_active(boss_id: str, party_id: int) -> bool:
        with db_manager.transaction():
            rec = S1ClueBureauCatalogService._load_locked_catalog_record()
            if not rec:
                return False
            rows = S1ClueBureauCatalogService._decode_raw_catalog_value(rec.value)
            for row in rows:
                if str(row.get('boss_id') or '').strip() != boss_id:
                    continue
                status = str(row.get('status') or '')
                if status == BOSS_STATUS_DEFEATED:
                    return False
                if status == BOSS_STATUS_ACTIVE:
                    if S1ClueBureauCatalogService._active_lock_is_stale(row):
                        S1ClueBureauCatalogService._release_active_row(row)
                    elif int(row.get('active_party_id') or 0) == int(party_id):
                        return True
                    else:
                        return False
                row['status'] = BOSS_STATUS_ACTIVE
                row['active_party_id'] = int(party_id)
                row['active_expedition_log_id'] = None
                S1ClueBureauCatalogService._write_catalog_record(rec, rows)
                return True
        return False

    @staticmethod
    def attach_expedition_log(boss_id: str, party_id: int, expedition_log_id: int) -> None:
        with db_manager.transaction():
            rec = S1ClueBureauCatalogService._load_locked_catalog_record()
            if not rec:
                return
            rows = S1ClueBureauCatalogService._decode_raw_catalog_value(rec.value)
            for row in rows:
                if str(row.get('boss_id') or '').strip() != boss_id:
                    continue
                if int(row.get('active_party_id') or 0) != int(party_id):
                    continue
                row['active_expedition_log_id'] = int(expedition_log_id)
                S1ClueBureauCatalogService._write_catalog_record(rec, rows)
                return

    @staticmethod
    def settle_boss_challenge(
        boss_id: str,
        party_id: int,
        expedition_log_id: int,
        completed: bool,
        *,
        settlement_callback: Optional[Callable[[], None]] = None,
        auto_commit: bool = True,
        post_commit_callbacks: Optional[list[Callable[[], None]]] = None,
        wait: Optional[bool] = None,
    ) -> bool:
        from plugins.adventurer_s1.models import AdventurerS1Settings
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            CatalogBusyError,
            lock_catalog_names,
        )

        if not auto_commit and post_commit_callbacks is None:
            raise RuntimeError('外部事务结算必须提供提交后回调容器')
        if wait is None:
            wait = auto_commit
        snapshot = S1ClueBureauCatalogService.get_boss(boss_id)
        if snapshot is None:
            raise RuntimeError('追查 Boss 已不存在，无法完成结算')
        clue_names = [clue.item_name for clue in snapshot.clues]
        now_iso = _portable_utc_now().isoformat()
        changed_rows = []
        removed_from_pool = False
        transaction_context = db_manager.transaction() if auto_commit else nullcontext()
        with transaction_context:
            locked = lock_catalog_names(
                [
                    _CATALOG_SETTINGS_LOCK_NAME,
                    TRACE_POOL_SETTINGS_LOCK_NAME,
                    *clue_names,
                ],
                shared=False,
                wait=wait,
            )
            if not locked:
                raise CatalogBusyError()
            rec = S1ClueBureauCatalogService._load_locked_catalog_record()
            if not rec:
                raise RuntimeError('追查 Boss 图鉴不存在，无法完成结算')
            rows = S1ClueBureauCatalogService._decode_raw_catalog_value(rec.value)
            for row in rows:
                if str(row.get('boss_id') or '').strip() != boss_id:
                    continue
                if str(row.get('status') or '') != BOSS_STATUS_ACTIVE:
                    raise RuntimeError('追查 Boss 状态已变化，请重试结算')
                if int(row.get('active_party_id') or 0) != int(party_id):
                    raise RuntimeError('追查 Boss 已不属于当前队伍，请重试结算')
                log_id = row.get('active_expedition_log_id')
                if log_id and int(log_id) != int(expedition_log_id):
                    raise RuntimeError('追查 Boss 已绑定其它探险记录，请重试结算')
                authoritative_boss = S1ClueBureauCatalogService._row_to_boss(row)
                if authoritative_boss is None:
                    raise RuntimeError('追查 Boss 图鉴记录无效，无法完成结算')
                authoritative_names = [clue.item_name for clue in authoritative_boss.clues]
                if set(authoritative_names) != set(clue_names):
                    raise RuntimeError('追查 Boss 线索定义已变化，请重试')
                if settlement_callback is not None:
                    settlement_callback()
                if completed:
                    row['status'] = BOSS_STATUS_DEFEATED
                    row['defeated_at'] = now_iso
                else:
                    row['status'] = BOSS_STATUS_AVAILABLE
                row['active_party_id'] = None
                row['active_expedition_log_id'] = None
                S1ClueBureauCatalogService._write_catalog_record(rec, rows)
                authoritative_boss = S1ClueBureauCatalogService._row_to_boss(row)
                if authoritative_boss is None:
                    raise RuntimeError('追查 Boss 图鉴记录无效，无法完成结算')
                changed_rows = S1ClueBureauCatalogService._sync_clue_items_locked(
                    [authoritative_boss],
                )

                if completed:
                    pool_rec = (
                        AdventurerS1Settings.query
                        .filter_by(key=TRACE_POOL_SETTINGS_KEY)
                        .populate_existing()
                        .with_for_update()
                        .first()
                    )
                    if pool_rec is not None:
                        raw_pool = S1ClueBureauCatalogService._decode_raw_catalog_value(
                            pool_rec.value,
                        )
                        clean_pool = [
                            str(raw_id or '').strip()
                            for raw_id in raw_pool
                            if str(raw_id or '').strip()
                            and str(raw_id or '').strip() != boss_id
                        ]
                        removed_from_pool = len(clean_pool) != len(raw_pool)
                        if removed_from_pool:
                            pool_rec.value = json.dumps(clean_pool, ensure_ascii=False)
                break
        if not changed_rows:
            raise RuntimeError('未找到可结算的追查 Boss')

        def finalize_after_commit() -> None:
            S1ClueBureauCatalogService._refresh_catalog_rows(changed_rows)
            if completed and removed_from_pool:
                from plugins.adventurer_s1.services.s1_clue_bureau_service import (
                    S1ClueBureauService,
                )

                S1ClueBureauService._maybe_auto_supplement_trace_pool_after_removal()

        if auto_commit:
            finalize_after_commit()
        else:
            post_commit_callbacks.append(finalize_after_commit)
        return True
