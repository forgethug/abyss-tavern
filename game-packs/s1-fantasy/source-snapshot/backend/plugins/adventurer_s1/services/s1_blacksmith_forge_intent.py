"""铁匠铺固定锻造取向协议。

客户端只提交稳定 code；真正进入 AI user prompt 的文字全部由服务端持有。
这些取向声明成品的保留边界与边界内的构思侧重，不建立材料事实、配方、成功或升档权限。
"""

from __future__ import annotations


BLACKSMITH_FORGE_INTENT_DEFAULT = 'free'

_BLACKSMITH_FORGE_INTENT_PROMPTS = {
    'free': (
        '自由创作：按两件投入与普通铁匠工艺自由寻找最自然的成品方向；'
        '不预设主体、用途、成功或品质提升。'
    ),
    'reinforce': (
        '主体强化：保留第 1 件投入的主体身份与核心用途，在此基础上让第 2 件投入'
        '通过可行工艺加强它已经具备的关键性质。'
    ),
    'remedy_limit': (
        '补足短板：保留第 1 件投入的主体身份与核心用途；仅在第 2 件投入的事实确实对应'
        '第 1 件已经存在的限制时尝试缓解，不得删除没有被本次工艺真正解决的限制。'
    ),
    'merge_trait': (
        '特性融合：保留第 1 件投入的主体身份与核心用途，让第 2 件投入已有的关键性质通过本次工艺'
        '成为成品可观察、可使用的一部分；不得把名称或题材联想当作既有性质。'
    ),
    'reshape': (
        '形态重塑：允许改变第 1 件投入的形态或使用方式，以两件投入共同支持的新结构为优先；'
        '仍须保留理解原主体连续性所需的事实。'
    ),
    'conservative': (
        '稳健锻造：保留第 1 件投入的主体身份与核心用途，优先选择稳定、可用且工艺可信的改进；'
        '不为追求新奇或升档扩大投入能力。'
    ),
    'breakthrough': (
        '追求突破：仅在两件投入共同支持本质提升时，优先追求更明显的质变或品质提升；'
        '支持不足时保持应有品质、形成较小变化或失败，不能为了该取向强行升档。'
    ),
}

BLACKSMITH_FORGE_INTENT_CODES = tuple(_BLACKSMITH_FORGE_INTENT_PROMPTS)


def normalize_blacksmith_forge_intent(value) -> tuple[str | None, str | None]:
    """规范固定取向 code；缺省兼容旧会话，非法值失败关闭。"""
    if value is None:
        return BLACKSMITH_FORGE_INTENT_DEFAULT, None
    if not isinstance(value, str) or value not in _BLACKSMITH_FORGE_INTENT_PROMPTS:
        return None, '锻造取向不合法'
    return value, None


def get_blacksmith_forge_intent_prompt(code: str) -> str:
    """返回服务端固定的 user prompt 片段。调用前必须完成 code 规范化。"""
    return _BLACKSMITH_FORGE_INTENT_PROMPTS[code]
