import re


UNKNOWN_TRAIT_DESCRIPTIONS = {
    '神秘的特质',
    '神秘的特质，效果未知。',
}

_ADVENTURER_NAME_CONTROL_CHAR_PATTERN = re.compile(r'[\x00-\x1f\x7f-\x9f]+')
# 老格式：`名（短描述）` — 闭合括号 + 描述不含嵌套括号；DOTALL + 取消括号嵌套限制后能覆盖样本 2
# 这种长描述带嵌套中文括号的场景。base 上限 50 字避免吞掉过长的整段脏字符串。
_TRAIT_SUFFIX_NOTE_PATTERN = re.compile(
    r'^(?P<base>.{1,50}?)\s*[\(（](?P<note>.{2,800})[\)）]\s*$',
    re.DOTALL,
)
# 未闭合 / 截断场景：`名(描述...`，描述长度 ≥10 即接受；用于样本 1 这种 AI 没写完右括号的脏数据。
_TRAIT_INCOMPLETE_BRACKET_PATTERN = re.compile(
    r'^(?P<base>.{1,50}?)\s*[\(（](?P<note>.{10,})$',
    re.DOTALL,
)
_UPGRADED_EQUIPMENT_DESC_LABEL_PATTERN = re.compile(r'\s*(强化后|升级后|附魔后|重铸后|改造后)[：:]\s*')
_TEXT_MERGE_TRIM_CHARS = '。；;，,、:：!?？！ '
_TRUNCATED_EQUIPMENT_NAME_PATTERN = re.compile(r'^(?P<base>.+?[\)）])\s*[\(（](?P<desc_fragment>[^)）]{10,})$')
_NESTED_INCOMPLETE_BRACKET_NAME_PATTERN = re.compile(
    r'^(?P<base>.+?)\s*[\(（][^)）]*[\(（][^)）]*$',
)
_INCOMPLETE_BRACKET_NAME_PATTERN = re.compile(r'^(?P<base>.+?)\s*[\(（](?P<note>[^)）]{10,})$')
_NAME_DESC_COLON_SUFFIX_PATTERN = re.compile(
    r'^(?P<base>[^\s:：][^:：\n\r]{0,29})\s*[:：]\s*(?P<desc>\S.{3,})$',
    re.DOTALL,
)
_RESERVED_ADVENTURER_ITEM_NAMES = {
    'name',
    'type',
    'rarity',
    'desc',
    'description',
    'uses_left',
    'locked',
    'damage_stage',
    'damage_note',
    'note',
}


def _extract_display_text(value):
    """把历史脏值里的对象/数组描述尽量提取为可读文本。"""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ''

    if isinstance(value, dict):
        for key in ('desc', 'description', 'effect', 'text', 'content', 'note'):
            extracted = _extract_display_text(value.get(key))
            if extracted:
                return extracted

        ignored_keys = {'name', 'type', 'rarity', 'locked', 'hidden', 'merged_from', 'merged_into'}
        text_parts = []
        for key, item in value.items():
            if key in ignored_keys:
                continue
            extracted = _extract_display_text(item)
            if extracted and extracted not in text_parts:
                text_parts.append(extracted)
            if len(text_parts) >= 3:
                break
        return '；'.join(text_parts)

    if isinstance(value, (list, tuple, set)):
        text_parts = []
        for item in value:
            extracted = _extract_display_text(item)
            if extracted and extracted not in text_parts:
                text_parts.append(extracted)
        return '；'.join(text_parts)

    return str(value).strip()


def _strip_unknown_trait_description_fragments(text):
    """去掉被错误拼进描述里的占位废话，保留真实描述片段。"""
    cleaned_text = _extract_display_text(text)
    if not cleaned_text:
        return ''

    # 允许占位文案出现在句子末尾、换行、分号或字符串开头之后；
    # 仅要求占位本身完整 (`神秘的特质` 或 `神秘的特质，效果未知。`) 且后面紧跟句末或分号。
    text_without_placeholders = re.sub(
        r'(?:^|(?<=[；;。！？!?\n\r\s]))\s*神秘的特质(?:，效果未知。?)?(?=$|[；;。！？!?\n\r])',
        '',
        cleaned_text
    )
    text_without_placeholders = re.sub(r'^[；;\s]+|[；;\s]+$', '', text_without_placeholders).strip()
    if not text_without_placeholders:
        return ''

    fragments = []
    for fragment in re.split(r'\s*[；;]\s*', text_without_placeholders):
        cleaned_fragment = _extract_display_text(fragment)
        if not cleaned_fragment or cleaned_fragment in UNKNOWN_TRAIT_DESCRIPTIONS:
            continue
        if cleaned_fragment not in fragments:
            fragments.append(cleaned_fragment)
    return '；'.join(fragments)


