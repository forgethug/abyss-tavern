"""版本化静态环境 NPC 模板目录。

这里仅保存可验证的静态模板与动作声明；具体副作用由
``s1_ambient_npc_service`` 的 handler registry 执行。后续新增剧情入口、交易或
其他静态互动时，应新增 handler_key，而不是在路由或前端按 NPC 名称分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from plugins.adventurer_s1.services.s1_ambient_npc_cat_templates import (
    CAT_NPC_TEMPLATES,
)


AFFINITY_TIERS = ('wary', 'familiar', 'close', 'trusted')
_MAX_PUBLIC_BACKGROUND_CHARS = 600


@dataclass(frozen=True)
class AmbientNpcTextLine:
    key: str
    text: str


@dataclass(frozen=True)
class AmbientNpcActionTemplate:
    key: str
    handler_key: str
    labels: Mapping[str, str]
    responses: Mapping[str, tuple[AmbientNpcTextLine, ...]]
    requires_item: bool = False
    daily_limit_key: str | None = None
    accepted_item_names: tuple[str, ...] = ()
    accepted_item_types: tuple[str, ...] = ()
    handler_config: Mapping[str, object] | None = None


@dataclass(frozen=True)
class AmbientNpcTemplate:
    key: str
    revision: int
    name: str
    breed: str
    icon_key: str
    icon: str
    short_intro: str
    public_background: str | None
    personality: str
    visual_notes: str
    gift_preferences: tuple[str, ...]
    gift_dislikes: tuple[str, ...]
    presence: Mapping[str, tuple[AmbientNpcTextLine, ...]]
    actions: Mapping[str, AmbientNpcActionTemplate]


def _text_pool(raw_pool) -> tuple[AmbientNpcTextLine, ...]:
    return tuple(AmbientNpcTextLine(str(key), str(text)) for key, text in raw_pool)


def _optional_public_background(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError('环境 NPC 公开背景必须是字符串或 None')
    value = raw.strip()
    if not value:
        return None
    if len(value) > _MAX_PUBLIC_BACKGROUND_CHARS:
        raise ValueError(
            f'环境 NPC 公开背景不得超过 {_MAX_PUBLIC_BACKGROUND_CHARS} 字符'
        )
    return value


def _freeze_template(raw: dict) -> AmbientNpcTemplate:
    actions = {}
    for action_key, action in raw['actions'].items():
        actions[action_key] = AmbientNpcActionTemplate(
            key=action_key,
            handler_key=str(action['handler_key']),
            labels=MappingProxyType(dict(action['labels'])),
            responses=MappingProxyType({
                tier: _text_pool(action['responses'][tier])
                for tier in AFFINITY_TIERS
            }),
            requires_item=bool(action.get('requires_item', False)),
            daily_limit_key=(
                str(action.get('daily_limit_key') or '').strip() or None
            ),
            accepted_item_names=tuple(action.get('accepted_item_names') or ()),
            accepted_item_types=tuple(action.get('accepted_item_types') or ()),
            handler_config=MappingProxyType(dict(action.get('handler_config') or {})),
        )
    return AmbientNpcTemplate(
        key=str(raw['key']),
        revision=int(raw['revision']),
        name=str(raw['name']),
        breed=str(raw['breed']),
        icon_key=str(raw['icon_key']),
        icon=str(raw['icon']),
        short_intro=str(raw['short_intro']),
        public_background=_optional_public_background(raw.get('public_background')),
        personality=str(raw['personality']),
        visual_notes=str(raw['visual_notes']),
        gift_preferences=tuple(str(value) for value in (raw.get('gift_preferences') or ())),
        gift_dislikes=tuple(str(value) for value in (raw.get('gift_dislikes') or ())),
        presence=MappingProxyType({
            tier: _text_pool(raw['presence'][tier])
            for tier in AFFINITY_TIERS
        }),
        actions=MappingProxyType(actions),
    )


def _validate_templates(templates: tuple[AmbientNpcTemplate, ...]) -> None:
    if not templates:
        raise ValueError('环境 NPC 模板目录不能为空')
    npc_keys: set[str] = set()
    text_keys: set[str] = set()
    for template in templates:
        if not template.key or template.key in npc_keys:
            raise ValueError(f'环境 NPC key 缺失或重复：{template.key!r}')
        npc_keys.add(template.key)
        if template.revision < 1:
            raise ValueError(f'环境 NPC 模板版本无效：{template.key}')
        if set(template.presence) != set(AFFINITY_TIERS):
            raise ValueError(f'环境 NPC 出现文案档位不完整：{template.key}')
        if not template.actions:
            raise ValueError(f'环境 NPC 至少需要一个互动动作：{template.key}')
        for tier in AFFINITY_TIERS:
            if len(template.presence[tier]) < 4:
                raise ValueError(f'环境 NPC 出现文案不足：{template.key}/{tier}')
            for line in template.presence[tier]:
                if not line.key or not line.text or line.key in text_keys:
                    raise ValueError(f'环境 NPC 文案 key 缺失或重复：{line.key!r}')
                text_keys.add(line.key)
        for action in template.actions.values():
            if not action.key or not action.handler_key:
                raise ValueError(f'环境 NPC 动作 key/handler 缺失：{template.key}')
            if set(action.labels) != set(AFFINITY_TIERS):
                raise ValueError(f'环境 NPC 动作文案档位不完整：{template.key}/{action.key}')
            if set(action.responses) != set(AFFINITY_TIERS):
                raise ValueError(f'环境 NPC 回应文案档位不完整：{template.key}/{action.key}')
            if action.handler_key == 'consume_item_affinity' and (
                not action.accepted_item_names or not action.accepted_item_types
            ):
                raise ValueError(
                    f'消耗物品动作缺少精确名称/类型白名单：{template.key}/{action.key}'
                )
            if action.handler_key == 'consume_gift_ai' and (
                not action.requires_item or not action.daily_limit_key
            ):
                raise ValueError(
                    f'AI 赠礼动作缺少物品或每日额度声明：{template.key}/{action.key}'
                )
            for tier in AFFINITY_TIERS:
                if len(action.responses[tier]) < 6:
                    raise ValueError(f'环境 NPC 回应文案不足：{template.key}/{action.key}/{tier}')
                for line in action.responses[tier]:
                    if not line.key or not line.text or line.key in text_keys:
                        raise ValueError(f'环境 NPC 文案 key 缺失或重复：{line.key!r}')
                    text_keys.add(line.key)


_TEMPLATES = tuple(_freeze_template(raw) for raw in CAT_NPC_TEMPLATES)
_validate_templates(_TEMPLATES)
AMBIENT_NPC_CATALOG: Mapping[str, AmbientNpcTemplate] = MappingProxyType({
    template.key: template for template in _TEMPLATES
})
AMBIENT_NPC_KEYS = tuple(AMBIENT_NPC_CATALOG)


def get_ambient_npc_template(npc_key: str) -> AmbientNpcTemplate | None:
    return AMBIENT_NPC_CATALOG.get(str(npc_key or '').strip())


def iter_ambient_npc_templates() -> tuple[AmbientNpcTemplate, ...]:
    return _TEMPLATES
