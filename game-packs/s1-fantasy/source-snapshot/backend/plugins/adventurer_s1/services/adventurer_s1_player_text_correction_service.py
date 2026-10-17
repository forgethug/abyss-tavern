"""玩家自助文本更正：将技能/特质/装备/冒险史中残留的探险代号替换为玩家指定的冒险者真名。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Optional, Tuple

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1PartyMember,
    AdventurerS1Settings,
)
from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
    restore_names_in_narrative,
)
from plugins.adventurer_s1.services.adventurer_s1_prompt_service import (
    PLAYER_TEXT_CODENAME_PATTERN,
    _TIAN_GAN,
    build_codename_to_real_from_snapshot,
    extract_codename_tokens,
    is_expedition_codename,
)
from plugins.adventurer_s1.services.s1_description_ai_service import (
    migrate_reviewed_description_summary,
)
from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
    apply_deterministic_trait_description_transform,
)

_MAX_SELECTIONS = 80
_MAX_DISMISSED = 300
_MAX_NAME_LEN = 7
_SNIPPET_LEN = 72
_BACKUP_KEY_PREFIX = 'player_text_correction_backup:'
_DISMISSED_KEY_PREFIX = 'player_text_correction_dismissed:'
_FIELD_LABELS = {
    'trait': '特质',
    'skill': '技能',
    'equipment': '装备',
    'history_story': '冒险史',
}
_ATTRIBUTE_LABELS = {
    'name': '名称',
    'description': '描述',
    'recovered_desc': '恢复说明',
    'archived_reason': '归档说明',
    'custom_desc': '个性化描述',
    'damage_note': '损备注',
    'main': '正文',
}
_TRAIT_ATTRS = ('name', 'description', 'recovered_desc', 'archived_reason')
_SKILL_ATTRS = ('name', 'description')
_EQUIPMENT_ATTRS = ('custom_desc', 'damage_note')
# 单字天干若落在下列正常用词/防具词/常用词内部，不提示替换（基于库内 519 条含天干文本扫描归纳）。
_ARMOR_JIA_PREFIXES = (
    '皮|轻|链|革|软|硬|板|铁|鳞|重|棉|布|麻|锁|印|玄|幽|龙|战|法|胸|肩|护|血|冰|霜|炎|焰|雷|风|毒|暗|光|圣|邪|妖|兽|狼|熊|虎|'
    '铜|银|金|魔|回|斥|候|巡|夜|守|猎|沙|岩|石|晶|虫|蛛|蛇|鳄|龟|象|犀|鹿|狐|兔|貂|影|灰|黑|白|赤|青|紫|褐|棕|绿|蓝|朱|墨|素|粗|精|'
    '制|手|工|旧|新|破|完|损|残|半|全|反|增|幅|菌|丝|内|衬|臂|腕|背|面|骨|膝|腿|腹|腰|颈|额|鬓|'
    '锁子|龙鳞|骑士|圣骑|熔|穿|幽骨|潜行|辉龙|月龙|砂潜|骨屏|共振|龙息|毒蜥|贪欲|寒冰|锻'
)
_BENIGN_SINGLE_TIAN_GAN: dict[str, tuple[re.Pattern, ...]] = {
    '甲': (
        re.compile(r'(?:' + _ARMOR_JIA_PREFIXES + r')甲'),
        re.compile(r'锁子甲'),
        re.compile(r'[一-鿿A-Za-z0-9]{0,4}铠甲'),
        re.compile(r'指甲'),
        re.compile(r'穿甲'),
        re.compile(r'熔甲'),
        re.compile(r'甲[胄壳虫]'),
    ),
    '己': (
        re.compile(r'(?<=[自])己'),
        re.compile(r'(?<=[为])己(?=[人所方用])'),
        re.compile(r'(?<=[舍])己'),
        re.compile(r'(?<=[一])己(?=[之])'),
        re.compile(r'(?<=[知克利])己'),
        re.compile(r'(?<=[以])己(?=[任])'),
        re.compile(r'(?<=[属])己'),  # 属于己…（与「自己」衔接的己）
    ),
    '丙': (
        re.compile(r'丙(?=烯)'),  # 丙烯（炼金/化学描写）
    ),
    '丁': (
        re.compile(r'(?<=[几])丁(?=质)'),  # 几丁质
        re.compile(r'(?<=[补])丁(?=层)'),  # 补丁（双层补丁）
    ),
    '辛': (
        re.compile(r'辛(?=苦)'),  # 辛苦费
        re.compile(r'辛(?=里克)'),  # 守护灵「辛里克」等人名
    ),
    '乙': (
        re.compile(r'乙(?=醇)'),  # 乙醇等
    ),
    '戊': (
        re.compile(r'戊(?=土)'),  # 戊土等玄学用词
    ),
}
_HISTORY_SNIPPET_LEN = 240
_BENIGN_CONTEXT_WINDOW = 8


def filter_player_text_codename_tokens(text: str, tokens: list[str]) -> list[str]:
    """剔除单字天干落在正常用词内部的误报代号。"""
    if not text or not tokens:
        return tokens
    kept: list[str] = []
    for token in tokens:
        if len(token) == 1 and token in _TIAN_GAN:
            if _token_only_in_benign_tian_gan_context(text, token):
                continue
        kept.append(token)
    return kept


def _token_only_in_benign_tian_gan_context(text: str, token: str) -> bool:
    """token 在文本中的每次出现是否都落在已知正常用词内部。"""
    patterns = _BENIGN_SINGLE_TIAN_GAN.get(token)
    if not patterns:
        return False
    found = False
    for match in re.finditer(re.escape(token), text):
        found = True
        if not _is_benign_tian_gan_at(text, match.start(), patterns):
            return False
    return found


def _is_benign_tian_gan_at(text: str, pos: int, patterns: tuple[re.Pattern, ...]) -> bool:
    start = max(0, pos - _BENIGN_CONTEXT_WINDOW)
    end = min(len(text), pos + _BENIGN_CONTEXT_WINDOW)
    chunk = text[start:end]
    rel = pos - start
    for pattern in patterns:
        for m in pattern.finditer(chunk):
            if m.start() <= rel < m.end():
                return True
    return False


class AdventurerS1PlayerTextCorrectionService:
    """扫描并按条目粒度替换玩家冒险者文本中的残留探险代号。"""

    @staticmethod
    def _backup_key(adventurer_id: int) -> str:
        return f'{_BACKUP_KEY_PREFIX}{adventurer_id}'

    @staticmethod
    def _dismissed_key(adventurer_id: int) -> str:
        return f'{_DISMISSED_KEY_PREFIX}{adventurer_id}'

    @classmethod
    def _load_dismissed_ids(cls, adventurer_id: int) -> set[str]:
        rec = AdventurerS1Settings.query.filter_by(key=cls._dismissed_key(adventurer_id)).first()
        if not rec or not rec.value:
            return set()
        try:
            payload = json.loads(rec.value)
            raw = payload.get('occurrence_ids') or payload.get('ids') or []
            if not isinstance(raw, list):
                return set()
            return {str(item).strip() for item in raw if isinstance(item, str) and item.strip()}
        except (json.JSONDecodeError, TypeError):
            return set()

    @classmethod
    def _save_dismissed_ids(cls, adventurer_id: int, user_id: int, ids: set[str]) -> None:
        cleaned = sorted(ids)[:_MAX_DISMISSED]
        payload = json.dumps({
            'updated_at': _portable_utc_now().isoformat(),
            'occurrence_ids': cleaned,
        }, ensure_ascii=False)
        key = cls._dismissed_key(adventurer_id)
        rec = AdventurerS1Settings.query.filter_by(key=key).first()
        if rec is None:
            db_manager.add(AdventurerS1Settings(
                key=key,
                value=payload,
                description='玩家标记为无错的文本代号词条（不再提示）',
                updated_by_user_id=user_id,
            ))
        else:
            rec.value = payload
            rec.updated_by_user_id = user_id

    @classmethod
    def _filter_dismissed(cls, adventurer_id: int, occurrences: list[dict]) -> tuple[list[dict], int]:
        dismissed = cls._load_dismissed_ids(adventurer_id)
        if not dismissed:
            return occurrences, 0
        active_ids = {item['id'] for item in occurrences}
        hidden_count = len(dismissed & active_ids)
        visible = [item for item in occurrences if item['id'] not in dismissed]
        return visible, hidden_count

    @staticmethod
    def _get_owned_adventurer(adventurer_id: int, user_id: int) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        adv = db_manager.query(AdventurerS1).filter(AdventurerS1.id == adventurer_id).first()
        if not adv:
            return None, '冒险者不存在'
        if adv.owner_id != user_id:
            return None, '无权操作此冒险者'
        if adv.status == 'exploring':
            return None, '探险中无法更正文本'
        if adv.status == 'dead':
            return None, '冒险者已死亡，无法更正文本'
        return adv, None

    @staticmethod
    def _validate_target_name(name: str) -> Optional[str]:
        if not isinstance(name, str):
            return '替换名称格式无效'
        name = name.strip()
        if not name:
            return '替换名称不能为空'
        if len(name) > _MAX_NAME_LEN:
            return f'替换名称不能超过 {_MAX_NAME_LEN} 个字'
        if is_expedition_codename(name) or PLAYER_TEXT_CODENAME_PATTERN.search(name):
            return '替换名称不能仍是探险代号'
        if not re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9]+', name):
            return '替换名称只能包含中文、英文或数字'
        if not AdventurerS1.query.filter(AdventurerS1.name == name).first():
            return f'「{name}」不是酒馆中存在的冒险者名字'
        return None

    @staticmethod
    def _collect_teammate_names(adventurer_id: int) -> list[str]:
        party_ids = [
            row[0]
            for row in (
                AdventurerS1PartyMember.query
                .with_entities(AdventurerS1PartyMember.party_id)
                .filter(AdventurerS1PartyMember.adventurer_id == adventurer_id)
                .distinct()
                .all()
            )
        ]
        if not party_ids:
            return []
        names = {
            row[0]
            for row in (
                AdventurerS1.query
                .join(AdventurerS1PartyMember, AdventurerS1PartyMember.adventurer_id == AdventurerS1.id)
                .with_entities(AdventurerS1.name)
                .filter(
                    AdventurerS1PartyMember.party_id.in_(party_ids),
                    AdventurerS1.id != adventurer_id,
                    AdventurerS1.name.isnot(None),
                )
                .distinct()
                .all()
            )
            if row[0]
        }
        return sorted(names)

    @classmethod
    def _build_codename_hints(cls, adventurer_id: int) -> dict[str, list[str]]:
        """从该冒险者参与过的 completed 探险日志 snapshot 汇总 token→曾映射真名。"""
        id_to_name = dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name).all()
        )
        hints: dict[str, set[str]] = defaultdict(set)
        q = (
            AdventurerS1ExpeditionLog.query
            .join(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id,
            )
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.full_log,
            )
            .filter(
                AdventurerS1ExpeditionLogMember.adventurer_id == int(adventurer_id),
                AdventurerS1ExpeditionLog.status == 'completed',
            )
            .order_by(AdventurerS1ExpeditionLog.id.asc())
        )

        def iter_log_rows():
            after_id = 0
            while True:
                rows = q.filter(AdventurerS1ExpeditionLog.id > after_id).limit(50).all()
                db_manager.rollback()
                if not rows:
                    return
                yield from rows
                after_id = int(rows[-1][0])

        for _log_id, full_log in iter_log_rows():
            if not isinstance(full_log, dict):
                continue
            snap = full_log.get('audit_member_snapshot')
            if not isinstance(snap, list):
                continue
            if not any(
                isinstance(row, dict) and row.get('adventurer_id') == adventurer_id
                for row in snap
            ):
                continue
            mapping = build_codename_to_real_from_snapshot(snap, id_to_name)
            for token, real in mapping.items():
                if token and real and token != real:
                    hints[token].add(real)
        return {token: sorted(names) for token, names in hints.items()}

    @staticmethod
    def _snippet(text: str, token: str, *, field_type: str | None = None) -> str:
        if field_type == 'history_story' and len(text) <= _HISTORY_SNIPPET_LEN:
            return text
        idx = text.find(token)
        if idx < 0:
            return text[:_SNIPPET_LEN]
        start = max(0, idx - 24)
        end = min(len(text), idx + len(token) + 24)
        snippet = text[start:end]
        if start > 0:
            snippet = '…' + snippet
        if end < len(text):
            snippet = snippet + '…'
        return snippet

    @staticmethod
    def _occurrence_id(field_type: str, record_id: int, attribute: str) -> str:
        return f'{field_type}:{record_id}:{attribute}'

    @classmethod
    def _hints_for_tokens(cls, tokens: list[str], hints: dict[str, list[str]]) -> list[str]:
        names: set[str] = set()
        for token in tokens:
            for name in hints.get(token) or hints.get(f'冒险者{token}') or []:
                names.add(name)
        return sorted(names)

    @staticmethod
    def _history_story_lines(adv: AdventurerS1) -> list[str]:
        raw = adv.history_story or ''
        if not raw:
            return []
        return raw.split('\n')

    @classmethod
    def _scan_occurrences(cls, adv: AdventurerS1, hints: dict[str, list[str]]) -> list[dict]:
        """返回按「记录 + 字段（冒险史按行）」拆分的可更正条目。"""
        occurrences: list[dict] = []

        def _append(
            field_type: str,
            record_id: int,
            record_name: str,
            attribute: str,
            text: str | None,
        ):
            if not isinstance(text, str) or not text:
                return
            tokens = filter_player_text_codename_tokens(
                text, extract_codename_tokens(text),
            )
            if not tokens:
                return
            occurrences.append({
                'id': cls._occurrence_id(field_type, record_id, attribute),
                'field_type': field_type,
                'field_label': _FIELD_LABELS[field_type],
                'record_id': record_id,
                'record_name': record_name,
                'attribute': attribute,
                'attribute_label': _ATTRIBUTE_LABELS.get(attribute, attribute),
                'tokens': tokens,
                'snippet': cls._snippet(text, tokens[0], field_type=field_type),
                'hint_names': cls._hints_for_tokens(tokens, hints),
            })

        for trait in adv.traits or []:
            for attr in _TRAIT_ATTRS:
                _append('trait', trait.id, trait.name, attr, getattr(trait, attr, None))

        for skill in adv.skill_items or []:
            for attr in _SKILL_ATTRS:
                _append('skill', skill.id, skill.name, attr, getattr(skill, attr, None))

        for eq in adv.equipment_items or []:
            for attr in _EQUIPMENT_ATTRS:
                _append('equipment', eq.id, eq.name, attr, getattr(eq, attr, None))

        history_lines = cls._history_story_lines(adv)
        for line_idx, line in enumerate(history_lines):
            if not line.strip():
                continue
            _append(
                'history_story',
                line_idx,
                f'冒险史 · 第{line_idx + 1}段',
                'main',
                line,
            )

        return occurrences

    @classmethod
    def _load_backup_meta(cls, adventurer_id: int) -> tuple[bool, Optional[str]]:
        rec = AdventurerS1Settings.query.filter_by(key=cls._backup_key(adventurer_id)).first()
        if not rec or not rec.value:
            return False, None
        try:
            payload = json.loads(rec.value)
            return True, payload.get('created_at')
        except (json.JSONDecodeError, TypeError):
            return True, None

    @classmethod
    def preview(cls, user_id: int, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        adv, err = cls._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        hints = cls._build_codename_hints(adventurer_id)
        all_occurrences = cls._scan_occurrences(adv, hints)
        occurrences, dismissed_count = cls._filter_dismissed(adventurer_id, all_occurrences)
        has_backup, backup_at = cls._load_backup_meta(adventurer_id)

        return {
            'adventurer_id': adv.id,
            'adventurer_name': adv.name,
            'has_issues': bool(occurrences),
            'occurrences': occurrences,
            'dismissed_count': dismissed_count,
            'candidate_names': cls._collect_teammate_names(adventurer_id),
            'has_backup': has_backup,
            'backup_at': backup_at,
        }, None

    @classmethod
    def dismiss(
        cls,
        user_id: int,
        adventurer_id: int,
        occurrence_id: str,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """玩家标记某词条为无错，后续 preview 不再展示。"""
        adv, err = cls._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err
        occurrence_id = (occurrence_id or '').strip()
        if not occurrence_id:
            return None, '缺少词条 id'
        if not cls._parse_occurrence_id(occurrence_id):
            return None, '词条 id 无效'

        hints = cls._build_codename_hints(adventurer_id)
        all_occurrences = cls._scan_occurrences(adv, hints)
        allowed = {item['id'] for item in all_occurrences}
        if occurrence_id not in allowed:
            return None, '词条不存在或已处理'

        dismissed = cls._load_dismissed_ids(adventurer_id)
        if occurrence_id in dismissed:
            remaining = len([item for item in all_occurrences if item['id'] not in dismissed])
            return {
                'adventurer_id': adv.id,
                'occurrence_id': occurrence_id,
                'dismissed': True,
                'remaining_count': remaining,
            }, None

        dismissed.add(occurrence_id)
        if len(dismissed) > _MAX_DISMISSED:
            return None, f'无错标记已达上限（{_MAX_DISMISSED} 条）'

        cls._save_dismissed_ids(adventurer_id, user_id, dismissed)
        ok, commit_err = db_manager.commit()
        if not ok:
            return None, commit_err or '保存失败'

        remaining = len([item for item in all_occurrences if item['id'] not in dismissed])
        return {
            'adventurer_id': adv.id,
            'occurrence_id': occurrence_id,
            'dismissed': True,
            'remaining_count': remaining,
        }, None

    @staticmethod
    def _expand_mapping(raw: dict[str, str]) -> dict[str, str]:
        """补全 bracket/bare M 与天干变体，供 restore_names_in_narrative 使用。"""
        expanded = dict(raw)
        for token, real in list(raw.items()):
            if not token or not real or token == real:
                continue
            m_bracket = re.fullmatch(r'【M(\d+)】', token)
            if m_bracket:
                n = m_bracket.group(1)
                expanded.setdefault(f'M{n}', real)
                continue
            m_bare = re.fullmatch(r'M(\d+)', token)
            if m_bare:
                expanded.setdefault(f'【M{m_bare.group(1)}】', real)
                continue
            if token.startswith('冒险者') and len(token) == 4 and token[-1] in _TIAN_GAN:
                expanded.setdefault(token[-1], real)
                continue
            if len(token) == 1 and token in _TIAN_GAN:
                expanded.setdefault(f'冒险者{token}', real)
        return expanded

    @classmethod
    def _parse_occurrence_id(cls, occurrence_id: str) -> Optional[tuple[str, int, str]]:
        parts = (occurrence_id or '').split(':')
        if len(parts) != 3:
            return None
        field_type, record_id_raw, attribute = parts
        if field_type not in _FIELD_LABELS:
            return None
        try:
            record_id = int(record_id_raw)
        except (TypeError, ValueError):
            return None
        allowed = {
            'trait': _TRAIT_ATTRS,
            'skill': _SKILL_ATTRS,
            'equipment': _EQUIPMENT_ATTRS,
            'history_story': ('main',),
        }
        if attribute not in allowed.get(field_type, ()):
            return None
        return field_type, record_id, attribute

    @classmethod
    def _resolve_text_target(
        cls,
        adv: AdventurerS1,
        field_type: str,
        record_id: int,
        attribute: str,
    ) -> Tuple[Optional[object], Optional[str], Optional[str]]:
        if field_type == 'history_story':
            lines = cls._history_story_lines(adv)
            if record_id < 0 or record_id >= len(lines):
                return None, None, None
            return adv, f'__history_line_{record_id}', lines[record_id]
        collection = {
            'trait': adv.traits or [],
            'skill': adv.skill_items or [],
            'equipment': adv.equipment_items or [],
        }.get(field_type) or []
        record = next((row for row in collection if row.id == record_id), None)
        if record is None:
            return None, None, None
        return record, attribute, getattr(record, attribute, None)

    @classmethod
    def _save_backup(cls, adventurer_id: int, user_id: int, items: list[dict]) -> None:
        payload = json.dumps({
            'created_at': _portable_utc_now().isoformat(),
            'count': len(items),
            'items': items,
        }, ensure_ascii=False)
        key = cls._backup_key(adventurer_id)
        rec = AdventurerS1Settings.query.filter_by(key=key).first()
        if rec is None:
            db_manager.add(AdventurerS1Settings(
                key=key,
                value=payload,
                description='玩家文本代号更正前备份（供撤回）',
                updated_by_user_id=user_id,
            ))
        else:
            rec.value = payload
            rec.updated_by_user_id = user_id

    @classmethod
    def _apply_single_occurrence(
        cls,
        adv: AdventurerS1,
        field_type: str,
        record_id: int,
        attribute: str,
        mapping: dict[str, str],
        rename_errors: list[str],
    ) -> bool:
        target, attr_name, current = cls._resolve_text_target(adv, field_type, record_id, attribute)
        if target is None or attr_name is None or not isinstance(current, str) or not current:
            return False

        new_text = restore_names_in_narrative(current, mapping)
        if new_text == current:
            return False

        if field_type == 'history_story':
            lines = cls._history_story_lines(adv)
            if record_id < 0 or record_id >= len(lines):
                return False
            new_line = restore_names_in_narrative(lines[record_id], mapping)
            if new_line == lines[record_id]:
                return False
            lines[record_id] = new_line
            adv.history_story = '\n'.join(lines)
            return True

        if attribute == 'name' and field_type != 'equipment':
            existing_names = {
                row.name
                for row in {
                    'trait': adv.traits or [],
                    'skill': adv.skill_items or [],
                    'equipment': adv.equipment_items or [],
                }[field_type]
            }
            if new_text in existing_names and new_text != current:
                label = _FIELD_LABELS[field_type]
                rename_errors.append(f'{label}「{current}」更名后与现有条目冲突')
                return False

        transform = lambda text: restore_names_in_narrative(text, mapping)
        if field_type == 'trait' and attribute == 'description':
            apply_deterministic_trait_description_transform(
                target,
                new_text,
                transform=transform,
            )
            return True
        if field_type == 'skill' and attribute == 'description':
            migrate_reviewed_description_summary(
                current,
                new_text,
                kind='skill',
                transform=transform,
            )
        elif field_type == 'equipment' and attribute == 'custom_desc':
            item_type = str(getattr(target, 'type', '') or '').strip().lower()
            migrate_reviewed_description_summary(
                current,
                new_text,
                kind='companion' if item_type == 'companion' else 'item',
                transform=transform,
            )
        setattr(target, attr_name, new_text)
        return True

    @classmethod
    def apply(
        cls,
        user_id: int,
        adventurer_id: int,
        selections: list[dict],
    ) -> Tuple[Optional[dict], Optional[str]]:
        adv, err = cls._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err
        if not isinstance(selections, list) or not selections:
            return None, '请至少勾选一条要更正的词条'
        if len(selections) > _MAX_SELECTIONS:
            return None, f'一次最多提交 {_MAX_SELECTIONS} 条词条'

        preview, perr = cls.preview(user_id, adventurer_id)
        if perr:
            return None, perr
        allowed = {item['id']: item for item in (preview or {}).get('occurrences') or []}
        if not allowed:
            return None, '当前没有需要更正的探险代号'

        parsed_selections: list[tuple[str, dict[str, str]]] = []
        for item in selections:
            if not isinstance(item, dict):
                return None, '选择格式无效'
            occurrence_id = (item.get('occurrence_id') or item.get('id') or '').strip()
            if not occurrence_id:
                return None, '缺少词条 id'
            if occurrence_id not in allowed:
                return None, f'词条「{occurrence_id}」不在可更正范围内'
            replacements = item.get('replacements') or []
            if not isinstance(replacements, list) or not replacements:
                token = (item.get('token') or '').strip()
                name = (item.get('name') or '').strip()
                if token and name:
                    replacements = [{'token': token, 'name': name}]
            token_map: dict[str, str] = {}
            allowed_tokens = set(allowed[occurrence_id]['tokens'])
            for rep in replacements:
                if not isinstance(rep, dict):
                    return None, '替换映射格式无效'
                token = (rep.get('token') or '').strip()
                name = (rep.get('name') or '').strip()
                if not token or not name:
                    continue
                if token not in allowed_tokens:
                    return None, f'词条「{occurrence_id}」不包含代号「{token}」'
                name_err = cls._validate_target_name(name)
                if name_err:
                    return None, name_err
                token_map[token] = name
            if not token_map:
                return None, f'词条「{occurrence_id}」未指定有效替换'
            parsed_selections.append((occurrence_id, token_map))

        if not parsed_selections:
            return None, '请至少勾选一条要更正的词条'

        backup_items: list[dict] = []
        rename_errors: list[str] = []
        changed_occurrences: list[str] = []
        changed_summary = {
            'traits': 0,
            'skills': 0,
            'equipment': 0,
            'history_story': False,
        }

        for occurrence_id, token_map in parsed_selections:
            parsed = cls._parse_occurrence_id(occurrence_id)
            if not parsed:
                return None, f'词条 id 无效：{occurrence_id}'
            field_type, record_id, attribute = parsed
            _target, _attr, before = cls._resolve_text_target(adv, field_type, record_id, attribute)
            if before is None:
                return None, f'词条不存在或已变更：{occurrence_id}'
            backup_items.append({
                'occurrence_id': occurrence_id,
                'field_type': field_type,
                'record_id': record_id,
                'attribute': attribute,
                'before': before,
            })

        expanded_by_occurrence = {
            occ_id: cls._expand_mapping(token_map)
            for occ_id, token_map in parsed_selections
        }

        for occurrence_id, token_map in parsed_selections:
            field_type, record_id, attribute = cls._parse_occurrence_id(occurrence_id)  # type: ignore[misc]
            touched = cls._apply_single_occurrence(
                adv,
                field_type,
                record_id,
                attribute,
                expanded_by_occurrence[occurrence_id],
                rename_errors,
            )
            if touched:
                changed_occurrences.append(occurrence_id)
                if field_type == 'trait':
                    changed_summary['traits'] += 1
                elif field_type == 'skill':
                    changed_summary['skills'] += 1
                elif field_type == 'equipment':
                    changed_summary['equipment'] += 1
                elif field_type == 'history_story':
                    changed_summary['history_story'] = True

        if rename_errors:
            db_manager.rollback()
            return None, '；'.join(rename_errors)
        if not changed_occurrences:
            return None, '未产生任何文本变更，请检查选择'

        cls._save_backup(adventurer_id, user_id, backup_items)
        ok, commit_err = db_manager.commit()
        if not ok:
            return None, commit_err or '保存失败'

        return {
            'adventurer_id': adv.id,
            'changed': changed_summary,
            'changed_occurrences': changed_occurrences,
            'applied_selections': [
                {
                    'occurrence_id': occ_id,
                    'replacements': [
                        {'token': token, 'name': name}
                        for token, name in token_map.items()
                    ],
                }
                for occ_id, token_map in parsed_selections
                if occ_id in changed_occurrences
            ],
            'has_backup': True,
        }, None

    @classmethod
    def restore(cls, user_id: int, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        adv, err = cls._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        rec = AdventurerS1Settings.query.filter_by(key=cls._backup_key(adventurer_id)).first()
        if not rec or not rec.value:
            return None, '没有可撤回的更正记录'
        try:
            payload = json.loads(rec.value)
            items = payload.get('items') or []
        except (json.JSONDecodeError, TypeError):
            return None, '备份数据已损坏，无法撤回'

        restored = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            occurrence_id = item.get('occurrence_id')
            before = item.get('before')
            if occurrence_id:
                parsed = cls._parse_occurrence_id(occurrence_id)
            else:
                parsed = None
            if parsed:
                field_type, record_id, attribute = parsed
            else:
                field_type = item.get('field_type')
                record_id = item.get('record_id')
                attribute = item.get('attribute')
                if not field_type or record_id is None or not attribute:
                    continue
            target, attr_name, current = cls._resolve_text_target(
                adv, field_type, int(record_id), attribute,
            )
            if target is None or attr_name is None or before is None:
                continue
            if field_type == 'history_story':
                lines = cls._history_story_lines(adv)
                if record_id < 0 or record_id >= len(lines):
                    continue
                if lines[record_id] != before:
                    lines[record_id] = before
                    adv.history_story = '\n'.join(lines)
                    restored += 1
            elif getattr(target, attr_name, None) != before:
                setattr(target, attr_name, before)
                restored += 1

        if restored == 0:
            return None, '备份内容与当前文本一致，无需撤回'

        ok, commit_err = db_manager.commit()
        if not ok:
            return None, commit_err or '撤回失败'

        return {
            'adventurer_id': adv.id,
            'restored_fields': restored,
            'backup_at': payload.get('created_at'),
        }, None
