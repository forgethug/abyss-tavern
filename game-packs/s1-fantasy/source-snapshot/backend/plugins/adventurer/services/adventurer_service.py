"""
adventurer_service — 外观模块

仅保留需要直接访问 SQLAlchemy/db 的三个函数：
  _build_non_empty_item_snapshot_filter / sync_adventurer_item_snapshots / resolve_delivery_target

其余所有符号从各子模块导入后直接重导出，保持现有 import 路径不变。
"""

__all__ = [
    # 本模块自身定义
    '_build_non_empty_item_snapshot_filter',
    'sync_adventurer_item_snapshots',
    'resolve_delivery_target',
    # adventurer_config_service
    'BASIC_CONSUMABLES', 'CLASSES', 'DEFAULT_SKILLS', 'DUNGEONS', 'DUNGEON_RANK_MAP',
    'DUNGEON_UNIQUE_TIERS', 'FEMALE_NAMES', 'INITIAL_EQUIPMENT_SEED', 'LAST_NAMES',
    'MALE_NAMES', 'PERSONALITIES', 'RARITY_AI_RESTOCK_THRESHOLD', 'RARITY_PRICE_RANGES',
    'RARITY_STOCK_LIMITS', '_build_ai_output_preview', '_build_ai_response_summary',
    '_clean_compact_text', '_coerce_config_bool', '_coerce_float', '_coerce_int',
    '_extract_ai_message_text', '_extract_json_object_from_text',
    '_get_adventurer_ai_request_timeout', '_random_price_by_rarity',
    '_strip_markdown_code_fence', '_to_clean_string_list',
    'calculate_expedition_exp_gain', 'get_adventurer_exp_required',
    'get_auto_party_settings', 'get_dungeon_level_range', 'get_max_adventurers',
    'set_auto_party_settings', 'set_max_adventurers',
    # adventurer_shop_service
    '_build_visible_shop_items', '_do_ai_restock_items', '_get_shop_buy_probability',
    'ai_restock_items', 'clean_polluted_history_story', 'initialize_shop_items',
    'restock_basic_consumables',
    # adventurer_prompt_service
    '_ask_deepseek_prepare_expedition_context', '_build_expedition_danger_assessment',
    '_build_expedition_ai_system_prompt', '_build_expedition_ai_user_prompt',
    '_build_in_progress_expedition_log_payload', '_build_expedition_member_prompt_inputs',
    '_build_expedition_member_runtime_snapshots', '_build_expedition_departure_msg',
    '_build_member_prompt_context', '_build_pre_expedition_log_events',
    '_build_pre_expedition_storage_events', '_build_prepare_ai_context_debug_summary',
    '_build_prepare_ai_model_comparison_summary', '_build_prepare_ai_single_model_log',
    '_build_special_event_prompt_context', '_compose_member_info_for_expedition',
    '_extract_cached_prepared_member_context', '_flatten_shopping_events',
    '_merge_post_prepare_purchased_items', '_normalize_cached_shopping_purchased_items',
    '_normalize_consumed_equipments_payload', '_normalize_used_equipments_payload',
    '_persist_prepare_stage_checkpoint', '_run_post_prepare_shopping_stage',
    '_sanitize_context_preparation_result', '_build_generating_log_result_summary',
    # adventurer_parser_service
    '_apply_equipment_damage_updates', '_apply_member_hp_update',
    '_apply_pre_expedition_trait_merges', '_dedupe_duplicate_new_equipments',
    '_detect_implausible_extreme_zero_death_result', '_is_member_update_dead',
    '_normalize_equipment_damage_stage', '_normalize_equipment_damage_updates_payload',
    '_normalize_expedition_result_with_current_state', '_normalize_member_update_payload',
    '_normalize_member_updates_with_current_state', '_normalize_new_equipment_payload',
    '_normalize_stored_to_warehouse_payload', '_extract_stored_to_warehouse_names',
    '_parse_story_events', '_split_structured_sections',
    'parse_deepseek_expedition_response',
    # adventurer_party_service
    'ensure_min_adventurers', 'form_parties', 'generate_random_adventurers',
    # adventurer_log_service
    '_calculate_expedition_progress_step', '_normalize_member_new_traits_payload',
    'build_public_chat_message_payload', 'build_public_expedition_log_detail',
    'build_public_expedition_log_summary', 'emit_adventurer_update',
    'emit_tavern_precise_updates', 'get_expedition_log_detail',
    'get_expedition_log_details_batch', 'list_expedition_logs',
    # adventurer_expedition_runner
    '_do_ask_deepseek_expedition_result', 'ask_deepseek_expedition_result',
    'auto_game_loop', 'check_icu_recovery', 'check_normal_recovery',
    'progress_expeditions', 'send_parties_to_expedition',
]

from sqlalchemy import Text, cast, or_
from sqlalchemy.orm.attributes import flag_modified

