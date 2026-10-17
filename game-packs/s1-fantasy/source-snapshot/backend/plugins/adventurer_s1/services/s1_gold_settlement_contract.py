"""S1 探险团队金币收益与个人金币变动合同。"""

from __future__ import annotations

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)


S1_GOLD_SETTLEMENT_FIELD = 'gold_settlement'
S1_GOLD_SETTLEMENT_CONTRACT_VERSION = 2
S1_GOLD_SETTLEMENT_VERSION_FIELD = '_gold_settlement_contract_version'
S1_GOLD_SETTLEMENT_COMPONENTS_FIELD = '_gold_settlement_components'
S1_GOLD_SETTLEMENT_ERROR_MARKER = '金币结算归属合同错误'
S1_GOLD_SETTLEMENT_PERSONAL_REPAIR_MARKER = '金币个人结算局部可审核修复'

_TEAM_GAIN_FIELDS = frozenset({'amount'})
_PERSONAL_CHANGE_FIELDS = frozenset({
    'member_id', 'amount',
})


def has_current_s1_gold_settlement_contract(value: object) -> bool:
    """Whether a result belongs to the current top-level gold contract."""
    return isinstance(value, dict) and (
        S1_GOLD_SETTLEMENT_FIELD in value
        or value.get(S1_GOLD_SETTLEMENT_VERSION_FIELD)
        == S1_GOLD_SETTLEMENT_CONTRACT_VERSION
    )


def project_s1_gold_settlement(value: object) -> object:
    """按当前合同白名单投影嵌套字段，未知字段不取得消费权限。"""
    if not isinstance(value, dict):
        return value
    projected: dict = {}
    if 'schema_version' in value:
        projected['schema_version'] = value['schema_version']
    team_gain = value.get('team_gain')
    if isinstance(team_gain, dict):
        projected['team_gain'] = {
            key: team_gain[key]
            for key in _TEAM_GAIN_FIELDS
            if key in team_gain
        }
    elif 'team_gain' in value:
        projected['team_gain'] = team_gain
    personal_changes = value.get('personal_changes')
    if isinstance(personal_changes, list):
        projected['personal_changes'] = [
            {
                key: row[key]
                for key in _PERSONAL_CHANGE_FIELDS
                if key in row
            }
            if isinstance(row, dict) else row
            for row in personal_changes
        ]
    elif 'personal_changes' in value:
        projected['personal_changes'] = personal_changes
    return projected


def rebase_s1_gold_settlement_for_log(
    value: object,
    *,
    event_index_offset: int,
) -> object:
    """兼容旧日志装配入口；金币不再消费剧情索引，仅投影实际分量。"""
    if type(event_index_offset) is not int or event_index_offset < 0:
        raise ValueError('event_index_offset 必须是非负整数')

    return project_s1_gold_settlement(value)


def _is_omittable_zero_personal_change(value: object) -> bool:
    """识别无消费效果的个人零金额占位行。"""
    if not isinstance(value, dict):
        return False
    amount, _ = normalize_ascii_integer(
        value.get('amount'),
        minimum=0,
        maximum=0,
    )
    return (
        type(amount) is int
        and amount == 0
    )


def _personal_repair_error(error: str) -> str:
    return f'{S1_GOLD_SETTLEMENT_PERSONAL_REPAIR_MARKER}: {error}'


