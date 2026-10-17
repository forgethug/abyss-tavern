"""物品使用条件：探险 AI 组装成员块时按目标副本过滤随身装备（条件本身不进 prompt）。"""
from __future__ import annotations

import re
from typing import Any
DEFAULT_USES_LEFT_KEY = 'default_uses_left'

EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD = '_expedition_ai_hidden'
def normalize_use_condition(raw: Any) -> dict | None:
    if not raw or not isinstance(raw, dict):
        return None
    cleaned: dict[str, Any] = {}
    for k, v in raw.items():
        key = str(k)
        if v is None:
            continue
        if key == DEFAULT_USES_LEFT_KEY:
            try:
                cleaned[key] = int(v)
            except (TypeError, ValueError):
                continue
            continue
        if not str(v).strip():
            continue
        cleaned[key] = v
    return cleaned or None
def catalog_has_default_uses_left(use_condition: dict | None) -> bool:
    cond = normalize_use_condition(use_condition) or {}
    return DEFAULT_USES_LEFT_KEY in cond
def catalog_default_uses_left(use_condition: dict | None) -> int | None:
    """读取图鉴 default_uses_left；调用方须先用 catalog_has_default_uses_left 判断。"""
    cond = normalize_use_condition(use_condition) or {}
    return int(cond[DEFAULT_USES_LEFT_KEY])
def merge_default_uses_left_into_condition(use_condition: dict | None, uses_left: int | None, *, unset: bool=False) -> dict | None:
    """写入或清除图鉴 default_uses_left（发放物品时的默认剩余次数）。"""
    cond = dict(normalize_use_condition(use_condition) or {})
    if unset or uses_left is None:
        cond.pop(DEFAULT_USES_LEFT_KEY, None)
    else:
        val = int(uses_left)
        if val < 0:
            raise ValueError('使用次数不能为负')
        cond[DEFAULT_USES_LEFT_KEY] = val
    return normalize_use_condition(cond)
def apply_catalog_default_uses_left(catalog, uses_left: int | None, *, provided: bool) -> str | None:
    """provided=True 表示请求体显式携带 uses_left（含 null 表示清除覆盖）。"""
    if not provided:
        return None
    if uses_left is not None and int(uses_left) < 0:
        return '使用次数不能为负'
    try:
        catalog.use_condition = merge_default_uses_left_into_condition(catalog.use_condition, int(uses_left) if uses_left is not None else None, unset=uses_left is None)
    except ValueError as exc:
        return str(exc)
    return None
def equipment_visible_in_expedition(use_condition: dict | None, target_dungeon: str | None) -> bool:
    """空条件 = 任意副本可见；否则按条件键判定。"""
    cond = normalize_use_condition(use_condition)
    if not cond:
        return True
    required_dungeon = cond.get('expedition_dungeon')
    if required_dungeon is not None:
        return (target_dungeon or '').strip() == str(required_dungeon).strip()
    return True
def is_class_transfer_invitation(use_condition: dict | None) -> bool:
    """正式转职邀请函只由图鉴结构化条件标识，不依赖名称或描述关键词。"""
    cond = normalize_use_condition(use_condition) or {}
    return bool(str(cond.get('expedition_dungeon') or '').strip() and str(cond.get('target_class') or '').strip())
def class_transfer_invitation_matches_dungeon(use_condition: dict | None, target_dungeon: str | None) -> bool:
    """正式转职邀请函是否属于本次副本；目标职业仅作凭证标识，不参与转职判定。"""
    cond = normalize_use_condition(use_condition) or {}
    if not is_class_transfer_invitation(cond):
        return False
    return str(cond.get('expedition_dungeon') or '').strip() == str(target_dungeon or '').strip()
