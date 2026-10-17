"""Exact legacy display sentinels, not natural-language effect classification."""


def is_missing_asset_description(kind: str, value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = value.strip().rstrip('。.')
    if not text:
        return True
    placeholders = {
        'trait': {'神秘的特质', '神秘的特质，效果未知', '未知效果的特质'},
        'skill': {'技能效果待探索'},
    }
    return text in placeholders.get(kind, set())


S1_MISSING_DESCRIPTION_REPAIR_RULES = """
【特质与技能的缺失描述】
空白描述，以及整段仅为历史显示占位「神秘的特质」「神秘的特质，效果未知」「未知效果的特质」「技能效果待探索」（可带句末句号），都表示描述缺失，不是已确定的能力、隐藏效果或正常玩法设定。完整有效描述中引用这些文字，不因此视为空描述。
描述缺失本身是数据异常；补写内容须来自该记录归属者的权威历史描述、完整探险正文与结算证据，只恢复已经成立的持续状态或可复现效果，保留代价、限制和恢复边界。名字或玩家自拟效果不能证明能力，补描述不授予新职业、新能力或队友资产，也不删除再创建原记录。
证据足够时，一审按选中稳定 id 输出 traits.update.description 或 skills.update.description；二审核对该实际字段变更，必要时使用已有 supplement_patch / override_patch 补齐，不能把另一条占位文案当成补全成功。证据不足时明确缺失的内容依据，按当前角色已有 request 状态和查询权限处理；缺失效果依据不等于描述占位是正常设定。玩家复核只判断原处理是否漏修及证据是否充分，不直接执行补写。
"""