def normalize_s1_gold_settlement_value(
    value: object,
    *,
    member_ids: list[int] | tuple[int, ...] | set[int],
    event_count: int,
    path: str = S1_GOLD_SETTLEMENT_FIELD,
) -> tuple[dict | None, list[str]]:
    """严格校验并规范化一个完整的当前金币结算包。"""
    projected = project_s1_gold_settlement(value)
    if not isinstance(projected, dict):
        return None, [f'{path}: 必须是对象']

    errors: list[str] = []
    schema_version, _ = normalize_ascii_integer(
        projected.get('schema_version'),
        minimum=S1_GOLD_SETTLEMENT_CONTRACT_VERSION,
        maximum=S1_GOLD_SETTLEMENT_CONTRACT_VERSION,
    )
    if schema_version != S1_GOLD_SETTLEMENT_CONTRACT_VERSION:
        errors.append(
            f'{path}.schema_version: 必须是 '
            f'{S1_GOLD_SETTLEMENT_CONTRACT_VERSION}'
        )
    team_gain = projected.get('team_gain')
    if not isinstance(team_gain, dict):
        errors.append(f'{path}.team_gain: 必须是对象')
    personal_changes = projected.get('personal_changes')
    if not isinstance(personal_changes, list):
        errors.append(f'{path}.personal_changes: 必须是数组')
    if errors:
        return None, errors

    # personal_changes 是稀疏账本。模型偶尔会为每名成员输出个人零金额占位；
    # 该行没有任何消费效果，可无损删除，不应为此重生成整段剧情。
    personal_changes = [
        row
        for row in personal_changes
        if not _is_omittable_zero_personal_change(row)
    ]

    team_amount = team_gain.get('amount')
    if type(team_amount) is not int or team_amount < 0:
        errors.append(f'{path}.team_gain.amount: 必须是非负整数')
    allowed_member_ids: set[int] = set()
    for raw_member_id in member_ids:
        member_id, _ = normalize_ascii_integer(raw_member_id, minimum=1)
        if type(member_id) is int and member_id > 0:
            allowed_member_ids.add(member_id)
    if len(personal_changes) > len(allowed_member_ids):
        errors.append(
            f'{path}.personal_changes: 条目数不得超过本队成员数 '
            f'{len(allowed_member_ids)}'
        )

    normalized_personal_changes: list[dict] = []
    seen_member_ids: set[int] = set()
    for index, row in enumerate(personal_changes):
        row_path = f'{path}.personal_changes[{index}]'
        if not isinstance(row, dict):
            errors.append(f'{row_path}: 必须是对象')
            continue
        member_id, _ = normalize_ascii_integer(row.get('member_id'), minimum=1)
        if type(member_id) is not int or member_id not in allowed_member_ids:
            errors.append(f'{row_path}.member_id: 必须是本队成员 id')
        elif member_id in seen_member_ids:
            errors.append(f'{row_path}.member_id: 成员 {member_id} 重复')
        else:
            seen_member_ids.add(member_id)

        amount = row.get('amount')
        if type(amount) is not int or amount == 0:
            errors.append(_personal_repair_error(
                f'{row_path}.amount: 必须是非零有符号整数'
            ))
        if (
            type(member_id) is int
            and member_id in allowed_member_ids
            and type(amount) is int
            and amount != 0
        ):
            normalized_personal_changes.append({
                'member_id': member_id,
                'amount': amount,
            })

    if errors:
        return None, errors
    return {
        'schema_version': S1_GOLD_SETTLEMENT_CONTRACT_VERSION,
        'team_gain': {
            'amount': team_amount,
        },
        'personal_changes': normalized_personal_changes,
    }, []


