"""One-run destinations with permanent, versioned location memory.

Matching/campaign ownership stays on target_dungeon. The destination owns only
physical location, location memory and dungeon-scoped asset applicability.
"""
from __future__ import annotations

from copy import deepcopy

TEMPORARY_LOCATION_FIELD = '_temporary_location'
TEMPORARY_LOCATION_NAMESPACE = '__s1_temporary_locations__'
TEMPORARY_LOCATION_PROPOSAL_FIELD = 'temporary_location'

TEMPORARY_LOCATION_RULE = """【本次独立临时地点】
本次临时地点是实际到访地，原 target_dungeon 只保留组队、基础等级与已冻结任务的归属，不表示仍处在原地图内。进入原因必须承接本次共同主线与已成立的移动或接触因果，地点不替换任务目标，也不预定结果。
本场地图、生态与空间范围按临时地点的权威现状解释，普通地牢内部探索规则只在未选择临时地点时适用；快照顶层的 is_explorable 与 identity_is_persistent 仍描述原匹配地牢，不保证临时入口可通行或永久保留。原地图的现场事实不随队伍移动，已获世界线使用权限也不自动证明其它主体与此地共址。
地点身份与当前状态以本次冻结的 location_id、location_name、revision 为准；已登记现状高于大纲候选，正文沿现状继续，不能重置已经消耗、破坏、关闭或留下的事物。后台地点记忆不自动成为角色知识。
入口仅服务本次剧情，不加入普通匹配池；保存地点不保证入口再次开放。后续因果支持时可重访，实际能否进入仍服从已保存现状。
首次登记的 current_state 为空时，正文应实际到访并用 location_state_changes 保存结尾可继承的完整现状，包括本场已经发生的重要经过与仍有效的后果；仅有大纲候选不算发生。已有现状未变化时继承原版本，有变化才更新。该地点的关联场景归入同一地点现状，不另造其它地点写入身份。
地点版本记录只使用冻结的 location_id 与 expected_revision。临时地点的入口变化记录在本地点，不修改原副本整体可探索性。副本限定物品和特质以实际到访地为适用范围，不从原组队副本继承触发资格。"""

TEMPORARY_LOCATION_OUTLINE_RULE = """【临时地点选择】
temporary_location 是可选对象或 null；不需要独立新地点时为 null。对象只包含 location_name 与 entry_basis，均为非空字符串：location_name 是稳定地点名（最多 100 字符）；entry_basis 描述本次共同主线为什么需要在该地点发生，以及与冻结剧情的移动、接触或任务关系。
只有本次输入允许临时地点、且现有剧情因果需要独立地点时才选择；可复用已在剧情中成立的稳定名称，也可创作需要的新地点，不为换景而另开目标。target_dungeon 仍原样回传为匹配与基础规则归属。原局部落点只在不选择临时地点时生效；原地图的局部当前态不因离开而改变。
后端将在正文前按稳定名称解析地点身份与已保存现状。大纲对该地点的新设想保持候选性质，不覆盖尚未读取的历史，也不宣告任务结果、入口永久开放或地点已写入世界。名称不使用固定副本名冒充临时地点，不用改名重置已经建立的地点。"""


def normalize_temporary_location_proposal(raw: object) -> dict | None:
    """Normalize empty/singleton wrappers without inventing a destination.

    Only a completely empty container/string means absence. Partial objects,
    nonempty strings and multiple records still require the owner's retry.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    if isinstance(raw, (dict, list)) and not raw:
        return None
    if isinstance(raw, list):
        if len(raw) != 1:
            raise ValueError('temporary_location_cardinality_invalid')
        raw = raw[0]
    if not isinstance(raw, dict):
        raise ValueError('temporary_location_not_object')
    name, basis = raw.get('location_name'), raw.get('entry_basis')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ValueError('temporary_location_name_invalid')
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError('temporary_location_entry_basis_missing')
    return {'location_name': name.strip(), 'entry_basis': basis.strip()}


def temporary_location_from_context(context: object) -> dict | None:
    """Read a server-frozen identity, never the AI's raw name proposal."""
    raw = context.get(TEMPORARY_LOCATION_FIELD) if isinstance(context, dict) else None
    if not isinstance(raw, dict) or raw.get('schema_version') != 1:
        return None
    if (
        type(raw.get('location_id')) is not int or raw['location_id'] <= 0
        or not isinstance(raw.get('location_name'), str) or not raw['location_name'].strip()
        or not isinstance(raw.get('base_dungeon'), str) or not raw['base_dungeon'].strip()
    ):
        return None
    return {
        'schema_version': 1,
        'location_id': raw['location_id'],
        'location_name': raw['location_name'],
        'base_dungeon': raw['base_dungeon'],
        'entry_basis': str(raw.get('entry_basis') or ''),
    }


def expedition_asset_dungeon(context: object, default_dungeon: str | None) -> str | None:
    destination = temporary_location_from_context(context)
    return destination['location_name'] if destination else default_dungeon


def resolve_outline_temporary_location(
    expedition_log_id: int | None, target_dungeon: str, outline: dict,
    *, generation_claim_token: str | None,
) -> dict:
    """Freeze once before narrative; recovery uses the same identity and snapshot."""
    proposal = normalize_temporary_location_proposal(outline.get(TEMPORARY_LOCATION_PROPOSAL_FIELD))
    result = deepcopy(outline)
    result.pop(TEMPORARY_LOCATION_FIELD, None)
    if TEMPORARY_LOCATION_PROPOSAL_FIELD in result:
        result[TEMPORARY_LOCATION_PROPOSAL_FIELD] = proposal
    if not expedition_log_id:
        if proposal:
            raise ValueError('temporary_location_requires_expedition_log')
        return result
    from db_repository.s1_temporary_location_repository import freeze_temporary_location

    frozen = freeze_temporary_location(
        expedition_log_id, target_dungeon, proposal,
        generation_claim_token=generation_claim_token,
    )
    if frozen:
        result[TEMPORARY_LOCATION_FIELD] = frozen
    return result


def render_temporary_location_snapshot(snapshot: dict) -> str:
    destination = temporary_location_from_context(snapshot)
    if not destination:
        return ''
    lines = ['【本次独立临时地点权威现状】',
             f"本次实际地点：{destination['location_name']}",
             f"进入缘由候选：{destination['entry_basis']}"]
    for item in snapshot.get('locations') or []:
        lines.append(
            f"[location_id:{item['location_id']} revision:{item['revision']}] "
            f"{item['location_name']}：{item.get('current_state') or '尚无已发布现状，本场实际到访后首次登记。'}"
        )
        if item.get('public_risk_notice'):
            lines.append(f"已传回酒馆的风险情报：{item['public_risk_notice']}")
    return '\n'.join(lines)


def validate_temporary_location_result(candidate: dict, full_log: dict) -> list[str]:
    destination = temporary_location_from_context(full_log)
    if not destination:
        return []
    changes = candidate.get('location_state_changes') or []
    if any(not isinstance(row, dict) or row.get('location_id') != destination['location_id']
           for row in changes):
        return ['临时地点变更必须使用本次冻结地点的精确 location_id']
    snapshot = full_log.get('location_state_snapshot') or {}
    primary = next((row for row in snapshot.get('locations', [])
                    if row.get('location_id') == destination['location_id']), None)
    if primary is None:
        return ['临时地点缺少冻结的权威现状']
    if not primary.get('current_state') and not changes:
        return ['首次到访的临时地点必须保存最终正文已成立的完整现状']
    return []
