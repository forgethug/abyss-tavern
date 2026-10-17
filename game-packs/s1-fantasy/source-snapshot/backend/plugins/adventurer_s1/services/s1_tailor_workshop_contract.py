"""绮罗裁坊固定裁制方案协议。

客户端只提交稳定 code；进入 AI user prompt 的裁制文字全部由服务端持有。
裁制方案只调整构思优先级，不建立材料事实、成功条件或升档权限。
"""

from __future__ import annotations


TAILOR_WORKSHOP_OPERATION_DEFAULT = 'free'

_TAILOR_WORKSHOP_OPERATION_PROMPTS = {
    'free': (
        '自由裁作：按两件投入与裁坊的精细衣装工艺，自由寻找最自然的衣装成品；'
        '第 1 件仍是优先保留的主体，但不预设具体成品形态、用途、成功或品质提升。'
    ),
    'reinforce': (
        '密缝加固：优先保留第 1 件投入的主体身份与核心用途；仅在第 2 件投入的事实和'
        '精细衣装工艺确实支持时，加强主体已有的结构、耐用性或防护，不得凭空增加能力。'
    ),
    'refit': (
        '合身改制：优先保留第 1 件投入的主体与核心用途，调整其贴合、活动或穿戴方式；'
        '第 2 件投入必须实际参与这种改变，不得只作为装饰性借口。'
    ),
    'layer': (
        '复层织合：优先让第 2 件投入成为第 1 件主体的实际层次或连接结构；'
        '形成的性质与限制必须可由两件投入和本次工艺共同追溯。'
    ),
    'embroider': (
        '纹绣引性：优先通过表面纹饰或细部连接承接第 2 件投入已经具备的关键性质；'
        '名称、题材或外观联想不能替代真实物品事实。'
    ),
    'reshape': (
        '拆裁重制：允许改变第 1 件投入的衣装形态或穿戴用途，以两件投入共同支持的新结构'
        '为优先；仍须保留理解原主体连续性所需的事实。'
    ),
    'masterwork': (
        '极致巧作：仅在两件投入共同支持本质提升时，优先追求更明显的精细质变或品质提升；'
        '支持不足时保持应有品质、形成较小变化或失败，不得为该方案强行升档。'
    ),
}

TAILOR_WORKSHOP_OPERATION_CODES = tuple(_TAILOR_WORKSHOP_OPERATION_PROMPTS)


def normalize_tailor_workshop_operation(value) -> tuple[str | None, str | None]:
    """规范固定裁制 code；缺省兼容旧会话，非法值失败关闭。"""
    if value is None:
        return TAILOR_WORKSHOP_OPERATION_DEFAULT, None
    if not isinstance(value, str) or value not in _TAILOR_WORKSHOP_OPERATION_PROMPTS:
        return None, '裁制方案不合法'
    return value, None


def get_tailor_workshop_operation_prompt(code: str) -> str:
    """返回服务端固定的 user prompt 片段。调用前必须完成 code 规范化。"""
    return _TAILOR_WORKSHOP_OPERATION_PROMPTS[code]
