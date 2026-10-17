"""S1 持久化资产描述的严格三方合并。"""

from __future__ import annotations


def _append_only_suffix(base: str, candidate: str) -> str | None:
    """只识别 ``base + 空行 + 新段落`` 的精确追加形状。"""
    if candidate == base:
        return ''
    if not base:
        return None
    prefix = f'{base}\n\n'
    if not candidate.startswith(prefix):
        return None
    suffix = candidate[len(prefix):]
    return suffix if suffix.strip() else ''


def _append_unique(authoritative: str, suffix: str) -> str:
    """把一个已精确拆出的追加段落幂等地接到权威正文后。"""
    if not suffix.strip():
        return authoritative
    if authoritative == suffix or authoritative.endswith(f'\n\n{suffix}'):
        return authoritative
    if not authoritative:
        return suffix
    return f'{authoritative}\n\n{suffix}'


def rebase_s1_concurrent_public_description(
    base_description: object,
    current_description: object,
    proposed_description: object,
) -> str | None:
    """合并同一冻结正文派生出的并发修改；无法无损确定时返回 ``None``。

    仅当至少一条分支是基于冻结正文的精确追加时才合并。另一条分支可以是
    申诉确认后的整段改写；该改写保持权威，同时保留可精确拆出的追加段落。
    """
    base = str(base_description or '')
    current = str(current_description or '')
    proposed = str(proposed_description or '')

    if current == base:
        return proposed
    if proposed == base or current == proposed:
        return current
    if not base or not current or not proposed:
        return None

    current_suffix = _append_only_suffix(base, current)
    proposed_suffix = _append_only_suffix(base, proposed)
    if current_suffix is None and proposed_suffix is None:
        return None
    if current_suffix is not None and proposed_suffix is None:
        return _append_unique(proposed, current_suffix)
    if current_suffix is None and proposed_suffix is not None:
        return _append_unique(current, proposed_suffix)
    return _append_unique(current, proposed_suffix or '')
