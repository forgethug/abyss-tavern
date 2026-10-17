"""深渊酒馆 S1 悬赏布告栏委托生成 AI。

每小时由 :class:`S1CommissionService` 调用一次，按各 tier 缺口**批量**生成委托（单次 DS 调用）。
仿 ``s1_building_ai_service._call_building_ai`` 的 requests→DeepSeek→JSON 解析→markdown 兜底。

生成的委托返回给 service 持久化；本模块只负责"调 AI + 校验 + 规整"，不写库。
"""
import json
import logging
import random
from typing import Optional
import requests
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import AIProfileAssignmentError, post_s1_ai_request, resolve_s1_ai_profile_snapshot
from plugins.adventurer_s1.services.s1_ai_json_response_service import S1AiResponseContractResult, parse_s1_ai_http_json_response
from flask import current_app
from db_repository import db_manager
from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_ACTIVE_DUNGEONS, S1_DUNGEON_RANK_MAP, get_adventurer_real_rank_s1
from plugins.adventurer_s1.services.adventurer_s1_prompt_service import _S1_WORLD_GEOGRAPHY_ADDENDUM, _S1_PRICE_ADDENDUM
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_integer
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
from plugins.adventurer_s1.s1_item_types import S1_COMMISSION_REWARD_ITEM_TYPES, format_item_types_slash, normalize_item_type
logger = logging.getLogger(__name__)
TIER_LABELS = {'junior': '初级', 'intermediate': '中级', 'advanced': '高级'}
TASK_TYPES = ('gather', 'rescue', 'escort', 'combat', 'subjugation')

class _CommissionAIResult(dict):
    """Carry one response's usage until the full consumer contract is known."""

    def __init__(self, value: dict, *, usage: dict | None, model: str):
        super().__init__(value)
        self.usage = usage
        self.model = model

def _coerce_int(value, default: int) -> int:
    normalized, _ = normalize_ascii_integer(value)
    return normalized if type(normalized) is int else default

def _load_existing_catalog_names(candidate_names: set[str]) -> set[str]:
    """Existing catalog names are a deny-list for AI-generated commission rewards."""
    from plugins.adventurer_s1.models import AdventurerS1Item
    if not candidate_names:
        return set()
    rows = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(candidate_names)).with_entities(AdventurerS1Item.name).all()
    return {str(r.name or '').strip() for r in rows if str(r.name or '').strip()}

def _pick_random_dungeons(count: int, dungeon_names: list[str]) -> list[str]:
    """从当前可用副本池中等概率随机预选目标（高级委托由后端指定，不交给 AI 选图）。"""
    if count <= 0:
        return []
    names = list(dungeon_names)
    if not names:
        return []
    if count <= len(names):
        return random.sample(names, count)
    return [random.choice(names) for _ in range(count)]

def _available_dungeons_for_current_strength() -> list[str]:
    """限制委托目标副本不高于当前可派遣玩家冒险者能刷出的最高 rank。"""
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

    def int_cfg(key: str, default: int) -> int:
        try:
            return int(current_app.config.get(key, default))
        except (TypeError, ValueError):
            return default
    stamina_max = int_cfg('ADVENTURER_S1_STAMINA_MAX', 100)
    stamina_min = int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
    recovery_rate = int_cfg('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1)
    raw = AdventurerS1.query.filter(AdventurerS1.status.in_(AdventurerS1Service.FREE_STATUSES), AdventurerS1.is_player_created == True).all()
    from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
    from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
    seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id for adv in raw])
    eligible = [adv for adv in raw if get_live_stamina(adv, max_stamina=stamina_max, recovery_per_minute=recovery_rate, tavern_seated=adv.id in seated_ids) > stamina_min]
    if not eligible:
        max_rank = 0
    else:
        max_rank = max((get_adventurer_real_rank_s1(adv) for adv in eligible))
    available = [d['name'] for d in S1_ACTIVE_DUNGEONS if S1_DUNGEON_RANK_MAP.get(d['name'], 0) <= max_rank]
    from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
    closed_dungeons = S1WorldLocationStateService.closed_dungeon_names(available)
    return [name for name in available if name not in closed_dungeons]

