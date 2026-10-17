"""职业变更相关的共享解析工具。

职业技能/特质的具体命名与描述由 AI 生成；后端不再为 new_class 自动制造模板能力。
"""
from __future__ import annotations

import re
from typing import Iterable

from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
    parse_member_new_trait_updates,
)
from plugins.adventurer.services.adventurer_parser_service import _normalize_new_skills_payload

CLASS_IDENTITY_SKILL_SCROLL_NAME = '职业通用技能卷轴'
_CLASS_NAME_RE = re.compile(r'^[一-鿿a-zA-Z]{2,20}$')


class S1ClassIdentityService:
    """职业字段清洗与历史日志解析。"""

    @staticmethod
    def _normalize_class(value) -> str | None:
        if not isinstance(value, str):
            return None
        name = ' '.join(value.split())
        if not name or not _CLASS_NAME_RE.match(name):
            return None
        return name

    @classmethod
    def _iter_update_traits(cls, update: dict | None) -> Iterable[tuple[str, str]]:
        if not isinstance(update, dict):
            return
        raw = update.get('new_traits')
        if not raw:
            return
        for entry in parse_member_new_trait_updates(raw, {}, member_name=''):
            name = str(entry.get('name') or '').strip()
            if name:
                yield name, str(entry.get('description') or '')

    @classmethod
    def _iter_update_skills(cls, update: dict | None) -> Iterable[tuple[str, str]]:
        if not isinstance(update, dict):
            return
        for skill in _normalize_new_skills_payload(
            update.get('new_skills'),
            split_semantic_name_desc=False,
        ) or []:
            if not isinstance(skill, dict):
                continue
            name = str(skill.get('name') or '').strip()
            if name:
                yield name, str(skill.get('desc') or skill.get('description') or '')