def class_transfer_invitation_member_ids(member_snapshot: list | None, target_dungeon: str | None, use_condition_by_name: dict[str, dict | None] | None) -> list[int]:
    """返回冻结快照中本人持有、且与本次副本匹配的正式邀请函成员 ID。

    该结果只触发职业正文/结算一致性审核，不代表转职成功，也不参与邀请函消费。
    """
    condition_by_name = use_condition_by_name or {}
    member_ids: list[int] = []
    seen_member_ids: set[int] = set()
    for raw_member in member_snapshot or []:
        if not isinstance(raw_member, dict):
            continue
        try:
            member_id = int(raw_member.get('adventurer_id'))
        except (TypeError, ValueError):
            continue
        if member_id <= 0 or member_id in seen_member_ids:
            continue
        for raw_item in raw_member.get('equipment') or []:
            if not isinstance(raw_item, dict):
                continue
            try:
                quantity = int(raw_item.get('quantity', 1))
            except (TypeError, ValueError):
                quantity = 0
            item_name = str(raw_item.get('name') or '').strip()
            if quantity > 0 and item_name and class_transfer_invitation_matches_dungeon(condition_by_name.get(item_name), target_dungeon):
                member_ids.append(member_id)
                seen_member_ids.add(member_id)
                break
    return member_ids
def is_hidden_boss_clue_item(use_condition: dict | None) -> bool:
    cond = normalize_use_condition(use_condition)
    return bool(cond and cond.get('item_kind') == 'hidden_boss_clue')
def is_special_dungeon_ticket(use_condition: dict | None) -> bool:
    """特殊副本门票（转职试炼凭证 / 顿悟之钥等）：纯后端凭证，不对剧情 AI 暴露。"""
    cond = normalize_use_condition(use_condition)
    return bool(cond and cond.get('item_kind') == 'special_dungeon_ticket')
def is_backend_managed_expedition_equipment(use_condition: dict | None) -> bool:
    """仅由后端结算/触发的随身物品，不得作为普通装备资源进入探险 AI。"""
    return bool(is_hidden_boss_clue_item(use_condition) or is_special_dungeon_ticket(use_condition))


def is_equipment_hidden_from_expedition_ai(use_condition: dict | None) -> bool:
    """AI 输入排除资格；不改变物品本身，也不触发任何后端道具效果。"""
    return is_backend_managed_expedition_equipment(use_condition)


def special_dungeon_ticket_target(use_condition: dict | None) -> str | None:
    """门票绑定的特殊副本名；非门票返回 None。"""
    cond = normalize_use_condition(use_condition)
    if not is_special_dungeon_ticket(cond):
        return None
    target = str(cond.get('special_dungeon') or '').strip()
    return target or None
def is_catalog_text_locked(use_condition: dict | None) -> bool:
    """图鉴条目是否系统锁定。

    immutable 是规范字段，text_locked 是历史兼容字段。不可变物品不可锻造/炼金/嬗变，
    也不可通过申诉修改任何实例字段。正常消耗仍由 consume_item 路径允许。
    """
    cond = normalize_use_condition(use_condition)
    if not cond:
        return False
    if cond.get('immutable') is True or cond.get('text_locked') is True:
        return True
    return is_hidden_boss_clue_item(cond)
def is_catalog_immutable_item(use_condition: dict | None) -> bool:
    """不可变物品判定；保留 text_locked 作为兼容字段名。"""
    return is_catalog_text_locked(use_condition)
def catalog_text_locked_item_names(item_names: set[str] | list[str] | None) -> set[str]:
    """批量查图鉴：返回不可变（text_locked / 追查线索）的物品名集合。"""
    names = {str(n or '').strip() for n in item_names or [] if str(n or '').strip()}
    if not names:
        return set()
    from flask import has_app_context
    if not has_app_context():
        return set()
    from plugins.adventurer_s1.models import AdventurerS1Item
    rows = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(list(names))).populate_existing().all()
    return {row.name for row in rows if is_catalog_text_locked(row.use_condition)}
def catalog_immutable_item_names(item_names: set[str] | list[str] | None) -> set[str]:
    return catalog_text_locked_item_names(item_names)