def _build_system_prompt() -> str:
    return '\n\n'.join(['你是「深渊酒馆」悬赏布告栏的委托发布官。根据世界设定与现有副本，批量生成可供冒险者接取的悬赏委托。', _S1_WORLD_GEOGRAPHY_ADDENDUM, _S1_PRICE_ADDENDUM, f'【奖励物品原创规则】\n- reward_items 不是从数据库图鉴中选择；如需物品奖励，必须由你按委托剧情原创生成。\n- 不要复用玩家可能已经拥有或锻造过的具体物品名；不要写成某个已有独特装备、宠物、伙伴或命名神器。\n- 每个 reward_items 条目必须包含 name、type、rarity、desc、quantity。type 取 {format_item_types_slash(S1_COMMISSION_REWARD_ITEM_TYPES)} 之一；rarity 取 common/rare/epic/legendary 之一；desc 用 1~2 句中文描述外观、来源或用途。\n- 奖励物品只允许 0~2 项，quantity 1~3；不确定时只给金币。', '【委托发布规则】\n- 委托分三档：初级(junior)、中级(intermediate)、高级(advanced)。\n- task_type 可选：gather(采集)、rescue(救援)、escort(护送)、combat(讨伐杂兵)、subjugation(高级讨伐Boss)。\n- 高级委托**固定**为 subjugation 讨伐任务，必须给出 boss_name(Boss名) 与 boss_background(2~4句背景)，风格贴合目标副本。\n- 初级/中级委托不要用 subjugation，也不要给 boss 字段。\n- 初级/中级委托的 target_dungeons 从下方「可用副本」中选取，难度与档位大致匹配（初级偏低档、中级偏中档）。\n- 高级委托的 target_dungeons 由系统在 user 消息中**预先指定**，你必须原样写入 JSON，不得更改；只需为指定副本创作 title/background/requirement_text/boss_name/boss_background。\n- requirement_text：一句话写清"达成条件"（如"在迷雾沼泽找回失踪商队的货箱"），将作为探险触发时的任务目标交给探险AI判定。\n- required_count：若 requirement_text 是"消灭/收集/护送 N 个/只"等**数量目标**，必须额外给 required_count=该数量（整数，1~20，建议 3~10）；护送、讨伐单一Boss、找回单件信物等单一目标给 1。数量进度可跨多次探险累积。\n- background：2~4 句委托背景/来由，自然融入世界观。\n- title：6~16 字的醒目悬赏标题。\n- reward_gold：按档位金币区间取整数（见下方区间）。可附 reward_items（原创物品，0~2 件，数量 1~3），物品须贴合剧情且与金币合计大致符合档位价值；每件必须写 name/type/rarity/desc/quantity。不确定就只给金币。\n- 只输出 JSON，禁止多余解释。', '【输出格式】严格输出 JSON 对象：\n{"commissions":[{"tier":"junior|intermediate|advanced","task_type":"gather|rescue|escort|combat|subjugation","title":"...","background":"...","requirement_text":"...","required_count":1,"target_dungeons":["..."],"reward_gold":120,"reward_items":[{"name":"...","type":"consumable","rarity":"common","desc":"...","quantity":1}],"boss_name":"高级讨伐必填","boss_background":"高级讨伐必填"}]}'])