def normalize_s1_gold_settlement(
    result_data: dict,
    *,
    member_ids: list[int] | tuple[int, ...] | set[int],
    event_count: int,
    require_current_contract: bool = False,
) -> list[str]:
    """规范化顶层结算包；当前合同禁止再由 member_updates 写金币。"""
    if not isinstance(result_data, dict):
        return [f'{S1_GOLD_SETTLEMENT_ERROR_MARKER}: 结果不是对象']
    has_current_contract = (
        require_current_contract
        or has_current_s1_gold_settlement_contract(result_data)
    )
    if S1_GOLD_SETTLEMENT_FIELD not in result_data:
        return (
            [
                f'{S1_GOLD_SETTLEMENT_ERROR_MARKER}: '
                f'当前结果必须完整输出 {S1_GOLD_SETTLEMENT_FIELD}'
            ]
            if has_current_contract
            else []
        )

    mixed_member_ids = [
        update.get('id')
        for update in result_data.get('member_updates') or []
        if isinstance(update, dict) and 'gold_change' in update
    ]
    if mixed_member_ids:
        return [
            f'{S1_GOLD_SETTLEMENT_ERROR_MARKER}: 当前结果已使用 '
            f'{S1_GOLD_SETTLEMENT_FIELD}，member_updates 不得再输出 gold_change'
        ]

    normalized, errors = normalize_s1_gold_settlement_value(
        result_data.get(S1_GOLD_SETTLEMENT_FIELD),
        member_ids=member_ids,
        event_count=event_count,
    )
    if errors:
        return [f'{S1_GOLD_SETTLEMENT_ERROR_MARKER}: {error}' for error in errors]
    result_data[S1_GOLD_SETTLEMENT_FIELD] = normalized
    return []


def _gold_change_from_cents(cents: int) -> int | float:
    return cents // 100 if cents % 100 == 0 else cents / 100


def materialize_s1_gold_settlement(
    member_updates: list,
    member_ids: list[int],
    gold_settlement: dict,
    *,
    remainder_offset: int = 0,
) -> tuple[dict[int, int | float], dict]:
    """把权威团队/个人分量机械派生为逐成员最终 ``gold_change``。"""
    updates_by_id = {
        int(update['id']): update
        for update in member_updates
        if isinstance(update, dict) and type(update.get('id')) is int
    }
    ordered_ids: list[int] = []
    seen_ids: set[int] = set()
    for member_id in member_ids:
        if (
            type(member_id) is int
            and member_id in updates_by_id
            and member_id not in seen_ids
        ):
            ordered_ids.append(member_id)
            seen_ids.add(member_id)

    eligible_ids = [
        member_id
        for member_id in ordered_ids
        if not (
            updates_by_id[member_id].get('is_dead') is True
            or (
                'remaining_hp' in updates_by_id[member_id]
                and type(updates_by_id[member_id].get('remaining_hp')) is int
                and updates_by_id[member_id]['remaining_hp'] <= 0
            )
        )
    ]
    team_amount = int(gold_settlement['team_gain']['amount'])
    team_cents = team_amount * 100
    base_share, remainder = (
        divmod(team_cents, len(eligible_ids))
        if eligible_ids else (0, 0)
    )
    remainder_start = remainder_offset % len(eligible_ids) if eligible_ids else 0
    remainder_indexes = {
        (remainder_start + index) % len(eligible_ids)
        for index in range(remainder)
    }
    eligible_index = {member_id: index for index, member_id in enumerate(eligible_ids)}
    personal_cents_by_id = {
        int(row['member_id']): int(row['amount']) * 100
        for row in gold_settlement['personal_changes']
    }

    changes: dict[int, int | float] = {}
    member_components: list[dict] = []
    for member_id in ordered_ids:
        member_index = eligible_index.get(member_id)
        team_share_cents = (
            base_share + (1 if member_index in remainder_indexes else 0)
            if member_index is not None else 0
        )
        personal_cents = personal_cents_by_id.get(member_id, 0)
        final_cents = team_share_cents + personal_cents
        team_share = _gold_change_from_cents(team_share_cents)
        personal_change = _gold_change_from_cents(personal_cents)
        final_change = _gold_change_from_cents(final_cents)
        updates_by_id[member_id]['gold_change'] = final_change
        changes[member_id] = final_change
        member_components.append({
            'adventurer_id': member_id,
            'team_share': team_share,
            'personal_change': personal_change,
            'gold_change': final_change,
        })

    components = {
        'contract_version': S1_GOLD_SETTLEMENT_CONTRACT_VERSION,
        'team_gain': team_amount,
        'eligible_member_ids': eligible_ids,
        'member_components': member_components,
    }
    return changes, components