def hidden_boss_clue_label(clue_index: int, clue_total: int=4) -> str:
    """玩家可见线索序号文案（不含副本 / Boss）。"""
    idx = int(clue_index or 0)
    total = int(clue_total or 4)
    if idx in (1, 2, 3, 4):
        return f'线索 {idx}/{total}'
    return '隐藏Boss线索'
_PUBLIC_CLUE_ITEM_NAME = '未鉴定追查物证'
_PUBLIC_CLUE_ITEM_DESC = '一件等待与其它线索相互印证的追查物证。'
_PUBLIC_OBSOLETE_CLUE_PREFIX = '【已废弃】'


_CLUE_NAME_MARKER = '线索·'


def _clue_boss_prefix_from_catalog_name(catalog_name: str) -> str:
    """从结构化图鉴名取出 Boss 前缀；无分隔符时不猜测。"""
    name = str(catalog_name or '').strip()
    if _CLUE_NAME_MARKER not in name:
        return ''
    return name.rsplit(_CLUE_NAME_MARKER, 1)[0].strip()
def _redact_exact_clue_identity(text: str, *, catalog_name: str='', clue_fields: dict | None=None) -> str:
    """仅按结构化实体精确移除 Boss / 副本名，不做文本语义推断。"""
    value = str(text or '').strip()
    if not value:
        return ''
    fields = clue_fields if isinstance(clue_fields, dict) else {}
    protected_terms = {_clue_boss_prefix_from_catalog_name(catalog_name), str(fields.get('boss_name') or '').strip(), str(fields.get('target_dungeon') or fields.get('hidden_boss_dungeon') or '').strip()}
    for term in sorted((term for term in protected_terms if term), key=len, reverse=True):
        value = value.replace(term, '')
    return value.strip()
def player_clue_item_flavor_name(catalog_name: str, clue_fields: dict | None=None) -> str:
    """从结构化图鉴名返回不含 Boss 前缀的玩家可见线索简称。"""
    name = str(catalog_name or '').strip()
    if not name:
        return _PUBLIC_CLUE_ITEM_NAME
    if _CLUE_NAME_MARKER in name:
        flavor = name.rsplit(_CLUE_NAME_MARKER, 1)[-1].strip()
    elif '·' in name:
        flavor = name.rsplit('·', 1)[-1].strip()
    else:
        return _PUBLIC_CLUE_ITEM_NAME
    flavor = _redact_exact_clue_identity(flavor, catalog_name=name, clue_fields=clue_fields)
    flavor = flavor or _PUBLIC_CLUE_ITEM_NAME
    fields = clue_fields if isinstance(clue_fields, dict) else {}
    if fields.get('obsolete') is True and (not flavor.startswith(_PUBLIC_OBSOLETE_CLUE_PREFIX)):
        return f'{_PUBLIC_OBSOLETE_CLUE_PREFIX}{flavor}'
    return flavor
def player_clue_item_description(clue_fields: dict | None, *, fallback: str='') -> str:
    """返回线索专属物证描述，同时精确剔除结构化 Boss / 副本名。"""
    fields = clue_fields if isinstance(clue_fields, dict) else {}
    catalog_name = str(fields.get('catalog_name') or '').strip()
    description = str(fields.get('description') or fallback or '').strip()
    description = _redact_exact_clue_identity(description, catalog_name=catalog_name, clue_fields=fields)
    return description or _PUBLIC_CLUE_ITEM_DESC
def sanitize_player_clue_error(message: str, catalog_name: str) -> str:
    """Replace one exact structured clue name in a player-facing error."""
    text = str(message or '')
    clean_name = str(catalog_name or '').strip()
    if not clean_name:
        return text
    return text.replace(clean_name, player_clue_item_flavor_name(clean_name))