def normalize_adventurer_item_name(item_name):
    """统一清洗道具名，兼容历史脏值。"""
    return _extract_display_text(item_name)


def split_name_desc_colon_suffix(raw_name):
    """拆分 AI 常见的 `名称: 描述` / `名称：描述` 冒号写法。

    返回 (base_name, suffix_desc)；如果不符合"短名字 + 冒号 + 长描述"特征则
    保持原值并返回 ('', '')，避免误伤本身就含冒号的合法名字（极少见）。
    """
    cleaned = _extract_display_text(raw_name)
    if not cleaned:
        return '', ''

    match = _NAME_DESC_COLON_SUFFIX_PATTERN.match(cleaned)
    if not match:
        return cleaned, ''

    base = _extract_display_text(match.group('base'))
    suffix = _extract_display_text(match.group('desc'))
    if not base or not suffix:
        return cleaned, ''
    if len(base) > 30 or len(suffix) < 4:
        return cleaned, ''
    return base, suffix


def sanitize_equipment_name(name: str) -> str:
    """清洗装备名中被截断拼入的描述文本，返回干净的装备名。

    处理三种典型脏数据：
    1. "远古龙鳞重盾(虚空腐蚀)(以远古红龙腹鳞...每" — 闭合括号后又跟了截断的描述
    2. "远古龙鳞重盾(虚空腐蚀" — 只有左括号没有右括号（截断）
    3. "远古龙鳞重盾: 以远古红龙腹鳞..." — AI 把描述用冒号拼到名字后面
    """
    if not name or not isinstance(name, str):
        return name or ''
    cleaned = name.strip()
    if not cleaned:
        return cleaned
    m = _TRUNCATED_EQUIPMENT_NAME_PATTERN.match(cleaned)
    if m:
        return m.group('base').strip()
    m = _NESTED_INCOMPLETE_BRACKET_NAME_PATTERN.match(cleaned)
    if m:
        return m.group('base').strip()
    m = _INCOMPLETE_BRACKET_NAME_PATTERN.match(cleaned)
    if m:
        return m.group('base').strip()
    base, _suffix = split_name_desc_colon_suffix(cleaned)
    if base and base != cleaned:
        return base
    return cleaned


def is_reserved_adventurer_item_name(item_name):
    """字段键名误写成道具名时返回 True。"""
    normalized_name = normalize_adventurer_item_name(item_name)
    if not normalized_name:
        return True
    return normalized_name.lower() in _RESERVED_ADVENTURER_ITEM_NAMES


def _normalize_text_for_dedupe(value):
    """把文本压成便于去重比对的紧凑形式。"""
    return re.sub(r'[\s。；;，,、:：!?？！]+', '', str(value or ''))


def merge_adventurer_text_fragments(*parts):
    """按顺序合并多段文本，并自动去掉明显重复片段。"""
    merged_parts = []
    seen_parts = set()
    for part in parts:
        cleaned_part = _strip_unknown_trait_description_fragments(part)
        dedupe_key = _normalize_text_for_dedupe(cleaned_part)
        if not cleaned_part or not dedupe_key or dedupe_key in seen_parts:
            continue
        merged_parts.append(cleaned_part)
        seen_parts.add(dedupe_key)
    return '；'.join(merged_parts)


def _looks_like_trait_suffix_description(note):
    """只把明显像说明文本的尾部括号内容视为特质描述，而不是名字的一部分。"""
    cleaned_note = _extract_display_text(note)
    if not cleaned_note:
        return False
    if any(char in cleaned_note for char in '，。；：！？、:;,.!?'):
        return True
    if re.search(r'\s', cleaned_note):
        return True
    return len(cleaned_note) >= 8


_TRAIT_CLASSIFICATION_TAG_RE = re.compile(
    r'[\s;；]*[;；]\s*(negative|positive|负面|正面)\s*$',
    re.IGNORECASE,
)


def _strip_trait_classification_tag(name: str) -> str:
    """剥离 AI 末尾附带的分类标签（；negative / ；positive / ；负面 / ；正面）。"""
    return _TRAIT_CLASSIFICATION_TAG_RE.sub('', name).strip()