from db_repository import db_manager
from plugins.adventurer.models import Adventurer, AdventurerItem

# ── 配置 / 常量 / 辅助工具 ───────────────────────────────────────────────────
from plugins.adventurer.services.adventurer_config_service import (
    BASIC_CONSUMABLES,
    CLASSES,
    DEFAULT_SKILLS,
    DUNGEONS,
    DUNGEON_RANK_MAP,
    DUNGEON_UNIQUE_TIERS,
    FEMALE_NAMES,
    INITIAL_EQUIPMENT_SEED,
    LAST_NAMES,
    MALE_NAMES,
    PERSONALITIES,
    RARITY_AI_RESTOCK_THRESHOLD,
    RARITY_PRICE_RANGES,
    RARITY_STOCK_LIMITS,
    _build_ai_output_preview,
    _build_ai_response_summary,
    _clean_compact_text,
    _coerce_config_bool,
    _coerce_float,
    _coerce_int,
    _extract_ai_message_text,
    _extract_json_object_from_text,
    _get_adventurer_ai_request_timeout,
    _random_price_by_rarity,
    _strip_markdown_code_fence,
    _to_clean_string_list,
    calculate_expedition_exp_gain,
    get_adventurer_exp_required,
    get_auto_party_settings,
    get_dungeon_level_range,
    get_max_adventurers,
    set_auto_party_settings,
    set_max_adventurers,
)

# ── 商店系统 ──────────────────────────────────────────────────────────────────
from plugins.adventurer.services.adventurer_shop_service import (
    _build_visible_shop_items,
    _do_ai_restock_items,
    _get_shop_buy_probability,
    ai_restock_items,
    clean_polluted_history_story,
    initialize_shop_items,
    restock_basic_consumables,
)

# ── Prompt 构建 ───────────────────────────────────────────────────────────────
from plugins.adventurer.services.adventurer_prompt_service import (
    _ask_deepseek_prepare_expedition_context,
    _build_expedition_danger_assessment,
    _build_expedition_ai_system_prompt,
    _build_expedition_ai_user_prompt,
    _build_in_progress_expedition_log_payload,
    _build_expedition_member_prompt_inputs,
    _build_expedition_member_runtime_snapshots,
    _build_expedition_departure_msg,
    _build_member_prompt_context,
    _build_pre_expedition_log_events,
    _build_pre_expedition_storage_events,
    _build_prepare_ai_context_debug_summary,
    _build_prepare_ai_model_comparison_summary,
    _build_prepare_ai_single_model_log,
    _build_special_event_prompt_context,
    _compose_member_info_for_expedition,
    _extract_cached_prepared_member_context,
    _flatten_shopping_events,
    _merge_post_prepare_purchased_items,
    _normalize_cached_shopping_purchased_items,
    _normalize_consumed_equipments_payload,
    _normalize_used_equipments_payload,
    _persist_prepare_stage_checkpoint,
    _run_post_prepare_shopping_stage,
    _sanitize_context_preparation_result,
    _build_generating_log_result_summary,
)

# ── AI 结果解析 ───────────────────────────────────────────────────────────────
from plugins.adventurer.services.adventurer_parser_service import (
    _apply_equipment_damage_updates,
    _apply_member_hp_update,
    _apply_pre_expedition_trait_merges,
    _dedupe_duplicate_new_equipments,
    _detect_implausible_extreme_zero_death_result,
    _is_member_update_dead,
    _normalize_equipment_damage_stage,
    _normalize_equipment_damage_updates_payload,
    _normalize_expedition_result_with_current_state,
    _normalize_member_update_payload,
    _normalize_member_updates_with_current_state,
    _normalize_new_equipment_payload,
    _normalize_stored_to_warehouse_payload,
    _extract_stored_to_warehouse_names,
    _parse_story_events,
    _split_structured_sections,
    parse_deepseek_expedition_response,
)

# ── 组队与冒险者生成 ──────────────────────────────────────────────────────────
from plugins.adventurer.services.adventurer_party_service import (
    ensure_min_adventurers,
    form_parties,
    generate_random_adventurers,
)

# ── 日志 + Socket 推送 ────────────────────────────────────────────────────────
from plugins.adventurer.services.adventurer_log_service import (
    _calculate_expedition_progress_step,
    _normalize_member_new_traits_payload,
    build_public_chat_message_payload,
    build_public_expedition_log_detail,
    build_public_expedition_log_summary,
    emit_adventurer_update,
    emit_tavern_precise_updates,
    get_expedition_log_detail,
    get_expedition_log_details_batch,
    list_expedition_logs,
)

# ── 探险主流程 ────────────────────────────────────────────────────────────────
from plugins.adventurer.services.adventurer_expedition_runner import (
    _do_ask_deepseek_expedition_result,
    ask_deepseek_expedition_result,
    auto_game_loop,
    check_icu_recovery,
    check_normal_recovery,
    progress_expeditions,
    send_parties_to_expedition,
)