def public_hidden_boss_clue_fields(use_condition: dict | None) -> dict | None:
    """服务端组装的隐藏 Boss 线索元数据（内部/AI 用，勿直接下发客户端）。"""
    cond = normalize_use_condition(use_condition)
    if not is_hidden_boss_clue_item(cond):
        return None
    dungeon = str(cond.get('hidden_boss_dungeon') or '').strip()
    try:
        clue_index = int(cond.get('clue_index') or 0)
    except (TypeError, ValueError):
        clue_index = 0
    if not dungeon or clue_index not in (1, 2, 3, 4):
        return None
    boss_name = str(cond.get('boss_name') or '').strip()
    if not boss_name:
        boss_id = str(cond.get('clue_bureau_boss_id') or '').strip()
        if boss_id:
            from plugins.adventurer_s1.services.s1_clue_bureau_catalog_service import S1ClueBureauCatalogService
            boss = S1ClueBureauCatalogService.get_boss(boss_id)
            if boss:
                boss_name = boss.name
        if not boss_name:
            from plugins.adventurer_s1.services.s1_hidden_boss_service import get_hidden_boss_definition
            definition = get_hidden_boss_definition(dungeon)
            if definition:
                boss_name = definition.name
    return {'target_dungeon': dungeon, 'clue_index': clue_index, 'clue_total': 4, 'boss_name': boss_name or None}
def build_clue_attach_meta(catalog_row) -> dict | None:
    """组装玩家投影所需的内部线索元数据；敏感实体不得直接下发。"""
    cond = normalize_use_condition(getattr(catalog_row, 'use_condition', None))
    if not is_hidden_boss_clue_item(cond):
        return None
    try:
        clue_index = int(cond.get('clue_index') or 0)
    except (TypeError, ValueError):
        clue_index = 0
    if clue_index not in (1, 2, 3, 4):
        clue_index = 0
    from plugins.adventurer_s1.services.s1_hidden_boss_service import HIDDEN_BOSS_CLUE_RARITY_DEFEATED
    catalog_rarity = str(getattr(catalog_row, 'rarity', '') or '').strip()
    obsolete = catalog_rarity == HIDDEN_BOSS_CLUE_RARITY_DEFEATED
    return {'clue_index': clue_index, 'clue_total': 4, 'description': (getattr(catalog_row, 'description', None) or '').strip(), 'clue_text': str(cond.get('clue_text') or '').strip(), 'catalog_name': str(getattr(catalog_row, 'name', '') or '').strip(), 'target_dungeon': str(cond.get('hidden_boss_dungeon') or '').strip(), 'boss_name': str(cond.get('boss_name') or '').strip(), 'rarity': catalog_rarity, 'obsolete': obsolete}
def catalog_clue_meta_by_item_names(item_names, *, session=None) -> dict[str, dict]:
    """Batch-load structured clue metadata for exact catalog names."""
    names = {str(name or '').strip() for name in item_names or [] if str(name or '').strip()}
    if not names:
        return {}
    from plugins.adventurer_s1.models import AdventurerS1Item
    query = AdventurerS1Item.query.with_entities(AdventurerS1Item.name, AdventurerS1Item.description, AdventurerS1Item.rarity, AdventurerS1Item.use_condition).filter(AdventurerS1Item.name.in_(sorted(names)))
    if session is not None:
        query = query.with_session(session)
    rows = query.all()
    result: dict[str, dict] = {}
    catalog_names = {row.name for row in rows}
    for row in rows:
        fields = build_clue_attach_meta(row)
        if fields:
            result[row.name] = fields
    missing_names = names - catalog_names
    if missing_names:
        from plugins.adventurer_s1.services.s1_clue_bureau_catalog_service import BOSS_STATUS_DEFEATED, S1ClueBureauCatalogService
        from plugins.adventurer_s1.services.s1_hidden_boss_service import HIDDEN_BOSS_CLUE_RARITY_ACTIVE, HIDDEN_BOSS_CLUE_RARITY_DEFEATED
        bosses = S1ClueBureauCatalogService.list_bosses(session=session) if session is not None else S1ClueBureauCatalogService.list_bosses()
        for boss in bosses:
            obsolete = boss.status == BOSS_STATUS_DEFEATED
            rarity = HIDDEN_BOSS_CLUE_RARITY_DEFEATED if obsolete else HIDDEN_BOSS_CLUE_RARITY_ACTIVE
            for clue in boss.clues:
                if clue.item_name not in missing_names:
                    continue
                result[clue.item_name] = {'clue_index': int(clue.index), 'clue_total': 4, 'description': clue.description, 'clue_text': clue.clue_text, 'catalog_name': clue.item_name, 'target_dungeon': boss.dungeon, 'boss_name': boss.name, 'rarity': rarity, 'obsolete': obsolete}
    return result