def split_adventurer_trait_name_suffix(trait_name):
    """拆分 AI 常见的 `特质名（描述）` / `特质名(长描述含嵌套括号)` / `特质名(未闭合截断` / `特质名: 描述` 写法。"""
    cleaned_name = _extract_display_text(trait_name)
    if not cleaned_name:
        return '', ''

    # 1) 闭合括号 + 任意 note 内容（含换行、嵌套括号）
    match = _TRAIT_SUFFIX_NOTE_PATTERN.match(cleaned_name)
    if match:
        base_name = _extract_display_text(match.group('base'))
        suffix_desc = _extract_display_text(match.group('note'))
        if base_name and _looks_like_trait_suffix_description(suffix_desc):
            return base_name, suffix_desc

    # 2) 未闭合括号 fallback：AI 把描述写到一半就被截断的脏 name
    incomplete_match = _TRAIT_INCOMPLETE_BRACKET_PATTERN.match(cleaned_name)
    if incomplete_match:
        base_name = _extract_display_text(incomplete_match.group('base'))
        suffix_desc = _extract_display_text(incomplete_match.group('note'))
        if base_name and _looks_like_trait_suffix_description(suffix_desc):
            return base_name, suffix_desc

    # 3) `特质名: 描述` 冒号写法
    base_name, colon_suffix = split_name_desc_colon_suffix(cleaned_name)
    if base_name and base_name != cleaned_name and _looks_like_trait_suffix_description(colon_suffix):
        return base_name, colon_suffix

    # 4) 剥离 AI 末尾附带的分类标签（；negative / ；positive / ；负面 / ；正面）
    cleaned_name = _strip_trait_classification_tag(cleaned_name)

    return cleaned_name, ''


def sanitize_adventurer_name(name, fallback='未知冒险者'):
    """运行时清洗冒险者显示名，兼容历史脏数据但不直接改库。"""
    raw_name = str(name or '')
    if not raw_name:
        return fallback

    cleaned_name = _ADVENTURER_NAME_CONTROL_CHAR_PATTERN.sub('', raw_name)
    cleaned_name = re.sub(r'\s+', ' ', cleaned_name).strip()
    if not cleaned_name:
        return fallback

    if raw_name.lower().startswith('null') and _ADVENTURER_NAME_CONTROL_CHAR_PATTERN.search(raw_name):
        return 'null'

    if cleaned_name.lower().startswith('null'):
        suffix = cleaned_name[4:].strip()
        if not suffix:
            return 'null'

    return cleaned_name[:100]


def is_unknown_trait_description(desc):
    cleaned_desc = _strip_unknown_trait_description_fragments(desc)
    return not cleaned_desc


def resolve_adventurer_trait_description(trait_name, desc=None, trait_dict=None):
    """返回特质应使用的描述，兼容历史占位文案与旧字符串格式。"""
    raw_name = _extract_display_text(trait_name)
    normalized_name, suffix_desc = split_adventurer_trait_name_suffix(raw_name)
    normalized_desc = merge_adventurer_text_fragments(suffix_desc, desc)

    if normalized_desc and not is_unknown_trait_description(normalized_desc):
        return normalized_desc

    if trait_dict is not None:
        fallback_desc = _extract_display_text(trait_dict.get(normalized_name, ''))
        if not fallback_desc and raw_name and raw_name != normalized_name:
            fallback_desc = _extract_display_text(trait_dict.get(raw_name, ''))
        if fallback_desc:
            return fallback_desc

    if normalized_desc:
        return normalized_desc
    return "神秘的特质，效果未知。"


def normalize_adventurer_trait_name_and_desc(trait_name, desc=None, trait_dict=None):
    """统一清洗特质名与描述，兼容尾部括号说明和对象脏值。"""
    normalized_name, suffix_desc = split_adventurer_trait_name_suffix(trait_name)
    return normalized_name, resolve_adventurer_trait_description(
        trait_name=normalized_name,
        desc=merge_adventurer_text_fragments(suffix_desc, desc),
        trait_dict=trait_dict,
    )


def normalize_adventurer_trait_entry(trait, trait_dict=None):
    """统一特质条目结构，兼容字符串和对象格式。"""
    is_object_entry = isinstance(trait, dict)
    if isinstance(trait, dict):
        entry = dict(trait)
        trait_name = entry.get('name', '')
    elif isinstance(trait, str):
        trait_name = trait
        entry = {'name': trait_name}
    else:
        trait_name = str(trait)
        entry = {'name': trait_name}

    normalized_name, normalized_desc = normalize_adventurer_trait_name_and_desc(
        trait_name=trait_name,
        desc=entry.get('desc'),
        trait_dict=trait_dict,
    )
    entry['name'] = normalized_name
    entry['desc'] = normalized_desc

    entry['hidden'] = bool(entry.get('hidden', False))
    merged_from = entry.get('merged_from') or []
    if not isinstance(merged_from, list):
        merged_from = [str(merged_from)]
    entry['merged_from'] = [
        split_adventurer_trait_name_suffix(name)[0]
        for name in merged_from
        if split_adventurer_trait_name_suffix(name)[0]
    ]

    if entry.get('merged_into') is not None:
        entry['merged_into'] = split_adventurer_trait_name_suffix(entry.get('merged_into'))[0]

    return entry