def _build_user_prompt(missing_by_tier: dict, reward_gold_cfg: dict, dungeon_names: list[str], advanced_assigned_dungeons: list[str] | None=None) -> str:
    item_line = '请按委托背景原创生成；不要从物品图鉴或既有玩家物品中挑选。'
    tier_reqs = []
    for tier in ('junior', 'intermediate', 'advanced'):
        n = int(missing_by_tier.get(tier, 0) or 0)
        if n <= 0:
            continue
        lo, hi = reward_gold_cfg.get(tier, [0, 0])
        extra = '（固定 subjugation 讨伐，target_dungeons 使用下方预选副本，必须含 boss_name/boss_background）' if tier == 'advanced' else ''
        tier_reqs.append(f'- {TIER_LABELS[tier]}({tier})：生成 {n} 个，金币区间 {lo}~{hi}{extra}')
    lines = ['【可用副本】（初级/中级 target_dungeons 只能从这里精确取名）：', '、'.join(dungeon_names), '', '【奖励物品】（reward_items 不提供候选清单）：', item_line, '']
    assigned = list(advanced_assigned_dungeons or [])
    if assigned:
        lines.extend(['【高级委托预选副本】（每条高级委托 target_dungeons 必须且只能使用该副本名，不得更改）：', *[f'- 高级委托 #{i + 1}：{name}' for i, name in enumerate(assigned)], ''])
    lines.extend(['【本次需要生成的委托】：', *tier_reqs, '', '请生成上述全部委托，按【输出格式】只返回 JSON。'])
    return '\n'.join(lines)

def _call_deepseek(system_prompt: str, user_prompt: str, *, response_validator=None) -> Optional[dict]:
    try:
        route = resolve_s1_ai_profile_snapshot('commission_generation')
    except AIProfileAssignmentError as exc:
        logger.warning('委托生成 AI 用途路由不可用: %s', exc)
        return None
    api_key = route.api_key
    api_base = route.api_base
    model = route.model
    timeout = _coerce_int(current_app.config.get('ADVENTURER_S1_COMMISSION_AI_TIMEOUT_SECONDS', 600), 600)
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    usage = None
    usage_deferred = False
    request_metadata: dict[str, str] = {}
    request_exception: BaseException | None = None

    def validate_json_object_response(decoded) -> S1AiResponseContractResult:
        if decoded.json_result.ok and isinstance(decoded.json_result.data, dict):
            return S1AiResponseContractResult.accept(decoded.json_result.data)
        return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'result_not_object', failure_stage='response_parse')
    try:
        db_manager.remove()
        resp = post_s1_ai_request(f'{api_base}/chat/completions', feature='commission_generation', resolved_profile=route, request_metadata_out=request_metadata, headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}], 'temperature': 0.7}, timeout=timeout, response_validator=response_validator or validate_json_object_response)
        decoded = parse_s1_ai_http_json_response(resp)
        usage = decoded.http_result.usage
        if not decoded.json_result.ok:
            raise ValueError(f'委托生成响应不可用: {decoded.json_result.error_code}')
        parsed = decoded.json_result.data
        if isinstance(parsed, dict) and isinstance(parsed.get('commissions'), list):
            usage_deferred = True
            return _CommissionAIResult(parsed, usage=usage, model=request_metadata.get('model') or model)
        return parsed if isinstance(parsed, dict) else None
    except Exception as e:
        request_exception = e
        logger.warning('委托生成 AI 调用/解析失败: %s', e)
        return None
    finally:
        if (usage or request_exception is not None) and (not usage_deferred):
            record_token_usage(call_type='commission_generation', usage=usage, model=request_metadata.get('model') or model, attempt_number=1, is_success=False, failure_stage='http_or_response', validation_error_code=f'http_or_response.{type(request_exception).__name__}' if request_exception is not None else 'commission_generation_invalid', record_without_usage=request_exception is not None, request_metadata=request_metadata, request_exception=request_exception)

def _commission_ai_text(value: object, *, maximum: int | None=None) -> tuple[str, bool]:
    if not isinstance(value, str):
        return ('', False)
    text = value.strip()
    if not text:
        return ('', False)
    if maximum is not None and len(text) > maximum:
        return (text[:maximum], False)
    return (text, True)