def sanitize_player_catalog_item_dict(entry: dict, clue_fields: dict | None, *, name_key: str, desc_key: str | None=None, type_key: str | None=None) -> None:
    """Apply the safe nickname/description projection to one catalog-backed item."""
    if not isinstance(entry, dict) or not clue_fields:
        return
    strip_hidden_boss_clue_secrets(entry)
    catalog_name = str(clue_fields.get('catalog_name') or entry.get(name_key) or '').strip()
    entry[name_key] = player_clue_item_flavor_name(catalog_name, clue_fields)
    clue_rarity = str(clue_fields.get('rarity') or '').strip()
    if clue_rarity and 'rarity' in entry:
        entry['rarity'] = clue_rarity
    if desc_key is not None:
        entry[desc_key] = player_clue_item_description(clue_fields, fallback=str(entry.get(desc_key) or ''))
    if type_key is not None:
        entry[type_key] = 'clue'
def sanitize_player_currency_log_entries(entries: list[dict] | None, *, clue_names: set[str] | None=None) -> None:
    """Hide exact persisted clue names in player-facing currency descriptions."""
    if not entries:
        return
    if clue_names is None:
        candidate_names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item_name = str(entry.get('item_name') or '').strip()
            if item_name:
                candidate_names.add(item_name)
            description = str(entry.get('description') or '')
            for pattern in ('「([^」]{1,100})」', '（([^（）]{1,100})）'):
                candidate_names.update((match.strip() for match in re.findall(pattern, description) if match.strip()))
        clue_names = set(catalog_clue_meta_by_item_names(candidate_names))
    replacements = {clean_name: player_clue_item_flavor_name(clean_name) for name in clue_names if (clean_name := str(name or '').strip())}
    if not replacements:
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item_name = str(entry.get('item_name') or '').strip()
        description = str(entry.get('description') or '')
        if item_name in replacements:
            entry['item_name'] = replacements[item_name]
        for catalog_name, flavor_name in replacements.items():
            description = description.replace(catalog_name, flavor_name)
        entry['description'] = description
def strip_hidden_boss_clue_secrets(entry: dict) -> None:
    """从客户端条目移除追查目标敏感字段（F12 不可见）。"""
    for key in ('target_dungeon', 'boss_name', 'clue_index', 'clue_total', 'clue_flavor_name', 'clue_bureau_boss_id', 'catalog_name', 'clue_text', 'description'):
        entry.pop(key, None)
def sanitize_player_item_entries(items: list | None) -> None:
    """统一 equipment/warehouse 的安全简称与专属物证描述。"""
    if not items:
        return
    for entry in items:
        if not isinstance(entry, dict):
            continue
        if entry.get('type') != 'clue' and (not entry.get('clue_label')):
            continue
        label = str(entry.get('clue_label') or '').strip()
        catalog_name = str(entry.get('name') or '')
        clue_fields = {'catalog_name': catalog_name, 'description': str(entry.get('desc') or '')}
        flavor = catalog_name.strip() if label and _CLUE_NAME_MARKER not in catalog_name and ('·' not in catalog_name) else player_clue_item_flavor_name(catalog_name, clue_fields)
        if not flavor or flavor == label:
            flavor = _PUBLIC_CLUE_ITEM_NAME
        entry['name'] = flavor
        entry['type'] = 'clue'
        entry['desc'] = player_clue_item_description(clue_fields) if label else _PUBLIC_CLUE_ITEM_DESC
        strip_hidden_boss_clue_secrets(entry)
