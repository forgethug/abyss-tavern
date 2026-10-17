"""Activity world identity. Character assets retain their normal authority."""
from copy import deepcopy
import json

from flask import g, has_app_context


_CAMPAIGN_WORLD_KNOWLEDGE_RULE = '''
非核心队伍的 activity_world.dungeons 只含本次出发已公开的地图；未知路线保持未知，不从后台设定补造知情者或可传播线索来宣告隐藏地图。正常本地探索、战斗、知识获取与真实结算沿本场依据继续。
'''.strip()

_CAMPAIGN_WORLD_CONTEXT_RULE = """【独立活动世界】
本次地点和世界连续性由活动世界快照、当前地点及本活动已发布事实确定。首次进入从活动设定展开；再次进入继承本活动实际形成的状态。以全新的地方、生态、人物和模因构成本活动，不把旧副本的地点、势力、人物关系或旧世界事件迁入、换名复用，角色也不因旧探险记录预知这里的规则。
成员当前职业、能力、装备、特质与生命状态按冻结资产正常生效；描述中的既有功能和知识支持其真实范围内的观察与尝试，不证明这个新地方已发生过旧事件或属于旧世界。目标完成仍需本场行动满足任务条件；条件未成立时照实保留进展、代价及尚未解决的目标。
同一地图可由多支小队探索。只有冻结 campaign_dispatch_context 绑定本队时才执行对应的核心行动；没有该上下文时，从本地图当前仍成立的环境和可参与资料组织独立冒险与正常结算，不重复安排活动初始设定中的核心冲突，也不代裁其它小队的任务或补写其在途结果。初始设定只提供起点，活动内已经发生的世界与地点变化优先，人物取得新知识仍须有本场依据。
""" + _CAMPAIGN_WORLD_KNOWLEDGE_RULE

CAMPAIGN_WORLD_OUTLINE_RULE = _CAMPAIGN_WORLD_CONTEXT_RULE + """
本轮为快照中的活动地图建立可行动的客观局势，目标保持 target_dungeon，不选择 temporary_location；只规划场景、主体和行动机会，成败由实际行动形成。本活动内已输入的地点、世界线与主体继续按准确身份、版本和采纳权限使用。
"""

CAMPAIGN_WORLD_RULE = _CAMPAIGN_WORLD_CONTEXT_RULE + """
世界、地点、接力和战报继续使用本次冻结身份与修订号。目标地图保持本次 target_dungeon；world_fact_updates 可省略 scope_keys，提供时只使用快照中的活动地图名或本地图已冻结的地点范围；本模式不输出 temporary_location、私人 story_threads、relationship_changes、fate_progress、waiting_npc_updates。history_story_append 正常记录本场个人经历，资产成长与普通战利品按实际正文结算。
"""

CAMPAIGN_WORLD_REVIEW_RULE = _CAMPAIGN_WORLD_KNOWLEDGE_RULE + '\n' + """【独立活动世界审核边界】
未绑定 campaign_dispatch_context 的小队按普通探险审核，不要求核心任务战报或节点完成。若正文无冻结权限地执行他队核心冲突、重置本活动已经成立的结果，且无法仅修正派生字段，沿同一 other_unrepairable 与实际事件索引要求重生成；普通本地冒险与真实结算照常保留。
以 activity_world 快照、活动内地点/世界输入与本场 events 核对连续性。成员当前资产的真实功能与生命状态保持权威；旧资产经历不证明新地图具有旧地点、人物、势力关系或旧事件。事实结算沿既有字段纠正出口；若正文把未输入的旧副本世界事实当作本活动既成状态，且无法只修正派生字段，使用既有 other_unrepairable 与实际事件索引要求重生成。此检查不要求成功、不限制合法资产功能，也不以仅有同名或措辞相似判断旧世界迁入。
"""

CAMPAIGN_WORLD_MAINTENANCE_RULE = """【独立活动世界维护】
activity_world 是本活动的不可变地图目录。本轮仅保存本活动实际形成的新事实和演变，已有身份只沿本轮提供的准确 ID/版本更新。world_story_threads、world_actors 和 faction_profile_updates 的 scope_keys 使用本活动地图名，地点键只能使用 activity_world.allowed_location_scopes 中的准确值，不保存 s1_expedition_locus 临时域；既有角色或势力不提供 scope_keys 时继承原范围，新建时必须指定活动地图范围。人物、势力和线程关联只指向本活动已有身份或本批合法新建身份。保留当前成员的真实结算；旧副本事件、身份与关系不迁入本活动。
"""