def _validate_one_with_status(raw: dict, tier: str, reward_gold_cfg: dict, existing_item_names: set[str], dungeon_names: list[str], forced_dungeon: str | None=None, deterministic_fallback_dungeon: str | None=None) -> tuple[Optional[dict], bool]:
    """规整单个委托，并区分安全结果与完整 AI 输出契约状态。"""
    if not isinstance(raw, dict):
        return (None, False)
    title, title_valid = _commission_ai_text(raw.get('title'), maximum=60)
    background, background_valid = _commission_ai_text(raw.get('background'))
    requirement_text, requirement_valid = _commission_ai_text(raw.get('requirement_text'))
    if not title or not background or (not requirement_text):
        return (None, False)
    contract_valid = title_valid and 6 <= len(title) <= 16 and background_valid and requirement_valid
    raw_dungeons = raw.get('target_dungeons')
    if tier == 'advanced' and forced_dungeon:
        dungeons = [forced_dungeon]
        contract_valid = contract_valid and raw_dungeons == [forced_dungeon]
    else:
        dungeon_name_set = set(dungeon_names)
        dungeons = []
        if not isinstance(raw_dungeons, list):
            contract_valid = False
            raw_dungeons = []
        for dungeon in raw_dungeons:
            if not isinstance(dungeon, str) or dungeon not in dungeon_name_set:
                contract_valid = False
                continue
            if dungeon not in dungeons:
                dungeons.append(dungeon)
        if not dungeons:
            if not dungeon_names:
                return (None, False)
            dungeons = [deterministic_fallback_dungeon if deterministic_fallback_dungeon in dungeon_name_set else random.choice(dungeon_names)]
            contract_valid = False
    raw_task_type = raw.get('task_type')
    if tier == 'advanced':
        task_type = 'subjugation'
        contract_valid = contract_valid and raw_task_type == 'subjugation'
    else:
        task_type = raw_task_type
        if task_type not in TASK_TYPES or task_type == 'subjugation':
            task_type = 'combat'
            contract_valid = False
    lo, hi = reward_gold_cfg.get(tier, [0, 0])
    lo = int(lo)
    hi = int(hi)
    normalized_reward_gold, _ = normalize_ascii_integer(raw.get('reward_gold'), minimum=lo, maximum=hi)
    reward_gold_valid = type(normalized_reward_gold) is int and lo <= normalized_reward_gold <= hi
    if reward_gold_valid:
        reward_gold = normalized_reward_gold
    else:
        raw_reward_gold = raw.get('reward_gold')
        reward_gold = max(lo, min(hi, raw_reward_gold)) if type(raw_reward_gold) is int else lo
        contract_valid = False
    normalized_required_count, _ = normalize_ascii_integer(raw.get('required_count'), minimum=1, maximum=20)
    required_count_valid = type(normalized_required_count) is int and 1 <= normalized_required_count <= 20
    required_count = normalized_required_count if required_count_valid else 1
    contract_valid = contract_valid and reward_gold_valid and required_count_valid
    allowed_rarities = {'common', 'rare', 'epic', 'legendary'}
    reward_items = []
    raw_reward_items = raw.get('reward_items')
    if raw_reward_items is None and 'reward_items' not in raw:
        raw_reward_items = []
    elif not isinstance(raw_reward_items, list):
        raw_reward_items = []
        contract_valid = False
    elif len(raw_reward_items) > 2:
        contract_valid = False
    for entry in raw_reward_items[:2]:
        if not isinstance(entry, dict):
            contract_valid = False
            continue
        name, name_valid = _commission_ai_text(entry.get('name'), maximum=80)
        if not name or not name_valid or name in existing_item_names:
            contract_valid = False
            continue
        raw_item_type = entry.get('type')
        item_type = normalize_item_type(raw_item_type if isinstance(raw_item_type, str) else '', allowed=S1_COMMISSION_REWARD_ITEM_TYPES)
        item_type_valid = isinstance(raw_item_type, str) and raw_item_type.strip() == item_type and (item_type in S1_COMMISSION_REWARD_ITEM_TYPES)
        raw_rarity = entry.get('rarity')
        rarity = raw_rarity.strip() if isinstance(raw_rarity, str) else 'common'
        rarity_valid = rarity in allowed_rarities
        if rarity not in allowed_rarities:
            rarity = 'common'
        raw_desc = entry.get('desc') if 'desc' in entry else entry.get('description')
        desc, desc_valid = _commission_ai_text(raw_desc)
        if not desc:
            contract_valid = False
            continue
        qty, _ = normalize_ascii_integer(entry.get('quantity'), minimum=1, maximum=3)
        qty_valid = type(qty) is int and 1 <= qty <= 3
        if not qty_valid:
            qty = 1
        contract_valid = contract_valid and name_valid and item_type_valid and rarity_valid and desc_valid and qty_valid
        reward_items.append({'name': name, 'type': item_type, 'rarity': rarity, 'desc': desc, 'quantity': qty})
        existing_item_names.add(name)
    out = {'tier': tier, 'task_type': task_type, 'title': title, 'background': background, 'requirement_text': requirement_text, 'target_dungeons': dungeons, 'required_count': required_count, 'reward_gold': reward_gold, 'reward_items': reward_items}
    if tier == 'advanced':
        boss_name, boss_name_valid = _commission_ai_text(raw.get('boss_name'), maximum=100)
        boss_background, boss_background_valid = _commission_ai_text(raw.get('boss_background'))
        if not boss_name:
            boss_name = f'{dungeons[0]}的隐秘霸主'
        if not boss_background:
            boss_background = f'盘踞{dungeons[0]}深处的强大存在，至今无人将其讨伐。'
        contract_valid = contract_valid and boss_name_valid and boss_background_valid
        out['boss_name'] = boss_name
        out['boss_background'] = boss_background
    return (out, contract_valid)