def sanitize_player_item_log_entry(entry: dict) -> None:
    """最近动态中的线索流水使用安全简称替换完整图鉴名。"""
    if not isinstance(entry, dict) or entry.get('item_type') != 'clue':
        return
    catalog_name = str(entry.get('item_name') or '').strip()
    flavor_name = player_clue_item_flavor_name(catalog_name)
    entry['item_name'] = flavor_name
    entry['item_type'] = 'clue'
    entry['description'] = str(entry.get('description') or '').replace(catalog_name, flavor_name)
def sanitize_adventurer_payload_items(data: dict | None) -> None:
    """冒险者详情/列表 payload 中装备与仓库条目统一脱敏。"""
    if not data or not isinstance(data, dict):
        return
    sanitize_player_item_entries(data.get('equipment'))
    sanitize_player_item_entries(data.get('warehouse'))
def sanitize_market_item_name_field(data: dict, *, name_key: str, clue_meta: dict[str, dict], clue_label_key: str='clue_label', type_key: str | None=None, desc_key: str | None=None, rarity_key: str | None=None) -> None:
    """市场序列化条目：catalog 名 → 与仓库/身上一致的线索绰号 + 序号标签。"""
    catalog_name = str(data.get(name_key) or '').strip()
    if not catalog_name:
        return
    clue_fields = clue_meta.get(catalog_name)
    if not clue_fields:
        return
    idx = clue_fields.get('clue_index')
    total = int(clue_fields.get('clue_total') or 4)
    label = hidden_boss_clue_label(idx, total)
    catalog_name_full = str(clue_fields.get('catalog_name') or catalog_name).strip()
    flavor = player_clue_item_flavor_name(catalog_name_full, clue_fields)
    if flavor == label:
        flavor = _PUBLIC_CLUE_ITEM_NAME
    data[clue_label_key] = label
    data[name_key] = flavor
    if type_key is not None:
        data[type_key] = 'clue'
    if desc_key is not None:
        data[desc_key] = player_clue_item_description(clue_fields, fallback=str(data.get(desc_key) or ''))
    clue_rarity = str(clue_fields.get('rarity') or '').strip()
    if rarity_key is not None and clue_rarity:
        data[rarity_key] = clue_rarity
def sanitize_market_listing_dict(data: dict, clue_meta: dict[str, dict]) -> None:
    """对 market listing 序列化结果应用线索展示脱敏。"""
    sanitize_market_item_name_field(data, name_key='item_name', type_key='item_type', desc_key='desc', rarity_key='rarity', clue_meta=clue_meta)
    if data.get('traded_item_name'):
        sanitize_market_item_name_field(data, name_key='traded_item_name', clue_label_key='traded_clue_label', clue_meta=clue_meta)
def sanitize_market_offer_dict(data: dict, clue_meta: dict[str, dict]) -> None:
    """对 market offer 序列化结果应用线索展示脱敏。"""
    sanitize_market_item_name_field(data, name_key='offered_item_name', clue_label_key='offered_clue_label', type_key='offered_item_type', desc_key='offered_desc', rarity_key='offered_rarity', clue_meta=clue_meta)
    listing = data.get('listing')
    if isinstance(listing, dict) and listing.get('item_name'):
        sanitize_market_item_name_field(listing, name_key='item_name', clue_label_key='clue_label', clue_meta=clue_meta)
def sanitize_market_trade_result_dict(data: dict, clue_meta: dict[str, dict]) -> None:
    """对以物易物成交结果应用线索展示脱敏。"""
    sanitize_market_item_name_field(data, name_key='received_item', clue_label_key='received_clue_label', clue_meta=clue_meta)
    sanitize_market_item_name_field(data, name_key='given_item', clue_label_key='given_clue_label', clue_meta=clue_meta)
    sanitize_market_listing_dict(data, clue_meta)