def activity_maintenance_errors(maintained, world, existing_actors, existing_factions, *, allowed_location_scopes=()):
    if not world:
        return []
    allowed = set(world['dungeons'])
    for field, existing in (('world_actors', existing_actors), ('faction_profile_updates', existing_factions)):
        allowed_ids = {row['id'] for row in existing}
        for row in maintained.get(field) or []:
            if type(row.get('id')) is int and row['id'] not in allowed_ids:
                return ['activity_world_identity_not_frozen:' + field]
    for field in ('world_story_threads', 'world_actors', 'faction_profile_updates'):
        for row in maintained.get(field) or []:
            scopes = set(row.get('scope_keys') or [])
            if field != 'world_story_threads' and not scopes and type(row.get('id')) is int:
                continue
            local_scopes = {value for value in scopes if value.startswith(('s1_location_state:', 's1_expedition_locus:'))}
            if not local_scopes.issubset(set(allowed_location_scopes)):
                return ['activity_world_location_out_of_scope:' + field]
            map_scopes = scopes - local_scopes
            if not map_scopes or not map_scopes.issubset(allowed):
                return ['activity_world_scope_mismatch:' + field]
    return []


def campaign_world(dungeon, *, include_challenge=False):
    if not dungeon or not has_app_context():
        return None
    # Static names need no catalog SQL, preserving hot ordinary read paths.
    from .adventurer_s1_config_service import S1_DUNGEONS, S1_SPECIAL_DUNGEON_MAP, S1_HIDDEN_DUNGEON_BY_NAME
    if (dungeon in S1_SPECIAL_DUNGEON_MAP or dungeon in S1_HIDDEN_DUNGEON_BY_NAME
            or any(item['name'] == dungeon for item in S1_DUNGEONS)):
        return None
    cache = getattr(g, '_s1_campaign_worlds', None)
    if cache is None:
        cache = g._s1_campaign_worlds = {}
    if dungeon not in cache:
        from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository as Repo
        # Only positive entries are cached: creation can occur in this app context.
        cache.update(Repo.worlds_for_dungeons([dungeon]))
    world = deepcopy(cache.get(dungeon))
    if world and not include_challenge:
        # The immutable operator baseline enters generation and Observer only
        # through the local task's canonical challenge_context, never as a
        # second capability owner in outline/maintenance activity metadata.
        world.pop('challenge', None)
    return world


def activity_world_block(dungeon, expedition_log_id=None):
    if expedition_log_id:
        from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository
        log = S1CampaignDispatchRepository.log(expedition_log_id)
        world = (log.full_log or {}).get('activity_world') if log and log.target_dungeon == dungeon else None
    else:
        world = campaign_world(dungeon)
    if not world:
        return ''
    return '【活动世界快照 activity_world】\n' + json.dumps(world, ensure_ascii=False)


def activity_scope_errors(result, full_log):
    """Mechanical ownership only; narrative identity is reviewed by Observer."""
    world = full_log.get('activity_world') or {}
    if not world:
        return []
    allowed = set(world['dungeons'])
    from .s1_expedition_locus_service import EXPEDITION_LOCUS_LOG_FIELD
    locus = (full_log.get(EXPEDITION_LOCUS_LOG_FIELD) or {}).get('scope_key')
    if locus:
        allowed.add(locus)
    for location in (full_log.get('location_state_snapshot') or {}).get('locations') or []:
        if type(location.get('location_id')) is int:
            allowed.add(f"s1_location_state:{location['location_id']}")
    errors = []
    for index, fact in enumerate(result.get('world_fact_updates') or []):
        if not set(fact.get('scope_keys') or []).issubset(allowed):
            errors.append(f'activity_world_scope_mismatch:{index}')
    return errors


def project_activity_result(result, dungeon):
    if not campaign_world(dungeon) or not isinstance(result, dict):
        return result
    result = dict(result)
    for key in ('temporary_location', 'fate_progress', 'waiting_npc_updates'):
        result.pop(key, None)
    if isinstance(result.get('member_updates'), list):
        result['member_updates'] = [
            {key: value for key, value in row.items() if key not in ('story_threads', 'relationship_changes')}
            if isinstance(row, dict) else row for row in result['member_updates']
        ]
    return result