def _validate_commission_generation_contract(raw: object, *, missing_by_tier: dict, reward_gold_cfg: dict, dungeon_names: list[str], advanced_assigned: list[str]) -> str | None:
    """Validate the frozen AI role contract without querying live authority."""
    if not isinstance(raw, dict):
        return 'commission_generation.result_not_object'
    raw_commissions = raw.get('commissions')
    if not isinstance(raw_commissions, list):
        return 'commission_generation.commissions_list_invalid'
    requested_total = sum(missing_by_tier.values())
    if len(raw_commissions) != requested_total:
        return 'commission_generation.count_mismatch'
    remaining = dict(missing_by_tier)
    advanced_dungeon_queue = list(advanced_assigned)
    response_item_names: set[str] = set()
    deterministic_fallback = dungeon_names[0] if dungeon_names else None
    for raw_commission in raw_commissions:
        if not isinstance(raw_commission, dict):
            return 'commission_generation.row_not_object'
        tier = raw_commission.get('tier')
        if tier not in remaining or remaining[tier] <= 0:
            return 'commission_generation.tier_scope_invalid'
        forced_dungeon = None
        if tier == 'advanced' and advanced_dungeon_queue:
            forced_dungeon = advanced_dungeon_queue.pop(0)
        normalized, contract_valid = _validate_one_with_status(raw_commission, tier, reward_gold_cfg, response_item_names, dungeon_names, forced_dungeon=forced_dungeon, deterministic_fallback_dungeon=deterministic_fallback)
        if normalized is None or not contract_valid:
            return 'commission_generation.row_contract_invalid'
        remaining[tier] -= 1
    if any(remaining.values()):
        return 'commission_generation.tier_count_incomplete'
    return None