def attach_hidden_boss_clue_fields(entry: dict, clue_fields: dict | None) -> None:
    """向装备/仓库序列化条目写入线索展示字段（一律玩家安全，不含副本/Boss）。"""
    if not clue_fields:
        return
    idx = clue_fields.get('clue_index')
    total = int(clue_fields.get('clue_total') or 4)
    label = hidden_boss_clue_label(idx, total)
    entry['clue_label'] = label
    entry['type'] = 'clue'
    catalog_name = str(clue_fields.get('catalog_name') or entry.get('name') or '').strip()
    flavor = player_clue_item_flavor_name(catalog_name, clue_fields)
    if flavor == label:
        flavor = _PUBLIC_CLUE_ITEM_NAME
    entry['desc'] = player_clue_item_description(clue_fields, fallback=str(entry.get('desc') or ''))
    entry['name'] = flavor
    clue_rarity = str(clue_fields.get('rarity') or '').strip()
    if clue_rarity:
        entry['rarity'] = clue_rarity
    strip_hidden_boss_clue_secrets(entry)
def sanitize_building_usage_extra_for_client(extra: dict | None, *, is_admin: bool=False) -> dict | None:
    """仅投影占用列表、恢复计算及办理倒计时实际使用的字段。"""
    if not isinstance(extra, dict):
        return None
    if is_admin:
        return extra
    fields = ('usage_duration_minutes', 'rental_start_at', 'search_hours', 'operation', 'skill_name', 'target_skill_name', 'heal_hp', 'hp_per_minute', 'hp_percent_per_minute')
    out = {key: extra[key] for key in fields if key in extra and isinstance(extra[key], (str, int, float, bool, type(None)))}
    rental_start_at = out.get('rental_start_at')
    if isinstance(rental_start_at, str) and rental_start_at:
        has_explicit_offset = rental_start_at.endswith('Z') or (len(rental_start_at) >= 6 and rental_start_at[-6] in ('+', '-') and (rental_start_at[-3] == ':'))
        if not has_explicit_offset:
            out['rental_start_at'] = f'{rental_start_at}Z'
    return out
def filter_equipment_for_expedition_ai(equipment_items, target_dungeon: str | None, use_condition_by_name: dict[str, dict | None] | None):
    if not equipment_items:
        return []
    cond_map = use_condition_by_name or {}
    visible = []
    for item in equipment_items:
        name = getattr(item, 'name', None)
        if not name:
            continue
        cond = cond_map.get(name)
        if is_equipment_hidden_from_expedition_ai(cond):
            continue
        if equipment_visible_in_expedition(cond, target_dungeon):
            visible.append(item)
    return visible
def build_use_condition_map(item_names: set[str], *, independent_read: bool=False) -> dict[str, dict | None]:
    if not item_names:
        return {}
    if independent_read:
        from db_repository.s1_item_catalog_repository import read_item_use_condition_rows
        rows = read_item_use_condition_rows(item_names)
    else:
        from plugins.adventurer_s1.models import AdventurerS1Item
        items = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(list(item_names))).all()
        rows = [(row.name, getattr(row, 'use_condition', None)) for row in items]
    return {name: normalize_use_condition(condition) for name, condition in rows}
def project_member_snapshot_for_expedition_ai(member_snapshot, *, use_condition_by_name: dict[str, dict | None] | None=None, target_dungeon: str | None=None):
    """从冻结成员快照剔除后端托管装备，同时保留内部原快照作为结算证据。

    生产调用在 Flask app context 内会批量补查图鉴条件，因此上线前已冻结、尚未
    携带投影标记的旧 checkpoint 也遵守同一边界。无 app context 的纯函数测试可
    显式传入 use_condition_by_name，避免隐式数据库依赖。
    """
    if not isinstance(member_snapshot, list):
        return member_snapshot
    names = {str(row.get('name') or '').strip() for member in member_snapshot if isinstance(member, dict) for field in ('equipment', 'equipments') for row in member.get(field) or [] if isinstance(row, dict) and str(row.get('name') or '').strip()}
    cond_map = use_condition_by_name
    if cond_map is None:
        from flask import has_app_context
        cond_map = build_use_condition_map(names) if has_app_context() else {}
    projected = []
    for raw_member in member_snapshot:
        if not isinstance(raw_member, dict):
            projected.append(raw_member)
            continue
        member = dict(raw_member)
        for field in ('equipment', 'equipments'):
            raw_rows = raw_member.get(field)
            if not isinstance(raw_rows, list):
                continue
            projected_rows = []
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    projected_rows.append(raw_row)
                    continue
                is_hidden = raw_row.get(EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD) is True or is_equipment_hidden_from_expedition_ai(cond_map.get(str(raw_row.get('name') or '').strip())) or (target_dungeon is not None and (not equipment_visible_in_expedition(cond_map.get(str(raw_row.get('name') or '').strip()), target_dungeon)))
                if is_hidden:
                    continue
                row = dict(raw_row)
                row.pop(EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD, None)
                if is_catalog_immutable_item(cond_map.get(str(raw_row.get('name') or '').strip())):
                    row['immutable'] = True
                projected_rows.append(row)
            member[field] = projected_rows
        projected.append(member)
    return projected