def normalize_adventurer_traits_payload(traits, trait_dict=None):
    """批量清洗并按名字去重特质，避免历史脏数据展示出重复名字。"""
    normalized_traits = []
    entry_map = {}
    for trait in traits or []:
        entry = normalize_adventurer_trait_entry(trait, trait_dict)
        trait_name = entry.get('name')
        if not trait_name:
            continue

        dedupe_key = (trait_name, bool(entry.get('hidden')))
        existing_entry = entry_map.get(dedupe_key)
        if existing_entry is None:
            normalized_traits.append(entry)
            entry_map[dedupe_key] = entry
            continue

        existing_entry['desc'] = merge_adventurer_text_fragments(existing_entry.get('desc'), entry.get('desc'))
        existing_entry['hidden'] = bool(existing_entry.get('hidden') or entry.get('hidden'))
        existing_entry['is_merged_trait'] = bool(existing_entry.get('is_merged_trait') or entry.get('is_merged_trait'))
        merged_from = [
            name
            for name in [*(existing_entry.get('merged_from') or []), *(entry.get('merged_from') or [])]
            if name
        ]
        if merged_from:
            existing_entry['merged_from'] = list(dict.fromkeys(merged_from))
        if not existing_entry.get('merged_into') and entry.get('merged_into'):
            existing_entry['merged_into'] = entry.get('merged_into')
        if not existing_entry.get('archived_reason') and entry.get('archived_reason'):
            existing_entry['archived_reason'] = entry.get('archived_reason')
        if not existing_entry.get('archived_at') and entry.get('archived_at'):
            existing_entry['archived_at'] = entry.get('archived_at')
        if not existing_entry.get('merge_generated_by') and entry.get('merge_generated_by'):
            existing_entry['merge_generated_by'] = entry.get('merge_generated_by')

    return normalized_traits


def _merge_upgraded_equipment_desc_fragments(base_desc, upgraded_desc):
    """合并升级前后的装备描述，并折叠升级描述中重复的旧前缀。"""
    if not base_desc:
        return upgraded_desc
    if not upgraded_desc:
        return base_desc

    upgraded_body = _UPGRADED_EQUIPMENT_DESC_LABEL_PATTERN.sub('', upgraded_desc, count=1).strip()
    base_prefix = base_desc.rstrip(_TEXT_MERGE_TRIM_CHARS)
    if upgraded_body == base_desc:
        return base_desc
    if base_prefix and upgraded_body.startswith(base_prefix):
        upgraded_suffix = upgraded_body[len(base_prefix):].lstrip(_TEXT_MERGE_TRIM_CHARS)
        if not upgraded_suffix:
            return base_desc
        return f"{base_desc} 强化后：{upgraded_suffix}"
    if base_desc in upgraded_desc:
        return upgraded_desc
    return f"{base_desc} 强化后：{upgraded_desc}"


def collapse_redundant_adventurer_equipment_desc(desc):
    """折叠已写进数据库的重复升级描述，避免旧描述重复两遍。"""
    cleaned_desc = _extract_display_text(desc)
    if not cleaned_desc:
        return ''

    match = _UPGRADED_EQUIPMENT_DESC_LABEL_PATTERN.search(cleaned_desc)
    if not match:
        return cleaned_desc

    base_desc = cleaned_desc[:match.start()].strip()
    upgraded_desc = cleaned_desc[match.end():].strip()
    if not base_desc or not upgraded_desc:
        return cleaned_desc
    return _merge_upgraded_equipment_desc_fragments(base_desc, upgraded_desc)


def merge_adventurer_upgraded_equipment_desc(base_desc, upgraded_desc):
    """统一合并升级前后的装备描述，供运行时和修复脚本复用。"""
    return _merge_upgraded_equipment_desc_fragments(
        collapse_redundant_adventurer_equipment_desc(base_desc),
        collapse_redundant_adventurer_equipment_desc(upgraded_desc),
    )


def split_adventurer_traits(traits, trait_dict=None):
    """按可见与归档状态拆分特质。"""
    visible_traits = []
    archived_traits = []
    for entry in normalize_adventurer_traits_payload(traits, trait_dict):
        if entry.get('hidden'):
            archived_traits.append(entry)
        else:
            visible_traits.append(entry)
    return visible_traits, archived_traits