def _validate_one(raw: dict, tier: str, reward_gold_cfg: dict, existing_item_names: set[str], dungeon_names: list[str], forced_dungeon: str | None=None) -> Optional[dict]:
    validated, _ = _validate_one_with_status(raw, tier, reward_gold_cfg, existing_item_names, dungeon_names, forced_dungeon=forced_dungeon)
    return validated

def generate_commissions(missing_by_tier: dict) -> list[dict]:
    """按 tier 缺口批量生成委托（已校验），返回 dict 列表。失败返回空列表。"""
    missing_by_tier = {k: int(v or 0) for k, v in (missing_by_tier or {}).items() if int(v or 0) > 0}
    if not missing_by_tier:
        return []
    reward_gold_cfg = current_app.config.get('ADVENTURER_S1_COMMISSION_REWARD_GOLD', {})
    dungeon_names = _available_dungeons_for_current_strength()
    if not dungeon_names:
        logger.warning('S1 委托生成跳过：当前没有可正常进入的普通副本')
        return []
    advanced_count = int(missing_by_tier.get('advanced', 0) or 0)
    advanced_assigned = _pick_random_dungeons(advanced_count, dungeon_names)
    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(missing_by_tier, reward_gold_cfg, dungeon_names, advanced_assigned)

    def validate_commission_generation_response(decoded) -> S1AiResponseContractResult:
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'invalid_json', failure_stage='response_parse')
        contract_error = _validate_commission_generation_contract(decoded.json_result.data, missing_by_tier=missing_by_tier, reward_gold_cfg=reward_gold_cfg, dungeon_names=dungeon_names, advanced_assigned=advanced_assigned)
        if contract_error is None:
            return S1AiResponseContractResult.accept()
        return S1AiResponseContractResult.reject(contract_error, failure_stage='contract_validation')
    parsed = _call_deepseek(system_prompt, user_prompt, response_validator=validate_commission_generation_response)
    deferred_result = parsed if isinstance(parsed, _CommissionAIResult) else None
    contract_success = False
    try:
        if not parsed or not isinstance(parsed.get('commissions'), list):
            return []
        raw_commissions = parsed['commissions']
        reward_candidate_names: set[str] = set()
        for raw in raw_commissions:
            if not isinstance(raw, dict):
                continue
            raw_reward_items = raw.get('reward_items')
            if not isinstance(raw_reward_items, list):
                continue
            for entry in raw_reward_items:
                if isinstance(entry, dict) and isinstance(entry.get('name'), str):
                    name = entry['name'].strip()
                    if name:
                        reward_candidate_names.add(name[:80])
        existing_item_names = _load_existing_catalog_names(reward_candidate_names)
        remaining = dict(missing_by_tier)
        advanced_dungeon_queue = list(advanced_assigned)
        results: list[dict] = []
        rows_contract_valid = True
        for raw in raw_commissions:
            if not isinstance(raw, dict):
                rows_contract_valid = False
                continue
            tier = raw.get('tier')
            if tier not in remaining or remaining[tier] <= 0:
                rows_contract_valid = False
                continue
            forced_dungeon = None
            if tier == 'advanced' and advanced_dungeon_queue:
                forced_dungeon = advanced_dungeon_queue.pop(0)
            validated, row_contract_valid = _validate_one_with_status(raw, tier, reward_gold_cfg, existing_item_names, dungeon_names, forced_dungeon=forced_dungeon)
            rows_contract_valid = rows_contract_valid and row_contract_valid
            if not validated:
                continue
            results.append(validated)
            remaining[tier] -= 1
        requested_total = sum(missing_by_tier.values())
        contract_success = len(raw_commissions) == requested_total and len(results) == requested_total and all((count == 0 for count in remaining.values())) and rows_contract_valid
        return results
    finally:
        if deferred_result is not None and deferred_result.usage:
            record_token_usage(call_type='commission_generation', usage=deferred_result.usage, model=deferred_result.model, attempt_number=1, is_success=contract_success)