def collect_member_equipment_names(members) -> set[str]:
    names: set[str] = set()
    for member in members or []:
        for item in getattr(member, 'equipment_items', []) or []:
            name = getattr(item, 'name', None)
            if name:
                names.add(name)
    return names
def _settlement_equipment_ref_names(value) -> list[str]:
    """从 consumed_equipments 提取稳定原名（兼容历史 str 与当前对象）。"""
    if value is None:
        return []
    if isinstance(value, str):
        name = value.strip()
        return [name] if name else []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get('name') or '').strip()
        else:
            continue
        if name:
            names.append(name)
    return names
def _filter_settlement_equipment_refs(value, reject: set[str]):
    """从结算列表移除 reject 中的稳定原名，保留其余项与原结构。"""
    if not value or not reject:
        return value
    if isinstance(value, str):
        return value if value.strip() not in reject else []
    if not isinstance(value, list):
        return value
    kept = []
    for item in value:
        if isinstance(item, str):
            if item.strip() not in reject:
                kept.append(item)
        elif isinstance(item, dict):
            name = str(item.get('name') or '').strip()
            if name not in reject:
                kept.append(item)
        else:
            kept.append(item)
    return kept
def strip_backend_managed_equipment_refs(result_data: dict | None, *, independent_read: bool=False) -> None:
    """落库/写回前剥离后端托管物品的 consumed（隐藏 Boss 线索、特殊副本门票）。

    该剥离发生在普通装备归属校验前，因此托管物品无需进入 AI 可见白名单；
    同时避免 AI 结算字段与 settle_clue_victory_rewards / 发车扣票重复扣物。
    """
    if not isinstance(result_data, dict):
        return
    updates = result_data.get('member_updates')
    if not isinstance(updates, list):
        return
    candidate_names: set[str] = set()
    for raw in updates:
        if not isinstance(raw, dict):
            continue
        candidate_names.update(_settlement_equipment_ref_names(raw.get('consumed_equipments')))
    if not candidate_names:
        return
    cond_map = build_use_condition_map(candidate_names, independent_read=independent_read)
    managed = {name for name in candidate_names if is_equipment_hidden_from_expedition_ai(cond_map.get(name))}
    if not managed:
        return
    for raw in updates:
        if not isinstance(raw, dict):
            continue
        raw['consumed_equipments'] = _filter_settlement_equipment_refs(raw.get('consumed_equipments'), managed)
def build_expedition_equipment_context(members) -> tuple[dict, dict[str, dict | None]]:
    """返回 (图鉴描述 dict, 使用条件 dict)；描述进 AI，条件仅用于过滤。"""
    names = collect_member_equipment_names(members)
    if not names:
        return ({}, {})
    from plugins.adventurer_s1.models import AdventurerS1Item
    rows = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(list(names))).all()
    item_dict = {row.name: row.description for row in rows}
    use_condition_by_name = {row.name: normalize_use_condition(getattr(row, 'use_condition', None)) for row in rows}
    return (item_dict, use_condition_by_name)
