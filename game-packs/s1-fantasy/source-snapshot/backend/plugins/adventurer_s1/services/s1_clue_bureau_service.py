"""线索追查处：独立追查池 + AI 补充 Boss 图鉴（与任务栏 hidden_boss_hunt 分离）。"""
from __future__ import annotations

import json
import random
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_ACTIVE_DUNGEON_NAMES,
)
from plugins.adventurer_s1.services.s1_clue_bureau_catalog_service import (
    BOSS_STATUS_AVAILABLE,
    BOSS_STATUS_DEFEATED,
    ClueBureauBoss,
    S1ClueBureauCatalogService,
    TRACE_POOL_SETTINGS_KEY,
    TRACE_POOL_SETTINGS_LOCK_NAME,
)

MIN_SEARCH_HOURS = 1
DEFAULT_MAX_SEARCH_HOURS = 6
DEFAULT_STAMINA_PER_HOUR = 20
DEFAULT_CLUE_DROP_PROB_PER_HOUR = 0.20
DEFAULT_POOL_CAP = 10
DEFAULT_AUTO_SUPPLEMENT_THRESHOLD = 5


class S1ClueBureauService:
    @staticmethod
    def pool_cap() -> int:
        if current_app:
            raw = current_app.config.get('ADVENTURER_S1_CLUE_BUREAU_POOL_CAP', DEFAULT_POOL_CAP)
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                pass
        return DEFAULT_POOL_CAP

    @staticmethod
    def _load_pool(
        *,
        bosses_by_id: Optional[dict[str, ClueBureauBoss]] = None,
    ) -> list[str]:
        from plugins.adventurer_s1.models import AdventurerS1Settings

        rec = AdventurerS1Settings.query.filter_by(key=TRACE_POOL_SETTINGS_KEY).first()
        if not rec or not rec.value:
            return []
        try:
            data = json.loads(rec.value)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        if bosses_by_id is None:
            bosses_by_id = {
                boss.boss_id: boss
                for boss in S1ClueBureauCatalogService.list_bosses()
            }
        valid_ids = {
            boss_id
            for boss_id, boss in bosses_by_id.items()
            if boss.dungeon in S1_ACTIVE_DUNGEON_NAMES
        }
        pool: list[str] = []
        seen: set[str] = set()
        for raw in data:
            boss_id = str(raw or '').strip()
            if boss_id in valid_ids and boss_id not in seen:
                seen.add(boss_id)
                pool.append(boss_id)
        return pool

    @staticmethod
    def _save_pool(
        boss_ids: list[str],
        updated_by_user_id: Optional[int] = None,
        *,
        expected_boss_ids: Optional[list[str]] = None,
    ) -> None:
        from plugins.adventurer_s1.models import AdventurerS1Settings
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )

        desired: list[str] = []
        desired_seen: set[str] = set()
        for raw in boss_ids:
            boss_id = str(raw or '').strip()
            if boss_id and boss_id not in desired_seen:
                desired_seen.add(boss_id)
                desired.append(boss_id)

        expected = None
        if expected_boss_ids is not None:
            expected = [
                str(raw or '').strip()
                for raw in expected_boss_ids
                if str(raw or '').strip()
            ]

        with db_manager.transaction():
            lock_catalog_names(
                [TRACE_POOL_SETTINGS_LOCK_NAME],
                shared=False,
                wait=True,
            )
            rec = (
                AdventurerS1Settings.query
                .filter_by(key=TRACE_POOL_SETTINGS_KEY)
                .populate_existing()
                .with_for_update()
                .first()
            )
            valid_ids = {
                boss.boss_id
                for boss in S1ClueBureauCatalogService.list_bosses(
                    populate_existing=True,
                )
                if boss.dungeon in S1_ACTIVE_DUNGEON_NAMES
            }
            clean = [boss_id for boss_id in desired if boss_id in valid_ids]
            if expected is not None:
                try:
                    current_raw = json.loads(rec.value) if rec and rec.value else []
                except (json.JSONDecodeError, TypeError):
                    current_raw = []
                current = []
                current_seen: set[str] = set()
                for raw in current_raw if isinstance(current_raw, list) else []:
                    boss_id = str(raw or '').strip()
                    if boss_id in valid_ids and boss_id not in current_seen:
                        current_seen.add(boss_id)
                        current.append(boss_id)
                removed = set(expected) - set(clean)
                added = set(clean) - set(expected)
                clean = [boss_id for boss_id in current if boss_id not in removed]
                clean.extend(
                    boss_id for boss_id in desired
                    if boss_id in added and boss_id not in clean and boss_id in valid_ids
                )

            payload = json.dumps(clean, ensure_ascii=False)
            if rec is None:
                ok, add_err = db_manager.add(AdventurerS1Settings(
                    key=TRACE_POOL_SETTINGS_KEY,
                    value=payload,
                    description='线索追查处追查池（boss_id 列表，独立于任务栏讨伐）',
                    updated_by_user_id=updated_by_user_id,
                ), flush=False)
                if not ok:
                    raise RuntimeError(add_err or '保存线索追查池失败')
            else:
                rec.value = payload
                if updated_by_user_id is not None:
                    rec.updated_by_user_id = updated_by_user_id

    @staticmethod
    def auto_supplement_threshold() -> int:
        if current_app:
            raw = current_app.config.get(
                'ADVENTURER_S1_CLUE_BUREAU_AUTO_SUPPLEMENT_THRESHOLD',
                DEFAULT_AUTO_SUPPLEMENT_THRESHOLD,
            )
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                pass
        return DEFAULT_AUTO_SUPPLEMENT_THRESHOLD

    @staticmethod
    def _maybe_auto_supplement_trace_pool_after_removal() -> None:
        """Boss 移出追查池后，池内数量 <= 阈值时异步补至 cap。"""
        threshold = S1ClueBureauService.auto_supplement_threshold()
        if threshold <= 0:
            return
        pool = S1ClueBureauService._load_pool()
        if len(pool) > threshold:
            return
        S1ClueBureauService._submit_auto_supplement(len(pool), threshold)

    @staticmethod
    def _submit_auto_supplement(pool_count: int, threshold: int) -> None:
        from services.async_task_service import AsyncTaskService
        payload = {}
        if AsyncTaskService.is_task_enqueued_or_running('ai_s1_clue_bureau_supplement', payload):
            return
        if not AsyncTaskService.submit_task('ai_s1_clue_bureau_supplement', payload) and current_app:
            current_app.logger.warning(
                '线索追查池自动补充入队失败 count=%s threshold=%s', pool_count, threshold,
            )

    @staticmethod
    def auto_supplement_trace_pool_job() -> None:
        """恢复曾经补充失败的低库存池；调度线程只投递，不调用 AI。"""
        threshold = S1ClueBureauService.auto_supplement_threshold()
        if threshold <= 0:
            return
        try:
            pool, bosses_by_id, _stored_pool = (
                S1ClueBureauService._eligible_pool_snapshot()
            )
        finally:
            db_manager.remove()
        count = len(pool)
        if count > threshold or count >= S1ClueBureauService.pool_cap():
            return
        # 新服/尚未入池的空目录仍由 GM 初始化。已有讨伐记录的空池
        # 则是耗尽后的恢复，不能因没有下一次讨伐而永久停止补充。
        if not pool and not any(
            boss.status == BOSS_STATUS_DEFEATED for boss in bosses_by_id.values()
        ):
            return
        S1ClueBureauService._submit_auto_supplement(count, threshold)

    @staticmethod
    def remove_from_trace_pool(boss_id: str) -> None:
        boss_id = str(boss_id or '').strip()
        if not boss_id:
            return
        pool = S1ClueBureauService._load_pool()
        if boss_id not in pool:
            return
        S1ClueBureauService._save_pool(
            [x for x in pool if x != boss_id],
            expected_boss_ids=pool,
        )
        S1ClueBureauService._maybe_auto_supplement_trace_pool_after_removal()

    @staticmethod
    def _eligible_pool_snapshot() -> tuple[
        list[str],
        dict[str, ClueBureauBoss],
        list[str],
    ]:
        bosses_by_id = {
            boss.boss_id: boss
            for boss in S1ClueBureauCatalogService.list_bosses()
        }
        pool = S1ClueBureauService._load_pool(bosses_by_id=bosses_by_id)
        kept = [
            boss_id
            for boss_id in pool
            if (
                (boss := bosses_by_id.get(boss_id)) is not None
                and boss.dungeon in S1_ACTIVE_DUNGEON_NAMES
                and boss.status != BOSS_STATUS_DEFEATED
            )
        ]
        return kept, bosses_by_id, pool

    @staticmethod
    def _prune_ineligible_from_pool(*, persist: bool = True) -> list[str]:
        """移出追查池中已 defeated、图鉴缺失或当前不可进入的 Boss。"""
        kept, _bosses_by_id, pool = S1ClueBureauService._eligible_pool_snapshot()
        if persist and len(kept) != len(pool):
            S1ClueBureauService._save_pool(kept, expected_boss_ids=pool)
        return kept

    @staticmethod
    def supplement_trace_pool(
        target_count: Optional[int] = None,
        updated_by_user_id: Optional[int] = None,
        *,
        dry_run: bool = False,
    ) -> dict:
        """AI 生成新 Boss 写入图鉴并加入追查池，直至达到 cap 或 AI 失败。"""
        from plugins.adventurer_s1.services.s1_clue_bureau_boss_gen_ai_service import (
            generate_bosses,
        )

        cap = S1ClueBureauService.pool_cap() if target_count is None else max(1, int(target_count))
        cap = min(cap, S1ClueBureauService.pool_cap())
        pool = S1ClueBureauService._prune_ineligible_from_pool()
        if len(pool) >= cap:
            return {
                'pool': pool,
                'added': [],
                'added_details': [],
                'count': len(pool),
                'cap': cap,
                'ai_error': None,
                'ai_error_code': None,
            }

        need = cap - len(pool)
        payloads, ai_err = generate_bosses(need)
        if ai_err and not payloads:
            return {
                'pool': pool,
                'added': [],
                'added_details': [],
                'count': len(pool),
                'cap': cap,
                'ai_error': ai_err,
                'ai_error_code': 'generation_failed',
            }
        if dry_run:
            added_details = [
                {'boss_id': None, 'dungeon': p['dungeon'], 'boss_name': p['name']}
                for p in payloads
            ]
            return {
                'pool': pool,
                'added': [],
                'added_details': added_details,
                'count': len(pool),
                'cap': cap,
                'ai_error': ai_err,
                'ai_error_code': 'generation_failed' if ai_err else None,
            }

        added_bosses, pool, publish_error = (
            S1ClueBureauCatalogService.add_bosses_from_ai_to_trace_pool(
                payloads,
                pool_cap=cap,
                updated_by_user_id=updated_by_user_id,
            )
        )
        if publish_error or len(added_bosses) != len(payloads):
            return {
                'pool': pool,
                'added': [],
                'added_details': [],
                'count': len(pool),
                'cap': cap,
                'ai_error': 'AI 候选在入库前发生名称或图鉴冲突，请重试',
                'ai_error_code': publish_error or 'catalog_conflict',
            }
        added_ids = [b.boss_id for b in added_bosses]

        added_details = [
            {'boss_id': b.boss_id, 'dungeon': b.dungeon, 'boss_name': b.name}
            for b in added_bosses
        ]
        return {
            'pool': pool,
            'added': added_ids,
            'added_details': added_details,
            'count': len(pool),
            'cap': cap,
            'ai_error': ai_err,
            'ai_error_code': 'generation_failed' if ai_err else None,
        }

    @staticmethod
    def ensure_trace_pool_bootstrapped() -> None:
        """追查池为空时不自动 bootstrap；Boss 被移出池后见 remove_from_trace_pool 自动补充。"""
        return

    @staticmethod
    def _dungeon_level_desc(dungeon_name: str) -> str | None:
        from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_DUNGEONS

        for row in S1_DUNGEONS:
            if row['name'] == dungeon_name:
                return f"Lv{row['min_level']}–{row['max_level']}"
        return None

    @staticmethod
    def _boss_to_admin_detail_dict(boss: ClueBureauBoss, *, in_pool: bool) -> dict:
        return {
            'boss_id': boss.boss_id,
            'dungeon': boss.dungeon,
            'dungeon_level_desc': S1ClueBureauService._dungeon_level_desc(boss.dungeon),
            'boss_name': boss.name,
            'background': boss.background,
            'status': boss.status,
            'global_status': boss.status,
            'active_party_id': boss.active_party_id,
            'active_expedition_log_id': boss.active_expedition_log_id,
            'clue_eligible': boss.status == BOSS_STATUS_AVAILABLE,
            'in_pool': in_pool,
            'clue_count': 4,
            'clues': [
                {
                    'index': c.index,
                    'item_name': c.item_name,
                    'description': c.description,
                    'clue_text': c.clue_text,
                }
                for c in boss.clues
            ],
        }

    @staticmethod
    def _boss_to_target_dict(boss: ClueBureauBoss) -> dict:
        return {
            'boss_id': boss.boss_id,
            'dungeon': boss.dungeon,
            'boss_name': boss.name,
            'status': boss.status,
            'global_status': boss.status,
            'clue_eligible': boss.status == BOSS_STATUS_AVAILABLE,
            'clue_count': 4,
        }

    @staticmethod
    def _boss_to_client_target_dict(boss: ClueBureauBoss, *, index: int) -> dict:
        """玩家 API：匿名追查目标，不含 Boss 名/副本/内部 id。"""
        return {
            'index': int(index),
            'label': f'追查目标 {int(index)}',
            'clue_eligible': boss.status == BOSS_STATUS_AVAILABLE,
        }

    @staticmethod
    def list_targets(*, for_client: bool = False) -> list[dict]:
        pool, bosses_by_id, _stored_pool = S1ClueBureauService._eligible_pool_snapshot()
        targets: list[dict] = []
        for idx, boss_id in enumerate(pool, start=1):
            boss = bosses_by_id.get(boss_id)
            if (
                not boss
                or boss.dungeon not in S1_ACTIVE_DUNGEON_NAMES
                or boss.status == BOSS_STATUS_DEFEATED
            ):
                continue
            if for_client:
                targets.append(S1ClueBureauService._boss_to_client_target_dict(boss, index=idx))
            else:
                targets.append(S1ClueBureauService._boss_to_target_dict(boss))
        return targets

    @staticmethod
    def list_grant_eligible_boss_ids() -> list[str]:
        return [
            t['boss_id']
            for t in S1ClueBureauService.list_targets()
            if t.get('clue_eligible') and t.get('boss_id')
        ]

    @staticmethod
    def summarize_pool(*, for_client: bool = False) -> dict:
        targets = S1ClueBureauService.list_targets(for_client=for_client)
        cap = S1ClueBureauService.pool_cap()
        summary = {
            'targets': targets,
            'pool_count': len(targets),
            'pool_cap': cap,
            'grant_eligible_count': sum(1 for t in targets if t.get('clue_eligible')),
        }
        if not for_client:
            summary['catalog_boss_count'] = len(S1ClueBureauCatalogService.list_bosses())
        return summary

    @staticmethod
    def admin_preview_supplement_trace_pool() -> Tuple[dict, Optional[str]]:
        """预览追查池状态；不在读路径调 AI（补充时 POST 再生成）。"""
        pool, bosses_by_id, _stored_pool = S1ClueBureauService._eligible_pool_snapshot()
        pool_ids = set(pool)
        cap = S1ClueBureauService.pool_cap()
        need = max(0, cap - len(pool))

        if len(pool) >= cap:
            supplement_block_reason = 'pool_full'
        else:
            supplement_block_reason = None

        pool_bosses: list[dict] = []
        catalog_not_in_pool: list[dict] = []
        for boss in bosses_by_id.values():
            in_pool = boss.boss_id in pool_ids
            detail = S1ClueBureauService._boss_to_admin_detail_dict(boss, in_pool=in_pool)
            if in_pool and boss.status != BOSS_STATUS_DEFEATED:
                pool_bosses.append(detail)
            elif not in_pool:
                catalog_not_in_pool.append(detail)

        targets = [
            S1ClueBureauService._boss_to_target_dict(bosses_by_id[boss_id])
            for boss_id in pool
        ]
        summary = {
            'targets': targets,
            'pool_count': len(targets),
            'pool_cap': cap,
            'grant_eligible_count': sum(
                1 for target in targets if target.get('clue_eligible')
            ),
            'catalog_boss_count': len(bosses_by_id),
        }
        return {
            **summary,
            'need_count': need,
            'would_add_count': need,
            'would_add': [],
            'can_supplement': need > 0,
            'supplement_block_reason': supplement_block_reason,
            'preview_ai_error': None,
            'pool_bosses': pool_bosses,
            'catalog_not_in_pool': catalog_not_in_pool,
        }, None

    @staticmethod
    def admin_supplement_trace_pool(admin_user_id: int) -> Tuple[dict, Optional[str]]:
        try:
            result = S1ClueBureauService.supplement_trace_pool(
                updated_by_user_id=admin_user_id,
            )
        except RuntimeError as exc:
            return None, str(exc)
        if result.get('ai_error') and not result.get('added'):
            return None, str(result['ai_error'])
        summary = S1ClueBureauService.summarize_pool()
        return {**result, **summary}, None

    @staticmethod
    def _stamina_per_hour(config: dict | None = None) -> int:
        cfg = config or {}
        raw = cfg.get('stamina_cost_per_hour')
        if raw is None and current_app:
            raw = current_app.config.get('ADVENTURER_S1_CLUE_BUREAU_STAMINA_PER_HOUR', DEFAULT_STAMINA_PER_HOUR)
        try:
            return max(0, int(raw if raw is not None else DEFAULT_STAMINA_PER_HOUR))
        except (TypeError, ValueError):
            return DEFAULT_STAMINA_PER_HOUR

    @staticmethod
    def _clue_drop_prob_per_hour(config: dict | None = None) -> float:
        cfg = config or {}
        raw = cfg.get('clue_drop_prob_per_hour')
        if raw is None and current_app:
            raw = current_app.config.get(
                'ADVENTURER_S1_CLUE_BUREAU_DROP_PROB_PER_HOUR',
                DEFAULT_CLUE_DROP_PROB_PER_HOUR,
            )
        try:
            prob = float(raw if raw is not None else DEFAULT_CLUE_DROP_PROB_PER_HOUR)
        except (TypeError, ValueError):
            prob = DEFAULT_CLUE_DROP_PROB_PER_HOUR
        return max(0.0, min(1.0, prob))

    @staticmethod
    def stamina_cost_for_hours(search_hours: int, config: dict | None = None) -> int:
        hours = max(0, int(search_hours or 0))
        return hours * S1ClueBureauService._stamina_per_hour(config)

    @staticmethod
    def resolve_dispatch(
        config: dict,
        extra_params: Optional[dict],
    ) -> Tuple[Optional[Tuple[int, int, int]], Optional[str]]:
        params = extra_params or {}
        max_hours = int((config or {}).get('max_search_hours') or DEFAULT_MAX_SEARCH_HOURS)
        if max_hours < MIN_SEARCH_HOURS:
            max_hours = DEFAULT_MAX_SEARCH_HOURS

        raw_hours = params.get('search_hours')
        if raw_hours is None:
            return None, '请选择出门搜寻时长'
        try:
            hours = int(raw_hours)
        except (TypeError, ValueError):
            return None, '出门时长无效'
        if hours < MIN_SEARCH_HOURS:
            return None, f'出门时长至少 {MIN_SEARCH_HOURS} 小时'
        if hours > max_hours:
            return None, f'单次出门最多 {max_hours} 小时'

        if not S1ClueBureauService._prune_ineligible_from_pool():
            return None, '追查池为空，请联系管理员补充可追查 Boss'

        stamina_cost = S1ClueBureauService.stamina_cost_for_hours(hours, config)
        per_hour = S1ClueBureauService._stamina_per_hour(config)
        if per_hour > 0 and stamina_cost < per_hour:
            return None, f'出门时长至少 {MIN_SEARCH_HOURS} 小时'

        return (hours * 60, hours, stamina_cost), None

    @staticmethod
    def pick_random_eligible_boss_id() -> Optional[str]:
        eligible = S1ClueBureauService.list_grant_eligible_boss_ids()
        if not eligible:
            return None
        return random.choice(eligible)

    @staticmethod
    def grant_random_clue(
        adv,
        *,
        max_attempts: int = 12,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
        """返回 (item_name, 玩家文案, boss_id, err, clue_label)。"""
        from plugins.adventurer_s1.services.s1_item_grant_service import (
            adventurer_holds_item_name,
            grant_raw_item,
        )
        from plugins.adventurer_s1.services.s1_item_use_condition_service import hidden_boss_clue_label

        eligible = S1ClueBureauService.list_grant_eligible_boss_ids()
        if not eligible:
            return None, None, None, None, None

        last_dup_message: Optional[str] = None
        attempts = max(1, int(max_attempts or 1))
        for _ in range(attempts):
            boss_id = random.choice(eligible)
            boss = S1ClueBureauCatalogService.get_boss(boss_id)
            if not boss or boss.status not in (BOSS_STATUS_AVAILABLE, BOSS_STATUS_DEFEATED):
                continue

            clue = random.choice(list(boss.clues))
            clue_label = hidden_boss_clue_label(clue.index)
            if adventurer_holds_item_name(adv.id, clue.item_name):
                last_dup_message = (
                    f'本次追查落在{clue_label}，但你已持有该线索，未带回新物。'
                )
                continue

            locked = S1ClueBureauCatalogService.lock_grantable_clue(
                boss.boss_id,
                clue.index,
                clue.item_name,
            )
            if locked is None:
                continue
            boss, clue = locked
            clue_label = hidden_boss_clue_label(clue.index)
            rarity = S1ClueBureauCatalogService.expected_clue_rarity(boss.boss_id)
            _, err = grant_raw_item(
                adv,
                clue.item_name,
                {
                    'type': 'clue',
                    'rarity': rarity,
                    'desc': clue.description,
                },
                source_type='clue_bureau',
                description=f'线索追查处：{clue.item_name}',
                force_destination='equipment',
            )
            if err:
                if '已持有' in str(err):
                    last_dup_message = (
                        f'本次追查落在{clue_label}，但你已持有该线索，未带回新物。'
                    )
                    continue
                return None, None, boss.boss_id, err, None
            return (
                clue.item_name,
                f'带回了{clue_label}',
                boss.boss_id,
                None,
                clue_label,
            )

        if last_dup_message:
            return None, last_dup_message, None, None, None
        return None, None, None, None, None

    @staticmethod
    def grant_clues_for_search_hours(
        adv,
        search_hours: int,
        config: dict | None = None,
    ) -> Tuple[list[str], list[str], Optional[str]]:
        hours = max(0, int(search_hours or 0))
        if hours <= 0:
            return [], [], None

        prob = S1ClueBureauService._clue_drop_prob_per_hour(config)
        granted: list[str] = []
        granted_labels: list[str] = []
        messages: list[str] = []
        for _ in range(hours):
            if prob <= 0 or random.random() >= prob:
                continue
            item_name, message, _, err, clue_label = S1ClueBureauService.grant_random_clue(adv)
            if err:
                return granted_labels, messages, err
            if message:
                messages.append(message)
            if item_name and clue_label:
                granted.append(item_name)
                granted_labels.append(clue_label)

        if not granted_labels and not messages:
            messages.append(f'追查 {hours} 小时，未获得新线索。')
        return granted_labels, messages, None

    @staticmethod
    def get_max_search_hours(config: dict | None = None) -> int:
        cfg = config or {}
        max_hours = int((cfg or {}).get('max_search_hours') or DEFAULT_MAX_SEARCH_HOURS)
        return max(MIN_SEARCH_HOURS, max_hours)