# ── 仍保留在本模块：直接使用 SQLAlchemy 表达式的底层函数 ──────────────────────

def _build_non_empty_item_snapshot_filter():
    """构建存在装备或收藏室物品快照的过滤条件，兼容 PostgreSQL JSON 字段比较。"""
    return or_(
        cast(Adventurer.equipment, Text) != '{}',
        cast(Adventurer.warehouse, Text) != '[]'
    )


def sync_adventurer_item_snapshots(item: AdventurerItem, old_name: str | None = None) -> tuple[dict, str | None]:
    """同步所有冒险者身上和收藏室中的物品快照信息。"""
    try:
        target_names = {item.name}
        if old_name and old_name != item.name:
            target_names.add(old_name)

        updated_adventurers = 0
        adventurers = db_manager.query(Adventurer).filter(
            _build_non_empty_item_snapshot_filter()
        ).all()

        for adv in adventurers:
            equipment_changed = False
            warehouse_changed = False

            current_equipment = adv.equipment or {}
            if current_equipment:
                synced_equipment = {}
                for eq_name, eq_data in current_equipment.items():
                    if eq_name in target_names:
                        new_eq_data = dict(eq_data) if isinstance(eq_data, dict) else {}
                        new_eq_data['type'] = item.type
                        new_eq_data['desc'] = item.description
                        new_eq_data['rarity'] = item.rarity
                        synced_equipment[item.name] = new_eq_data
                        if eq_name != item.name or new_eq_data != eq_data:
                            equipment_changed = True
                    else:
                        synced_equipment[eq_name] = dict(eq_data) if isinstance(eq_data, dict) else eq_data

                if equipment_changed:
                    adv.equipment = synced_equipment
                    flag_modified(adv, 'equipment')

            current_warehouse = adv.warehouse or []
            if current_warehouse:
                synced_warehouse = []
                for warehouse_item in current_warehouse:
                    if isinstance(warehouse_item, dict) and warehouse_item.get('name') in target_names:
                        new_warehouse_item = dict(warehouse_item)
                        new_warehouse_item['name'] = item.name
                        new_warehouse_item['type'] = item.type
                        new_warehouse_item['desc'] = item.description
                        new_warehouse_item['rarity'] = item.rarity
                        synced_warehouse.append(new_warehouse_item)
                        if new_warehouse_item != warehouse_item:
                            warehouse_changed = True
                    else:
                        synced_warehouse.append(dict(warehouse_item) if isinstance(warehouse_item, dict) else warehouse_item)

                if warehouse_changed:
                    adv.warehouse = synced_warehouse
                    flag_modified(adv, 'warehouse')

            if equipment_changed or warehouse_changed:
                updated_adventurers += 1

        return {'updated_adventurers': updated_adventurers}, None
    except Exception as e:
        return None, str(e)


def resolve_delivery_target(target_id_str: str, lock_for_update: bool = False) -> tuple[dict, str | None]:
    """解析管理员发送物品的目标冒险者。"""
    try:
        normalized_target = str(target_id_str or '').strip().lower()
        if not normalized_target:
            return None, '请提供玩家ID或冒险者ID'

        def _apply_lock(query):
            return query.with_for_update() if lock_for_update else query

        def _find_alive_owned_adventurer(owner_id: int):
            query = db_manager.query(Adventurer).filter(
                Adventurer.owner_id == owner_id,
                Adventurer.status != 'dead'
            )
            return _apply_lock(query).first()

        resolved_by = ''
        adv = None

        if normalized_target.startswith('a'):
            adv_id = int(normalized_target[1:])
            query = db_manager.query(Adventurer).filter(Adventurer.id == adv_id)
            adv = _apply_lock(query).first()
            resolved_by = 'adventurer_id'
        elif normalized_target.startswith('u'):
            user_id = int(normalized_target[1:])
            adv = _find_alive_owned_adventurer(user_id)
            resolved_by = 'user_id'
        else:
            target_id = int(normalized_target)
            adv = _find_alive_owned_adventurer(target_id)
            resolved_by = 'user_id_priority'
            if not adv:
                query = db_manager.query(Adventurer).filter(Adventurer.id == target_id)
                adv = _apply_lock(query).first()
                resolved_by = 'adventurer_id_fallback'

        if not adv:
            return None, '找不到对应的冒险者或该玩家未绑定存活冒险者'

        return {
            'adventurer': adv,
            'adventurer_id': adv.id,
            'adventurer_name': adv.name,
            'owner_id': adv.owner_id,
            'status': adv.status,
            'resolved_by': resolved_by
        }, None
    except ValueError:
        return None, 'ID必须是数字，可加前缀u(玩家)或a(冒险者)'
    except Exception as e:
        return None, str(e)
