"""百艺阁：旧卷传艺，以及 AI 驱动的量身授艺 / 指定技能磨炼。"""
from runtime_core.time import utc_now as _portable_utc_now
from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
from copy import deepcopy
from datetime import datetime, timedelta
from math import ceil
from typing import Optional, Tuple
from uuid import uuid4

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1SkillSchoolQueueEntry,
    AdventurerS1SkillSchoolSlot,
    AdventurerS1BuildingUsageLog,
    AdventurerS1CurrencyLog,
    AdventurerS1Skill,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService

_HANDLER_KEY = 'skill_school'
_CUSTOM_RESULT_TYPE = 'skill_training'
_CUSTOM_OPERATIONS = {'learn', 'advance'}
_USAGE_MODE_CLASSIC = 'classic'
_USAGE_MODE_CUSTOM = 'custom'
_CUSTOM_QUEUE_STATUS = 'skill_school_queue'
_RETRY_SNAPSHOT_VERSION = 1
_RETRY_EXTRA_FLAG = '_skill_training_retry'
_RETRY_OPPORTUNITY_ID_KEY = '_skill_training_retry_opportunity_id'
_RETRY_BLOCK_MESSAGE = '导师还替你留着上回没授成的补课，请先按原来的练法重试'

_DEFAULT_CATALOG = [
    {
        'code': 'sweeping_cleave',
        'name': '顺劈斩',
        'description': '横向挥砍横扫面前多名敌人，适合混战清小怪；狭窄空间容易误伤队友，对重甲前排单体效率一般。',
        'gold_cost': 100,
    },
    {
        'code': 'shield_bash',
        'name': '盾击',
        'description': '用盾牌猛击敌人面门或关节，短距离震慑、打乱架势；没有盾牌或对方体型过大时几乎无效。',
        'gold_cost': 100,
    },
    {
        'code': 'frost_armor',
        'name': '冰霜护甲',
        'description': '在自己或队友体表凝结一层寒霜护壳，短时减轻挥砍与刺击；对火焰、重击钝击与破冰类攻击防护有限。',
        'gold_cost': 100,
    },
    {
        'code': 'charge',
        'name': '冲锋',
        'description': '短途全速撞入敌阵，打穿薄弱阵线或撞倒单个目标；长距离、陷坑或反冲魔法会让冲锋变成自投罗网。',
        'gold_cost': 100,
    },
    {
        'code': 'aimed_shot',
        'name': '瞄准射击',
        'description': '屏息瞄准远处目标的要害精准一箭，射程远、单体伤害高；需要稳定站位与清晰视野，贴身混战或剧烈晃动时几乎无法命中。',
        'gold_cost': 100,
    },
    {
        'code': 'illuminate',
        'name': '照明术',
        'description': '凝聚光元素照亮周围大片区域，驱散黑暗与迷雾、暴露隐藏的敌人和机关；对光抗性极高的暗影生物几乎无效，持续时间有限。',
        'gold_cost': 100,
    },
    {
        'code': 'holy_smite',
        'name': '惩击',
        'description': '凝聚神圣之力打击单个目标，对亡灵与邪魔有额外震慑效果；对普通生物伤害平平，施术者精神不集中时容易打偏。',
        'gold_cost': 100,
    },
    {
        'code': 'trap_setup',
        'name': '陷阱布置',
        'description': '就地取材布设绊索、落石或尖刺陷阱，诱敌深入后触发伤害或限制行动；需要时间与隐蔽空间，被发现后陷阱会被绕开或拆除。',
        'gold_cost': 100,
    },
]


class S1SkillSchoolService:
    """百艺阁业务逻辑。"""

    @staticmethod
    def get_catalog() -> list[dict]:
        cfg = current_app.config.get('ADVENTURER_S1_SKILL_SCHOOL_CATALOG')
        if not isinstance(cfg, list) or not cfg:
            return list(_DEFAULT_CATALOG)
        return cfg

    @staticmethod
    def _catalog_by_code() -> dict[str, dict]:
        return {
            str(item.get('code') or '').strip(): item
            for item in S1SkillSchoolService.get_catalog()
            if str(item.get('code') or '').strip()
        }

    @staticmethod
    def _validate_building(
        inst: Optional[AdventurerS1BuildingInstance],
    ) -> Tuple[Optional[AdventurerS1BuildingInstance], Optional[str]]:
        if not inst:
            return None, '建筑不存在'
        if inst.status != 'built':
            return None, '建筑尚未建成'
        template = inst.template
        if not template or template.handler_key != _HANDLER_KEY:
            return None, '这里不是百艺阁'
        return inst, None

    @staticmethod
    def _get_user_adventurer(user_id: int) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        adv = (
            AdventurerS1.query.filter_by(owner_id=user_id)
            .filter(AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES))
            .order_by(AdventurerS1.id.asc())
            .first()
        )
        if not adv:
            return None, '你还没有冒险者，请先创建'
        return adv, None

    @staticmethod
    def _owned_skill_names(adv: AdventurerS1) -> set[str]:
        return {s.name for s in (adv.skill_items or [])}

    @staticmethod
    def get_custom_training_config() -> dict:
        """返回自定义研修的服务端权威配置。"""
        return {
            'direction_max_length': max(1, int(current_app.config.get(
                'ADVENTURER_S1_SKILL_SCHOOL_DIRECTION_MAX_LENGTH', 100,
            ))),
            'usage_duration_minutes': max(1, int(current_app.config.get(
                'ADVENTURER_S1_SKILL_SCHOOL_CUSTOM_DURATION_MINUTES', 60,
            ))),
            'stamina_cost': max(0, int(current_app.config.get(
                'ADVENTURER_S1_SKILL_SCHOOL_CUSTOM_STAMINA_COST', 40,
            ))),
            'cooldown_hours': max(0, int(current_app.config.get(
                'ADVENTURER_S1_SKILL_SCHOOL_CUSTOM_COOLDOWN_HOURS', 24,
            ))),
            'operation_config': {
                'learn': {
                    'gold_cost': max(0, int(current_app.config.get(
                        'ADVENTURER_S1_SKILL_SCHOOL_CUSTOM_LEARN_GOLD_COST', 300,
                    ))),
                },
                'advance': {
                    'gold_cost': max(0, int(current_app.config.get(
                        'ADVENTURER_S1_SKILL_SCHOOL_CUSTOM_ADVANCE_GOLD_COST', 600,
                    ))),
                },
            },
        }

    @staticmethod
    def has_retry_opportunity(adv: AdventurerS1) -> bool:
        """只判断私有凭据是否存在；结构异常也要阻止改练其它内容。"""
        return getattr(adv, 'skill_training_retry', None) is not None

    @staticmethod
    def retry_block_message() -> str:
        return _RETRY_BLOCK_MESSAGE

    @staticmethod
    def is_retry_training_extra(extra: object) -> bool:
        return isinstance(extra, dict) and extra.get(_RETRY_EXTRA_FLAG) is True

    @staticmethod
    def _normalize_retry_player_data(
        raw: object,
        *,
        operation: str,
        direction: str,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """投影补课会复用的 AI 输入；未知字段不进入 Prompt 或审核上下文。"""
        if not isinstance(raw, dict):
            return None, '补课凭据缺少授艺资料'
        if raw.get('operation') != operation or raw.get('direction') != direction:
            return None, '补课凭据与原授艺方向不一致'
        adventurer_raw = raw.get('adventurer')
        if not isinstance(adventurer_raw, dict):
            return None, '补课凭据缺少冒险者资料'

        level = adventurer_raw.get('level')
        profession = adventurer_raw.get('profession')
        if type(level) is not int or level < 1:
            return None, '补课凭据中的等级无效'
        if not isinstance(profession, str) or not profession.strip():
            return None, '补课凭据中的职业无效'

        traits_raw = adventurer_raw.get('active_traits')
        skills_raw = adventurer_raw.get('skills')
        if not isinstance(traits_raw, list) or not isinstance(skills_raw, list):
            return None, '补课凭据中的特质或技能清单无效'

        traits = []
        for trait_raw in traits_raw:
            if not isinstance(trait_raw, dict):
                return None, '补课凭据中的特质条目无效'
            name = trait_raw.get('name')
            description = trait_raw.get('description')
            if not isinstance(name, str) or not name.strip():
                return None, '补课凭据中的特质名称无效'
            if not isinstance(description, str):
                return None, '补课凭据中的特质描述无效'
            traits.append({
                'name': name,
                'description': description,
            })

        def normalize_skill(raw_skill: object) -> Tuple[Optional[dict], Optional[str]]:
            if not isinstance(raw_skill, dict):
                return None, '补课凭据中的技能条目无效'
            skill_id = raw_skill.get('id')
            name = raw_skill.get('name')
            description = raw_skill.get('description')
            equipped = raw_skill.get('equipped')
            recovery_minutes = raw_skill.get('recovery_minutes')
            recovering = raw_skill.get('recovering')
            if type(skill_id) is not int or skill_id <= 0:
                return None, '补课凭据中的技能标识无效'
            if not isinstance(name, str) or not name.strip():
                return None, '补课凭据中的技能名称无效'
            if not isinstance(description, str):
                return None, '补课凭据中的技能描述无效'
            if type(equipped) is not bool or type(recovering) is not bool:
                return None, '补课凭据中的技能状态无效'
            if type(recovery_minutes) is not int or recovery_minutes < 0:
                return None, '补课凭据中的技能恢复时间无效'
            return {
                'id': skill_id,
                'name': name,
                'description': description,
                'equipped': equipped,
                'recovery_minutes': recovery_minutes,
                'recovering': recovering,
            }, None

        skills = []
        skill_ids: set[int] = set()
        skill_names: set[str] = set()
        for skill_raw in skills_raw:
            skill, skill_err = normalize_skill(skill_raw)
            if skill_err:
                return None, skill_err
            if skill['id'] in skill_ids or skill['name'] in skill_names:
                return None, '补课凭据中的技能清单存在重复'
            skill_ids.add(skill['id'])
            skill_names.add(skill['name'])
            skills.append(skill)

        target_raw = raw.get('target_skill')
        target_skill = None
        if operation == 'learn':
            if target_raw is not None:
                return None, '学新本事的补课凭据不应包含旧招目标'
        else:
            target_skill, target_err = normalize_skill(target_raw)
            if target_err:
                return None, target_err
            frozen_skill = next(
                (skill for skill in skills if skill['id'] == target_skill['id']),
                None,
            )
            if frozen_skill != target_skill:
                return None, '补课凭据中的进阶目标与冻结技能清单不一致'

        return {
            'operation': operation,
            'direction': direction,
            'adventurer': {
                'level': level,
                'profession': profession,
                'active_traits': traits,
                'skills': skills,
            },
            'target_skill': target_skill,
        }, None

    @staticmethod
    def normalize_retry_opportunity(
        raw: object,
        *,
        building_instance_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """校验并白名单投影服务端私有补课凭据。"""
        if not isinstance(raw, dict):
            return None, '导师留下的补课凭据已经损坏，请联系酒馆管理员'
        if raw.get('version') != _RETRY_SNAPSHOT_VERSION:
            return None, '导师留下的补课凭据版本无法识别，请联系酒馆管理员'

        opportunity_id = raw.get('opportunity_id')
        source_usage_session_id = raw.get('source_usage_session_id')
        frozen_building_id = raw.get('building_instance_id')
        operation = raw.get('operation')
        direction = raw.get('direction')
        gold_cost = raw.get('gold_cost')
        stamina_cost = raw.get('stamina_cost')
        usage_duration_minutes = raw.get('usage_duration_minutes')
        created_at = raw.get('created_at')
        if not isinstance(opportunity_id, str) or not (1 <= len(opportunity_id) <= 64):
            return None, '导师留下的补课凭据缺少稳定标识'
        if not isinstance(source_usage_session_id, str) or not source_usage_session_id:
            return None, '导师留下的补课凭据缺少原课堂标识'
        if type(frozen_building_id) is not int or frozen_building_id <= 0:
            return None, '导师留下的补课凭据缺少百艺阁标识'
        if building_instance_id is not None and frozen_building_id != building_instance_id:
            return None, '这次补课不属于眼前这座百艺阁'
        if operation not in _CUSTOM_OPERATIONS:
            return None, '导师留下的补课凭据缺少研修方式'
        if not isinstance(direction, str) or not direction.strip():
            return None, '导师留下的补课凭据缺少原练法'
        if type(gold_cost) is not int or gold_cost < 0:
            return None, '导师留下的补课凭据中的束脩无效'
        if type(stamina_cost) is not int or stamina_cost < 0:
            return None, '导师留下的补课凭据中的精力消耗无效'
        if type(usage_duration_minutes) is not int or usage_duration_minutes < 1:
            return None, '导师留下的补课凭据中的课堂时长无效'
        if not isinstance(created_at, str) or not created_at:
            return None, '导师留下的补课凭据缺少留存时间'

        player_data, player_data_err = S1SkillSchoolService._normalize_retry_player_data(
            raw.get('player_data'),
            operation=operation,
            direction=direction,
        )
        if player_data_err:
            return None, player_data_err
        return {
            'version': _RETRY_SNAPSHOT_VERSION,
            'opportunity_id': opportunity_id,
            'source_usage_session_id': source_usage_session_id,
            'building_instance_id': frozen_building_id,
            'operation': operation,
            'direction': direction,
            'gold_cost': gold_cost,
            'stamina_cost': stamina_cost,
            'usage_duration_minutes': usage_duration_minutes,
            'player_data': player_data,
            'created_at': created_at,
        }, None

    @staticmethod
    def _build_active_lesson_player_data(
        adv: AdventurerS1,
        extra: dict,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """从入席当刻的权威状态构造冻结课堂；不得在 AI 启动时才回读。"""
        if not S1SkillSchoolService.is_custom_training_extra(extra):
            return None, '缺少自定义研修快照'
        operation = str(extra.get('operation') or '').strip()
        direction = str(extra.get('direction') or '').strip()
        if operation not in _CUSTOM_OPERATIONS or not direction:
            return None, '本次授艺的研修方式或方向无效'

        recovery_now = _portable_utc_now()
        skills = [
            {
                'id': skill.id,
                'name': skill.name,
                'description': skill.description or '',
                'equipped': bool(skill.equipped),
                'recovery_minutes': int(skill.recovery_minutes or 0),
                'recovering': bool(
                    skill.recovery_ends_at
                    and skill.recovery_ends_at > recovery_now
                ),
            }
            for skill in sorted(adv.skill_items or [], key=lambda item: item.id)
        ]
        target_skill = None
        if operation == 'advance':
            try:
                target_skill_id = int(extra.get('target_skill_id'))
            except (TypeError, ValueError):
                return None, '缺少进阶目标技能'
            target_skill = next(
                (skill for skill in skills if skill['id'] == target_skill_id),
                None,
            )
            if not target_skill:
                return None, '进阶目标技能已不存在'

        return {
            'operation': operation,
            'direction': direction,
            'adventurer': {
                'level': int(adv.level or 1),
                'profession': adv.character_class or '尚未形成明确职业',
                'active_traits': [
                    {'name': trait.name, 'description': trait.description or ''}
                    for trait in adv.traits
                    if not trait.is_archived
                ],
                'skills': skills,
            },
            'target_skill': target_skill,
        }, None

    @staticmethod
    def ensure_retry_opportunity_for_active_lesson(
        adv: AdventurerS1,
        building_instance_id: int,
        usage_session_id: str,
        *,
        player_data: Optional[dict] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """在持有冒险者行锁的短事务内，幂等冻结或复核当前课堂补课凭据。"""
        if (
            not adv
            or adv.status != 'building'
            or adv.building_instance_id != building_instance_id
        ):
            return None, '本次授艺会话已经失效'
        extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        if (
            str(extra.get('usage_session_id') or '') != usage_session_id
            or not S1SkillSchoolService.is_custom_training_extra(extra)
        ):
            return None, '本次授艺会话已经失效'

        existing = None
        if S1SkillSchoolService.has_retry_opportunity(adv):
            existing, existing_err = S1SkillSchoolService.normalize_retry_opportunity(
                adv.skill_training_retry,
                building_instance_id=building_instance_id,
            )
            if existing_err:
                return None, existing_err

        if S1SkillSchoolService.is_retry_training_extra(extra):
            opportunity_id = str(extra.get(_RETRY_OPPORTUNITY_ID_KEY) or '')
            if not existing or existing['opportunity_id'] != opportunity_id:
                return None, '补课凭据与当前会话不一致'
            snapshot = existing
        elif existing:
            if existing['source_usage_session_id'] != usage_session_id:
                return None, '已有另一堂补课尚未使用'
            snapshot = existing
        else:
            if player_data is None:
                player_data, player_data_err = (
                    S1SkillSchoolService._build_active_lesson_player_data(
                        adv,
                        extra,
                    )
                )
                if player_data_err:
                    return None, player_data_err
            operation = str(extra.get('operation') or '').strip()
            direction = str(extra.get('direction') or '').strip()
            normalized_player_data, player_data_err = (
                S1SkillSchoolService._normalize_retry_player_data(
                    player_data,
                    operation=operation,
                    direction=direction,
                )
            )
            if player_data_err:
                return None, player_data_err
            try:
                gold_cost = int(extra.get('gold_cost') or 0)
                stamina_cost = int(extra.get('stamina_cost') or 0)
                usage_duration_minutes = int(
                    extra.get('usage_duration_minutes') or 0,
                )
            except (TypeError, ValueError):
                return None, '本次授艺的资源快照无效'
            if gold_cost < 0 or stamina_cost < 0 or usage_duration_minutes < 1:
                return None, '本次授艺的资源快照无效'
            snapshot = {
                'version': _RETRY_SNAPSHOT_VERSION,
                'opportunity_id': uuid4().hex,
                'source_usage_session_id': usage_session_id,
                'building_instance_id': building_instance_id,
                'operation': operation,
                'direction': direction,
                'gold_cost': gold_cost,
                'stamina_cost': stamina_cost,
                'usage_duration_minutes': usage_duration_minutes,
                'player_data': normalized_player_data,
                'created_at': _portable_utc_now().isoformat(),
            }
            invariant_err = S1SkillSchoolService.validate_retry_target_unchanged(
                adv,
                snapshot,
            )
            if invariant_err:
                return None, invariant_err
            adv.skill_training_retry = snapshot
        return deepcopy(snapshot), None

    @staticmethod
    def freeze_retry_opportunity_for_session(
        adventurer_id: int,
        building_instance_id: int,
        usage_session_id: str,
        player_data: dict,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """AI worker 接手时复核入席快照，并兼容补冻结历史中的旧会话。"""
        from db_repository.s1_realm_repository import S1RealmRepository
        from plugins.adventurer_s1.services.s1_building_ai_ownership_service import (
            building_ai_owner_is_current,
        )

        db_manager.rollback()
        with db_manager.transaction():
            adv = S1RealmRepository.lock_adventurer(adventurer_id)
            if (
                not adv
                or adv.status != 'building'
                or adv.building_instance_id != building_instance_id
            ):
                return None, '本次授艺会话已经失效'
            extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
            if (
                str(extra.get('usage_session_id') or '') != usage_session_id
                or not building_ai_owner_is_current(extra)
            ):
                return None, '本次授艺会话已经失效'
            return S1SkillSchoolService.ensure_retry_opportunity_for_active_lesson(
                adv,
                building_instance_id,
                usage_session_id,
                player_data=player_data,
            )

    @staticmethod
    def _retry_target_invariant_error(
        adv: AdventurerS1,
        snapshot: dict,
    ) -> Optional[str]:
        if snapshot['operation'] != 'advance':
            return None
        target_snapshot = snapshot['player_data'].get('target_skill')
        if not isinstance(target_snapshot, dict):
            return '补课凭据缺少原本要磨炼的旧招'
        current = next(
            (
                skill
                for skill in (adv.skill_items or [])
                if skill.id == target_snapshot['id']
            ),
            None,
        )
        if not current:
            return '上回要磨炼的旧招已经不在本领册里，这次补课不能改用别的技能'
        if (
            current.name != target_snapshot['name']
            or (current.description or '') != target_snapshot['description']
            or int(current.recovery_minutes or 0) != target_snapshot['recovery_minutes']
        ):
            return '上回要磨炼的旧招已经发生变化，这次补课不能用在改变后的技能上'
        return None

    @staticmethod
    def validate_retry_target_unchanged(
        adv: AdventurerS1,
        snapshot: dict,
    ) -> Optional[str]:
        """补课发起与最终写回共用同一条旧招不可变校验。"""
        return S1SkillSchoolService._retry_target_invariant_error(adv, snapshot)

    @staticmethod
    def build_retry_public_state(
        adv: AdventurerS1,
        building_instance_id: int,
        *,
        gold_balance: int,
        current_stamina: int,
        dispatch_error: Optional[str],
    ) -> Optional[dict]:
        """只投影玩家可见补课资料，不暴露数据库 ID 或完整 AI 输入。"""
        if not S1SkillSchoolService.has_retry_opportunity(adv):
            return None
        snapshot, snapshot_err = S1SkillSchoolService.normalize_retry_opportunity(
            adv.skill_training_retry,
            building_instance_id=building_instance_id,
        )
        if snapshot_err:
            return {
                'can_retry': False,
                'reject_reason': snapshot_err,
            }

        target = snapshot['player_data'].get('target_skill')
        target_public = None
        if isinstance(target, dict):
            target_public = {
                'name': target['name'],
                'description': target['description'],
                'recovery_minutes': target['recovery_minutes'],
            }

        reject_reason = dispatch_error
        if reject_reason is None:
            reject_reason = S1SkillSchoolService.validate_retry_target_unchanged(
                adv,
                snapshot,
            )
        if reject_reason is None and current_stamina < snapshot['stamina_cost']:
            reject_reason = (
                f'精力不足（需要 {snapshot["stamina_cost"]}，当前 {current_stamina}）'
            )
        if reject_reason is None and gold_balance < snapshot['gold_cost']:
            reject_reason = (
                f'金币不足（需要 {snapshot["gold_cost"]}，当前 {gold_balance}）'
            )
        return {
            'operation': snapshot['operation'],
            'direction': snapshot['direction'],
            'target_skill': target_public,
            'gold_cost': snapshot['gold_cost'],
            'stamina_cost': snapshot['stamina_cost'],
            'usage_duration_minutes': snapshot['usage_duration_minutes'],
            'created_at': snapshot['created_at'],
            'can_retry': reject_reason is None,
            'reject_reason': reject_reason,
        }

    @staticmethod
    def consume_retry_opportunity(
        adv: AdventurerS1,
        extra: dict,
        usage_session_id: str,
    ) -> Optional[str]:
        """成功落库或明确语义拒绝时，只消费与当前会话对应的补课凭据。"""
        snapshot, snapshot_err = S1SkillSchoolService.normalize_retry_opportunity(
            adv.skill_training_retry,
            building_instance_id=adv.building_instance_id,
        )
        if snapshot_err:
            return snapshot_err
        if S1SkillSchoolService.is_retry_training_extra(extra):
            if snapshot['opportunity_id'] != str(
                extra.get(_RETRY_OPPORTUNITY_ID_KEY) or '',
            ):
                return '补课凭据与当前会话不一致'
        elif snapshot['source_usage_session_id'] != usage_session_id:
            return '授艺凭据与当前会话不一致'
        adv.skill_training_retry = None
        return None

    @staticmethod
    def retry_custom_training(
        building_instance_id: int,
        user_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """原样重开失败课堂：不排队、不占席、不等待，资源仍在本次发起时扣除。"""
        from db_repository.s1_realm_repository import S1RealmRepository
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
            get_live_stamina,
        )
        from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
            AdventurerS1StateTransitionCoordinator,
        )
        from plugins.adventurer_s1.services.s1_building_ai_ownership_service import (
            stamp_current_building_ai_owner,
        )

        adv, adv_err = S1SkillSchoolService._get_user_adventurer(user_id)
        if adv_err:
            return None, adv_err
        adventurer_id = int(adv.id)
        usage_session_id = uuid4().hex
        operation = ''
        snapshot = None
        try:
            db_manager.rollback()
            with db_manager.transaction():
                adv = S1RealmRepository.lock_adventurer(adventurer_id)
                if not adv or adv.owner_id != user_id:
                    return None, '只能操作自己拥有的冒险者'
                can_dispatch, dispatch_err = adv.can_dispatch()
                if not can_dispatch:
                    return None, dispatch_err

                inst = (
                    AdventurerS1BuildingInstance.query
                    .filter_by(id=building_instance_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                inst, inst_err = S1SkillSchoolService._validate_building(inst)
                if inst_err:
                    return None, inst_err
                if inst.disabled:
                    return None, '该建筑已被禁用'

                snapshot, snapshot_err = S1SkillSchoolService.normalize_retry_opportunity(
                    adv.skill_training_retry,
                    building_instance_id=building_instance_id,
                )
                if snapshot_err:
                    return None, snapshot_err
                invariant_err = S1SkillSchoolService.validate_retry_target_unchanged(
                    adv,
                    snapshot,
                )
                if invariant_err:
                    return None, invariant_err

                now = _portable_utc_now()
                max_stamina = int(current_app.config.get(
                    'ADVENTURER_S1_STAMINA_MAX', 100,
                ))
                current_stamina = get_live_stamina(
                    adv,
                    max_stamina=max_stamina,
                    now=now,
                )
                stamina_cost = snapshot['stamina_cost']
                if current_stamina < stamina_cost:
                    return None, (
                        f'精力不足（需要 {stamina_cost}，当前 {current_stamina}）；'
                        '补课仍替你留着'
                    )

                gold_cost = snapshot['gold_cost']
                operation = snapshot['operation']
                if gold_cost > 0:
                    action_label = (
                        '另学一门本事'
                        if operation == 'learn'
                        else f'磨炼「{snapshot["player_data"]["target_skill"]["name"]}」'
                    )
                    _, deduct_err = S1CurrencyService.deduct(
                        adv.id,
                        'gold',
                        float(gold_cost),
                        source_type=f'skill_school_custom_{operation}',
                        related_id=building_instance_id,
                        related_type='building_instance',
                        description=f'百艺阁导师补课：{action_label}',
                        auto_commit=False,
                    )
                    if deduct_err:
                        return None, f'{deduct_err}；补课仍替你留着'

                AdventurerS1StateTransitionCoordinator.leave_rest(
                    adv,
                    'building',
                    now,
                )
                adv.stamina = max(0, int(adv.stamina or 0) - stamina_cost)

                extra = {
                    'usage_session_id': usage_session_id,
                    'operation': operation,
                    'direction': snapshot['direction'],
                    'gold_cost': gold_cost,
                    'stamina_cost': stamina_cost,
                    'usage_duration_minutes': snapshot['usage_duration_minutes'],
                    _RETRY_EXTRA_FLAG: True,
                    _RETRY_OPPORTUNITY_ID_KEY: snapshot['opportunity_id'],
                }
                target = snapshot['player_data'].get('target_skill')
                if isinstance(target, dict):
                    extra.update({
                        'target_skill_id': target['id'],
                        'target_skill_name': target['name'],
                        'target_skill_description': target['description'],
                    })
                extra, _ = stamp_current_building_ai_owner(extra)
                adv.building_instance_id = building_instance_id
                # 补课免去原课堂时长；回收任务也应把它视为立即到期。
                adv.building_assigned_at = now - timedelta(
                    minutes=snapshot['usage_duration_minutes'],
                )
                adv.building_usage_extra = extra
        except Exception as exc:
            db_manager.rollback()
            return None, player_operation_failure(exc, '补课开始失败')

        # 外部任务投递不得跨持有数据库连接的事务。
        db_manager.remove()
        from services.async_task_service import AsyncTaskService

        payload = {
            'adventurer_id': adventurer_id,
            'building_instance_id': building_instance_id,
            'usage_session_id': usage_session_id,
        }
        submitted = False
        try:
            submitted = AsyncTaskService.submit_task('ai_skill_training', payload)
        except Exception:
            current_app.logger.exception(
                '百艺阁补课投递异常 adv=%s inst=%s',
                adventurer_id,
                building_instance_id,
            )
        if not submitted:
            from plugins.adventurer_s1.services.s1_building_ai_service import (
                _SKILL_TRAINING_FAILURE_NARRATIVE,
                _settle_skill_training_failure,
            )
            settled = _settle_skill_training_failure(
                adventurer_id,
                building_instance_id,
                usage_session_id,
                _SKILL_TRAINING_FAILURE_NARRATIVE,
            )
            if settled:
                return None, '导师暂时还开不了课，束脩和精力已退，原补课仍替你留着'
            return None, '导师暂时还开不了课，本次状态正等待系统恢复，请稍后查看'

        from plugins.adventurer_s1.services.adventurer_s1_service import (
            invalidate_adventurer_detail_cache,
            invalidate_buildings_list_cache,
        )
        invalidate_adventurer_detail_cache(adventurer_id)
        invalidate_buildings_list_cache()
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {
                'action': 'skill_school_retry_started',
                'reason': 'preserved_lesson_retry',
                'ids': [adventurer_id],
                'building_instance_id': building_instance_id,
            }, namespace='/')
        except Exception:
            pass
        return {
            'retry_started': True,
            'adventurer_id': adventurer_id,
            'building_instance_id': building_instance_id,
            'operation': operation,
            'usage_session_id': usage_session_id,
        }, None

    @staticmethod
    def is_custom_training_extra(extra: object) -> bool:
        """仅以结构化 operation 判定是否为自定义研修。"""
        return (
            isinstance(extra, dict)
            and isinstance(extra.get('operation'), str)
            and extra['operation'].strip() in _CUSTOM_OPERATIONS
        )

    @staticmethod
    def usage_mode_for_extra(extra: object) -> str:
        """按服务端固化的结构化参数区分旧卷传艺与导师指点占位池。"""
        if S1SkillSchoolService.is_custom_training_extra(extra):
            return _USAGE_MODE_CUSTOM
        return _USAGE_MODE_CLASSIC

    @staticmethod
    def get_usage_slot_limit() -> int:
        """返回两套独立占位池共同使用的百艺阁硬上限。

        此上限是玩法规则，不读取建筑实例的升级槽位加成，避免通用 Lv2
        ``usage_slots_bonus`` 把任一分池从 4 放大到 8。
        """
        return max(1, int(current_app.config.get(
            'ADVENTURER_S1_SKILL_SCHOOL_USAGE_SLOTS',
            4,
        )))

    @staticmethod
    def get_usage_counts(building_instance_id: int) -> dict[str, int]:
        """一次查询统计百艺阁两套独立占位池。"""
        rows = (
            AdventurerS1.query
            .with_entities(AdventurerS1.building_usage_extra)
            .filter(
                AdventurerS1.building_instance_id == building_instance_id,
                AdventurerS1.status == 'building',
            )
            .all()
        )
        counts = {
            _USAGE_MODE_CLASSIC: 0,
            _USAGE_MODE_CUSTOM: 0,
        }
        for row in rows:
            if S1SkillSchoolService.is_retry_training_extra(row[0]):
                continue
            mode = S1SkillSchoolService.usage_mode_for_extra(row[0])
            counts[mode] += 1
        return counts

    @staticmethod
    def get_custom_queue_state(
        building_instance_id: int,
        adventurer_id: Optional[int] = None,
    ) -> dict:
        """返回导师候场总人数及指定冒险者的位置，不暴露其他玩家资料。"""
        base_query = AdventurerS1SkillSchoolQueueEntry.query.filter_by(
            building_instance_id=building_instance_id,
        )
        waiting_count = base_query.count()
        position = None
        if adventurer_id is not None:
            entry = base_query.filter_by(adventurer_id=adventurer_id).first()
            if entry:
                ahead = base_query.filter(
                    (
                        AdventurerS1SkillSchoolQueueEntry.queued_at < entry.queued_at
                    )
                    | (
                        (
                            AdventurerS1SkillSchoolQueueEntry.queued_at
                            == entry.queued_at
                        )
                        & (AdventurerS1SkillSchoolQueueEntry.id < entry.id)
                    )
                ).count()
                position = int(ahead) + 1
        return {
            'waiting_count': int(waiting_count),
            'position': position,
            'is_waiting': position is not None,
        }

    @staticmethod
    def enqueue_custom_training(
        building_instance_id: int,
        adv: AdventurerS1,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """在已锁定冒险者与建筑的事务内加入 FIFO 候场队列。"""
        now = now or _portable_utc_now()
        stale = AdventurerS1SkillSchoolQueueEntry.query.filter_by(
            adventurer_id=adv.id,
        ).first()
        if stale:
            ok, err = db_manager.delete_instance(stale)
            if not ok:
                raise RuntimeError(f'清理旧候场记录失败: {err}')

        entry = AdventurerS1SkillSchoolQueueEntry(
            building_instance_id=building_instance_id,
            adventurer_id=adv.id,
            queued_at=now,
        )
        ok, err = db_manager.add(entry)
        if not ok:
            raise RuntimeError(f'加入导师候场失败: {err}')
        ok, err = db_manager.flush()
        if not ok:
            raise RuntimeError(f'加入导师候场失败: {err}')
        ahead = AdventurerS1SkillSchoolQueueEntry.query.filter(
            AdventurerS1SkillSchoolQueueEntry.building_instance_id == building_instance_id,
            (
                (AdventurerS1SkillSchoolQueueEntry.queued_at < now)
                | (
                    (AdventurerS1SkillSchoolQueueEntry.queued_at == now)
                    & (AdventurerS1SkillSchoolQueueEntry.id < entry.id)
                )
            ),
        ).count()
        return int(ahead) + 1

    @staticmethod
    def _queue_adventurer_lock_query(adventurer_id: int):
        """非阻塞锁定队首冒险者；锁冲突由晋升流程留待下一轮重试。"""
        return (
            AdventurerS1.query
            .filter_by(id=adventurer_id)
            .populate_existing()
            .with_for_update(skip_locked=True)
        )

    @staticmethod
    def promote_next_waiting(building_instance_id: int) -> Optional[int]:
        """若导师空闲，按 queued_at、id 原子送入队首；无可晋升者返回 None。"""
        while True:
            head = (
                AdventurerS1SkillSchoolQueueEntry.query
                .filter_by(building_instance_id=building_instance_id)
                .order_by(
                    AdventurerS1SkillSchoolQueueEntry.queued_at.asc(),
                    AdventurerS1SkillSchoolQueueEntry.id.asc(),
                )
                .first()
            )
            if not head:
                return None
            head_id = int(head.id)
            adventurer_id = int(head.adventurer_id)
            promoted = False
            stale_removed = False
            try:
                with db_manager.transaction():
                    adv = S1SkillSchoolService._queue_adventurer_lock_query(
                        adventurer_id,
                    ).first()
                    if adv is None:
                        # SKIP LOCKED 与记录不存在都会返回 None。若记录仍可见，说明
                        # 队首正由取消、结算或其它状态事务处理；保持严格 FIFO，本轮
                        # 不删除队首、也不越过他晋升后续候场者，交给下一次补扫重试。
                        adv_still_exists = (
                            AdventurerS1.query
                            .with_entities(AdventurerS1.id)
                            .filter_by(id=adventurer_id)
                            .first()
                            is not None
                        )
                        if adv_still_exists:
                            current_app.logger.info(
                                '导师候场晋升延后：队首冒险者正被其它事务处理 '
                                'inst=%s adv=%s',
                                building_instance_id,
                                adventurer_id,
                            )
                            return None
                    inst = (
                        AdventurerS1BuildingInstance.query
                        .filter_by(id=building_instance_id)
                        .populate_existing()
                        .with_for_update()
                        .first()
                    )
                    locked_head = (
                        AdventurerS1SkillSchoolQueueEntry.query
                        .filter_by(building_instance_id=building_instance_id)
                        .order_by(
                            AdventurerS1SkillSchoolQueueEntry.queued_at.asc(),
                            AdventurerS1SkillSchoolQueueEntry.id.asc(),
                        )
                        .populate_existing()
                        .with_for_update()
                        .first()
                    )
                    if not locked_head or int(locked_head.id) != head_id:
                        return None
                    if (
                        not adv
                        or not inst
                        or inst.status != 'built'
                        or adv.status != _CUSTOM_QUEUE_STATUS
                        or adv.building_instance_id != building_instance_id
                        or not S1SkillSchoolService.is_custom_training_extra(
                            adv.building_usage_extra,
                        )
                    ):
                        ok, err = db_manager.delete_instance(locked_head)
                        if not ok:
                            raise RuntimeError(f'清理失效候场记录失败: {err}')
                        stale_removed = True
                    else:
                        claimed, _ = S1SkillSchoolService.claim_usage_slot(
                            building_instance_id,
                            adv.id,
                            _USAGE_MODE_CUSTOM,
                            S1SkillSchoolService.get_usage_slot_limit(),
                            now=_portable_utc_now(),
                        )
                        if not claimed:
                            return None
                        extra = dict(adv.building_usage_extra or {})
                        extra.pop('queue_state', None)
                        extra.pop('queued_at', None)
                        adv.status = 'building'
                        adv.building_assigned_at = _portable_utc_now()
                        adv.building_usage_extra = extra
                        usage_session_id = str(
                            extra.get('usage_session_id') or '',
                        ).strip()
                        _, freeze_err = (
                            S1SkillSchoolService
                            .ensure_retry_opportunity_for_active_lesson(
                                adv,
                                building_instance_id,
                                usage_session_id,
                            )
                        )
                        if freeze_err:
                            raise RuntimeError(f'冻结导师课堂失败: {freeze_err}')
                        ok, err = db_manager.delete_instance(locked_head)
                        if not ok:
                            raise RuntimeError(f'移除已晋升候场记录失败: {err}')
                        promoted = True
            except Exception:
                current_app.logger.exception(
                    '导师候场晋升失败 inst=%s adv=%s',
                    building_instance_id,
                    adventurer_id,
                )
                return None

            if promoted:
                from plugins.adventurer_s1.services.adventurer_s1_service import (
                    invalidate_adventurer_detail_cache,
                    invalidate_buildings_list_cache,
                )
                invalidate_adventurer_detail_cache(adventurer_id)
                invalidate_buildings_list_cache()
                try:
                    from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                    socketio.emit('adventurer_changed', {
                        'action': 'skill_school_queue_promoted',
                        'reason': 'mentor_available',
                        'ids': [adventurer_id],
                        'building_instance_id': building_instance_id,
                    }, namespace='/')
                except Exception:
                    pass
                return adventurer_id
            if not stale_removed:
                return None

    @staticmethod
    def promote_waiting_custom_trainings_job() -> int:
        """调度器兜底：补扫所有导师候场队列，并显式提交每次晋升。"""
        building_ids = [
            int(row[0])
            for row in (
                AdventurerS1SkillSchoolQueueEntry.query
                .with_entities(AdventurerS1SkillSchoolQueueEntry.building_instance_id)
                .distinct()
                .all()
            )
        ]
        promoted = 0
        for building_id in building_ids:
            if S1SkillSchoolService.promote_next_waiting(building_id) is not None:
                promoted += 1
        return promoted

    @staticmethod
    def cancel_custom_queue(
        adventurer_id: int,
        building_instance_id: int,
        *,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """取消尚未入席的导师候场，原样退还预留金币与精力。"""
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if not is_admin and user_id is not None and adv.owner_id != user_id:
            return None, '只能操作自己拥有的冒险者'

        refunded_gold = 0
        refunded_stamina = 0
        try:
            with db_manager.transaction():
                adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
                if not adv:
                    return None, '冒险者不存在'
                AdventurerS1BuildingInstance.query.filter_by(
                    id=building_instance_id,
                ).with_for_update().first()
                entry = (
                    AdventurerS1SkillSchoolQueueEntry.query
                    .filter_by(
                        adventurer_id=adventurer_id,
                        building_instance_id=building_instance_id,
                    )
                    .with_for_update()
                    .first()
                )
                if (
                    not entry
                    or adv.status != _CUSTOM_QUEUE_STATUS
                    or adv.building_instance_id != building_instance_id
                ):
                    return None, '这名冒险者当前不在导师候场队列中'

                extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
                refunded_gold = max(0, int(extra.get('gold_cost') or 0))
                refunded_stamina = max(0, int(extra.get('stamina_cost') or 0))
                if refunded_gold > 0:
                    _, refund_err = S1CurrencyService.add(
                        adv.id,
                        'gold',
                        float(refunded_gold),
                        source_type='skill_school_queue_refund',
                        related_id=building_instance_id,
                        related_type='building_instance',
                        description='取消百艺阁导师候场，退回束脩',
                        auto_commit=False,
                    )
                    if refund_err:
                        raise RuntimeError(refund_err)
                if refunded_stamina > 0:
                    max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
                    adv.stamina = min(max_stamina, int(adv.stamina or 0) + refunded_stamina)

                ok, delete_err = db_manager.delete_instance(entry)
                if not ok:
                    raise RuntimeError(delete_err or '移除候场记录失败')
                from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
                AdventurerS1Service.enter_rest(adv)
                adv.building_instance_id = None
                adv.building_assigned_at = None
                adv.building_usage_extra = None
        except Exception as exc:
            return None, player_operation_failure(exc, '取消候场失败')

        from plugins.adventurer_s1.services.adventurer_s1_service import (
            AdventurerS1Service,
            invalidate_adventurer_detail_cache,
            invalidate_buildings_list_cache,
        )
        invalidate_adventurer_detail_cache(adventurer_id)
        invalidate_buildings_list_cache()
        S1SkillSchoolService.promote_next_waiting(building_instance_id)
        return {
            'adventurer': AdventurerS1Service._serialize_adventurer_for_client(
                adv,
                user_id,
                is_admin=is_admin,
            ),
            'gold_refunded': refunded_gold,
            'stamina_refunded': refunded_stamina,
        }, None

    @staticmethod
    def _ensure_usage_slots(
        building_instance_id: int,
        usage_slots: int,
    ) -> None:
        """在已锁定建筑行的事务内补齐两个独立槽位池。"""
        existing = {
            (str(mode), int(slot_index))
            for mode, slot_index in (
                AdventurerS1SkillSchoolSlot.query
                .with_entities(
                    AdventurerS1SkillSchoolSlot.usage_mode,
                    AdventurerS1SkillSchoolSlot.slot_index,
                )
                .filter_by(building_instance_id=building_instance_id)
                .all()
            )
        }
        missing = [
            AdventurerS1SkillSchoolSlot(
                building_instance_id=building_instance_id,
                usage_mode=mode,
                slot_index=slot_index,
            )
            for mode in (_USAGE_MODE_CLASSIC, _USAGE_MODE_CUSTOM)
            for slot_index in range(1, usage_slots + 1)
            if (mode, slot_index) not in existing
        ]
        if not missing:
            return
        ok, err = db_manager.add_all(missing)
        if not ok:
            raise RuntimeError(f'补齐百艺阁占位槽失败: {err}')

    @staticmethod
    def claim_usage_slot(
        building_instance_id: int,
        adventurer_id: int,
        usage_mode: str,
        usage_slots: int,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[bool, int]:
        """领取一个数据库槽位；调用方必须已锁住冒险者行和建筑行。

        槽位领取与冒险者进入建筑由外层事务一起提交。有限槽位、位置唯一约束和
        条件更新共同构成 Gunicorn 多 worker 下的最终容量闸门；同时按当前实际
        building 状态修复漏释放的陈旧占位。
        """
        if usage_mode not in (_USAGE_MODE_CLASSIC, _USAGE_MODE_CUSTOM):
            raise ValueError(f'未知百艺阁占位模式: {usage_mode}')
        usage_slots = max(0, int(usage_slots or 0))
        if usage_slots <= 0:
            return True, 0

        now = now or _portable_utc_now()
        S1SkillSchoolService._ensure_usage_slots(building_instance_id, usage_slots)

        # 先锁槽位再读取在场者：结算释放同样会更新槽位行。若两者并发，
        # 后到者必须等释放事务提交后再读取新的 adventurer 状态，不能拿旧快照回填。
        slots = (
            AdventurerS1SkillSchoolSlot.query
            .filter_by(building_instance_id=building_instance_id)
            .order_by(
                AdventurerS1SkillSchoolSlot.usage_mode.asc(),
                AdventurerS1SkillSchoolSlot.slot_index.asc(),
            )
            .with_for_update()
            .all()
        )

        occupant_rows = (
            AdventurerS1.query
            .with_entities(
                AdventurerS1.id,
                AdventurerS1.building_usage_extra,
            )
            .filter(
                AdventurerS1.building_instance_id == building_instance_id,
                AdventurerS1.status == 'building',
            )
            .order_by(AdventurerS1.id.asc())
            .all()
        )
        occupant_ids_by_mode = {
            _USAGE_MODE_CLASSIC: [],
            _USAGE_MODE_CUSTOM: [],
        }
        for occupant_id, usage_extra in occupant_rows:
            if S1SkillSchoolService.is_retry_training_extra(usage_extra):
                continue
            mode = S1SkillSchoolService.usage_mode_for_extra(usage_extra)
            occupant_ids_by_mode[mode].append(int(occupant_id))

        # 冒险者行已由调用方锁定且通过 can_dispatch；若历史异常遗留了别处槽位，
        # 先清掉再领取，唯一约束仍作为最终防重复占位护栏。当前建筑的槽位已在
        # identity map 中，直接改对象；其它建筑的陈旧槽位再做条件更新。
        for slot in slots:
            if slot.adventurer_id == adventurer_id:
                slot.adventurer_id = None
                slot.claimed_at = None
                slot.updated_at = now
        AdventurerS1SkillSchoolSlot.query.filter(
            AdventurerS1SkillSchoolSlot.adventurer_id == adventurer_id,
            AdventurerS1SkillSchoolSlot.building_instance_id != building_instance_id,
        ).update({
            AdventurerS1SkillSchoolSlot.adventurer_id: None,
            AdventurerS1SkillSchoolSlot.claimed_at: None,
            AdventurerS1SkillSchoolSlot.updated_at: now,
        }, synchronize_session=False)

        active_ids_by_mode = {
            mode: set(ids)
            for mode, ids in occupant_ids_by_mode.items()
        }

        # 先清理已离场或模式不一致的陈旧槽位。
        for slot in slots:
            if slot.adventurer_id is None:
                continue
            if int(slot.adventurer_id) not in active_ids_by_mode.get(slot.usage_mode, set()):
                slot.adventurer_id = None
                slot.claimed_at = None
                slot.updated_at = now

        # 部署前已在场的人可能没有槽位；按冒险者 ID 稳定回填，超出当前上限的
        # 历史占用不驱逐，但会让下方实际人数检查持续阻止新进入。
        represented = {
            int(slot.adventurer_id)
            for slot in slots
            if slot.adventurer_id is not None
        }
        for mode, occupant_ids in occupant_ids_by_mode.items():
            free_slots = [
                slot for slot in slots
                if (
                    slot.usage_mode == mode
                    and slot.slot_index <= usage_slots
                    and slot.adventurer_id is None
                )
            ]
            for occupant_id in occupant_ids:
                if occupant_id in represented or not free_slots:
                    continue
                slot = free_slots.pop(0)
                slot.adventurer_id = occupant_id
                slot.claimed_at = now
                slot.updated_at = now
                represented.add(occupant_id)

        ok, err = db_manager.flush()
        if not ok:
            raise RuntimeError(f'同步百艺阁占位槽失败: {err}')

        current_users = len(occupant_ids_by_mode[usage_mode])
        if current_users >= usage_slots:
            return False, current_users

        candidate_ids = [
            int(slot.id)
            for slot in slots
            if (
                slot.usage_mode == usage_mode
                and slot.slot_index <= usage_slots
                and slot.adventurer_id is None
            )
        ]
        for slot_id in candidate_ids:
            affected = AdventurerS1SkillSchoolSlot.query.filter(
                AdventurerS1SkillSchoolSlot.id == slot_id,
                AdventurerS1SkillSchoolSlot.adventurer_id.is_(None),
            ).update({
                AdventurerS1SkillSchoolSlot.adventurer_id: adventurer_id,
                AdventurerS1SkillSchoolSlot.claimed_at: now,
                AdventurerS1SkillSchoolSlot.updated_at: now,
            }, synchronize_session=False)
            if affected:
                return True, current_users + 1

        return False, max(current_users, usage_slots)

    @staticmethod
    def release_usage_slot(
        adventurer_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """在调用方事务内释放百艺阁槽位；对未占位冒险者为幂等空操作。"""
        if adventurer_id is None:
            return 0
        now = now or _portable_utc_now()
        return AdventurerS1SkillSchoolSlot.query.filter(
            AdventurerS1SkillSchoolSlot.adventurer_id == adventurer_id,
        ).update({
            AdventurerS1SkillSchoolSlot.adventurer_id: None,
            AdventurerS1SkillSchoolSlot.claimed_at: None,
            AdventurerS1SkillSchoolSlot.updated_at: now,
        }, synchronize_session=False)

    @staticmethod
    def format_usage_full_message(mode: str, current_users: int, usage_slots: int) -> str:
        if mode == _USAGE_MODE_CUSTOM:
            return f'导师指点名额已满（{current_users}/{usage_slots}）'
        return f'传艺席已满（{current_users}/{usage_slots}）'

    @staticmethod
    def format_custom_queue_priority_message(waiting_count: int) -> str:
        return f'导师已有 {max(1, int(waiting_count))} 人候场，请等待队首入席'

    @staticmethod
    def get_custom_cooldown_state(
        adventurer_id: int,
        building_instance_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> dict:
        """读取两种自定义研修共享的已消费 CD；经典传授不参与。"""
        now = now or _portable_utc_now()
        cooldown_hours = S1SkillSchoolService.get_custom_training_config()['cooldown_hours']
        if cooldown_hours <= 0:
            return {'remaining_seconds': 0, 'available_at': None}

        since = now - timedelta(hours=cooldown_hours)
        logs = (
            AdventurerS1BuildingUsageLog.query
            .filter(
                AdventurerS1BuildingUsageLog.adventurer_id == adventurer_id,
                AdventurerS1BuildingUsageLog.building_instance_id == building_instance_id,
                AdventurerS1BuildingUsageLog.used_at >= since,
            )
            .order_by(AdventurerS1BuildingUsageLog.used_at.desc())
            .all()
        )
        for usage_log in logs:
            result = usage_log.result if isinstance(usage_log.result, dict) else {}
            if result.get('type') != _CUSTOM_RESULT_TYPE:
                continue
            if (
                result.get('success') is not True
                and result.get('_counts_toward_cooldown') is not True
            ):
                continue
            available_at = usage_log.used_at + timedelta(hours=cooldown_hours)
            remaining = max(0, ceil((available_at - now).total_seconds()))
            if remaining > 0:
                return {
                    'remaining_seconds': remaining,
                    'available_at': available_at,
                }
        return {'remaining_seconds': 0, 'available_at': None}

    @staticmethod
    def prepare_custom_start(
        adv: AdventurerS1,
        inst: AdventurerS1BuildingInstance,
        operation: str,
        direction: str,
        target_skill_id=None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """校验并固化自定义研修参数，后续结算只信任这份快照。"""
        if S1SkillSchoolService.has_retry_opportunity(adv):
            return None, _RETRY_BLOCK_MESSAGE
        if not isinstance(operation, str):
            return None, '导师没听明白你想学新本事，还是磨炼旧招'
        operation = operation.strip()
        if operation not in _CUSTOM_OPERATIONS:
            return None, '先告诉导师：想另学一门本事，还是把旧招再磨一层'

        config = S1SkillSchoolService.get_custom_training_config()
        if not isinstance(direction, str):
            return None, '请用一小段话告诉导师想练什么'
        direction = direction.strip()
        max_length = config['direction_max_length']
        if not direction:
            return None, '先告诉导师你想往哪条路练'
        if len(direction) > max_length:
            return None, f'想法请收在 {max_length} 个字以内'

        target_skill = None
        if operation == 'learn':
            if target_skill_id not in (None, ''):
                return None, '学新本事时，不必再挑一门旧招'
        else:
            if target_skill_id in (None, ''):
                return None, '先挑一门想继续磨炼的旧招'
            if not isinstance(target_skill_id, int) or isinstance(target_skill_id, bool):
                return None, '这门招式认不出来，请重新挑选'
            target_skill = AdventurerS1Skill.query.filter_by(
                id=target_skill_id,
                adventurer_id=adv.id,
            ).first()
            if not target_skill:
                return None, '那门招式已经不在你的本领册里了'

        cooldown = S1SkillSchoolService.get_custom_cooldown_state(adv.id, inst.id)
        if cooldown['remaining_seconds'] > 0:
            remaining_minutes = max(1, ceil(cooldown['remaining_seconds'] / 60))
            return None, f'导师刚替你开过一回小灶，还需歇 {remaining_minutes} 分钟'

        operation_config = config['operation_config'][operation]
        normalized = {
            'usage_session_id': uuid4().hex,
            'operation': operation,
            'direction': direction,
            'gold_cost': int(operation_config['gold_cost']),
            'stamina_cost': int(config['stamina_cost']),
            'usage_duration_minutes': int(config['usage_duration_minutes']),
            'cooldown_hours': int(config['cooldown_hours']),
        }
        if target_skill:
            normalized.update({
                'target_skill_id': target_skill.id,
                'target_skill_name': target_skill.name,
                'target_skill_description': target_skill.description or '',
            })
        return normalized, None

    @staticmethod
    def prepare_start(adv: AdventurerS1, skill_code: str) -> Tuple[Optional[dict], Optional[str]]:
        """校验开始学习：技能存在、未习得、金币足够。"""
        if S1SkillSchoolService.has_retry_opportunity(adv):
            return None, _RETRY_BLOCK_MESSAGE
        code = str(skill_code or '').strip()
        if not code:
            return None, '先挑一门想学的招式'
        catalog_item = S1SkillSchoolService._catalog_by_code().get(code)
        if not catalog_item:
            return None, '这门招式不在阁里的旧卷上'

        skill_name = str(catalog_item.get('name') or '').strip()
        if not skill_name:
            return None, '这页旧卷字迹残缺，暂时学不了'

        if skill_name in S1SkillSchoolService._owned_skill_names(adv):
            return None, f'已习得「{skill_name}」'

        gold_cost = max(0, int(catalog_item.get('gold_cost') or 0))
        balance, bal_err = S1CurrencyService.get_balance(adv.id, 'gold')
        if bal_err:
            return None, bal_err
        if (balance or 0) < gold_cost:
            return None, f'金币不足（需要 {gold_cost}，当前 {int(balance or 0)}）'

        return catalog_item, None

    @staticmethod
    def grant_skill(
        adv: AdventurerS1,
        skill_code: str,
        inst: Optional[AdventurerS1BuildingInstance] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """学习完成后授予技能（金币已在 start 时扣除）。"""
        code = str(skill_code or '').strip()
        catalog_item = S1SkillSchoolService._catalog_by_code().get(code)
        if not catalog_item:
            return None, '这门招式不在阁里的旧卷上'

        skill_name = str(catalog_item.get('name') or '').strip()
        skill_desc = str(catalog_item.get('description') or '').strip()
        if not skill_name:
            return None, '这页旧卷字迹残缺，暂时学不了'

        existing = AdventurerS1Skill.query.filter_by(
            adventurer_id=adv.id, name=skill_name,
        ).first()
        if existing:
            return None, f'已习得「{skill_name}」'

        max_equipped = int(current_app.config.get('ADVENTURER_S1_MAX_EQUIPPED_SKILLS', 5))
        equipped_count = AdventurerS1Skill.query.filter_by(
            adventurer_id=adv.id, equipped=True,
        ).count()
        should_equip = equipped_count < max_equipped

        skill = AdventurerS1Skill(
            adventurer_id=adv.id,
            name=skill_name,
            description=skill_desc,
            equipped=should_equip,
        )
        ok, add_err = db_manager.add(skill)
        if not ok:
            return None, add_err or '添加技能失败'

        return {
            'skill': {
                'id': skill.id,
                'name': skill.name,
                'description': skill.description,
                'equipped': bool(skill.equipped),
            },
        }, None

    @staticmethod
    def _active_advanced_skill_pairs(
        adventurer_ids: set[int],
        candidate_pairs: set[tuple[int, str]],
    ) -> set[tuple[int, str]]:
        """返回已被成功且未撤销的百艺阁进阶消耗掉的旧技能身份。"""
        if not adventurer_ids or not candidate_pairs:
            return set()
        rows = (
            db_manager.query(
                AdventurerS1BuildingUsageLog.adventurer_id,
                AdventurerS1BuildingUsageLog.result,
            )
            .filter(AdventurerS1BuildingUsageLog.adventurer_id.in_(adventurer_ids))
            .all()
        )
        advanced_pairs: set[tuple[int, str]] = set()
        for adventurer_id, raw_result in rows:
            result = raw_result if isinstance(raw_result, dict) else {}
            if (
                result.get('type') != _CUSTOM_RESULT_TYPE
                or result.get('success') is not True
                or result.get('operation') != 'advance'
                or isinstance(result.get('undo'), dict)
            ):
                continue
            old_skill = result.get('old_skill')
            if not isinstance(old_skill, dict):
                continue
            old_name = str(old_skill.get('name') or '').strip()
            pair = (int(adventurer_id), old_name)
            if old_name and pair in candidate_pairs:
                advanced_pairs.add(pair)
        return advanced_pairs

    @staticmethod
    def repair_missing_skills() -> int:
        """定期扫描：扣了百艺阁旧卷传艺费用但未拿到技能、且已不在建筑中的冒险者，补发技能。

        只处理已释放的（status != 'building'）；仍在 building 态的由 auto_complete_expired_usage 负责。
        已被成功且未撤销的量身进阶原地替换掉的旧技能，不属于缺失，不得按历史扣费流水补回。
        返回补发数量。
        """
        # SQL 侧直接筛出"扣了费但技能子表里没有"的记录，避免全量加载到 Python。
        # description 含书名号中的技能名；前缀可随百艺阁玩家文案调整。
        rows_result, db_err = db_manager.execute_raw('''
            SELECT cl.id, cl.adventurer_id,
                   substr(cl.description,
                          strpos(cl.description, '「') + 1,
                          strpos(cl.description, '」') - strpos(cl.description, '「') - 1
                   ) AS skill_name
            FROM adventurer_s1_currency_logs cl
            JOIN adventurer_s1 adv ON adv.id = cl.adventurer_id
            WHERE cl.source_type = 'skill_school_learn'
              AND strpos(cl.description, '「') > 0
              AND strpos(cl.description, '」') > strpos(cl.description, '「')
              AND NOT (adv.status = 'building' AND adv.building_instance_id IS NOT NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM adventurer_s1_skills s
                  WHERE s.adventurer_id = cl.adventurer_id
                    AND s.name = substr(cl.description,
                                        strpos(cl.description, '「') + 1,
                                        strpos(cl.description, '」') - strpos(cl.description, '「') - 1)
              )
        ''')
        if db_err:
            current_app.logger.warning('技能书阁补发查询失败: %s', db_err)
            return 0
        rows = rows_result.fetchall() if rows_result else []
        if not rows:
            return 0

        # 去重：同一个 adventurer 可能有多条重复扣费日志指向同一技能
        need_pairs: list[tuple[int, str]] = []
        seen: set[tuple[int, str]] = set()
        for row in rows:
            pair = (int(row[1]), str(row[2]))  # (adventurer_id, skill_name)
            if pair not in seen:
                seen.add(pair)
                need_pairs.append(pair)

        if not need_pairs:
            return 0

        # 与百艺阁进阶、申诉撤回和其他 scheduler worker 统一锁冒险者；
        # 获锁后重新读取进阶日志与当前技能，关闭候选查询到写入之间的竞态窗口。
        adv_ids = {pair[0] for pair in need_pairs}
        advs = {
            adv.id: adv
            for adv in (
                AdventurerS1.query
                .filter(AdventurerS1.id.in_(adv_ids))
                .order_by(AdventurerS1.id.asc())
                .with_for_update()
                .all()
            )
        }
        candidate_pairs = set(need_pairs)
        advanced_pairs = S1SkillSchoolService._active_advanced_skill_pairs(
            set(advs), candidate_pairs,
        )
        equipped_counts: dict[int, int] = {}
        existing_pairs: set[tuple[int, str]] = set()
        for skill in AdventurerS1Skill.query.filter(
            AdventurerS1Skill.adventurer_id.in_(set(advs)),
        ).all():
            existing_pairs.add((int(skill.adventurer_id), str(skill.name or '')))
            if skill.equipped:
                equipped_counts[skill.adventurer_id] = (
                    equipped_counts.get(skill.adventurer_id, 0) + 1
                )

        catalog = {item.get('name'): item for item in S1SkillSchoolService.get_catalog() if item.get('name')}
        max_equipped = int(current_app.config.get('ADVENTURER_S1_MAX_EQUIPPED_SKILLS', 5))
        new_skills: list[AdventurerS1Skill] = []
        skipped_advanced = 0

        for adv_id, skill_name in need_pairs:
            adv = advs.get(adv_id)
            pair = (adv_id, skill_name)
            if not adv or pair in existing_pairs:
                continue
            if pair in advanced_pairs:
                skipped_advanced += 1
                continue

            catalog_item = catalog.get(skill_name)
            skill_desc = str(catalog_item.get('description', '')) if catalog_item else ''
            equipped_now = equipped_counts.get(adv_id, 0)
            should_equip = equipped_now < max_equipped

            new_skills.append(AdventurerS1Skill(
                adventurer_id=adv_id,
                name=skill_name,
                description=skill_desc,
                equipped=should_equip,
            ))
            if should_equip:
                equipped_counts[adv_id] = equipped_now + 1
            current_app.logger.info(
                '技能书阁补发技能 adv=%s skill=%s', adv_id, skill_name,
            )

        if skipped_advanced:
            current_app.logger.debug(
                '百艺阁补回扫描忽略已进阶旧技能 count=%s', skipped_advanced,
            )

        if not new_skills:
            # 已持有或已进阶的候选也曾取得行锁，显式提交只读事务以立即释放。
            ok, err = db_manager.commit()
            if not ok:
                current_app.logger.warning('百艺阁补发空批次提交失败: %s', err)
            return 0

        # 调度器包装层在 finally 会 db.session.remove()（回滚未提交事务），必须显式提交。
        ok, err = db_manager.add_all(new_skills)
        if not ok:
            current_app.logger.warning('技能书阁补发批量写入失败: %s', err)
            db_manager.remove()
            return 0
        ok, err = db_manager.commit()
        if not ok:
            current_app.logger.warning('技能书阁补发提交失败: %s', err)
            return 0
        return len(new_skills)

    @staticmethod
    def list_teachable(
        building_instance_id: int,
        user_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        inst, err = S1SkillSchoolService._validate_building(
            AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id),
        )
        if err:
            return None, err

        adv, adv_err = S1SkillSchoolService._get_user_adventurer(user_id)
        adv_summary = None
        gold_balance = 0
        owned_names: set[str] = set()
        owned_skills: list[dict] = []
        retry_pending = bool(
            adv and S1SkillSchoolService.has_retry_opportunity(adv)
        )

        config = BuildingSettlementService.get_reward_config(inst)
        usage_duration_minutes = int(config.get('usage_duration_minutes') or 60)
        usage_slots = S1SkillSchoolService.get_usage_slot_limit()
        usage_counts = S1SkillSchoolService.get_usage_counts(inst.id)
        classic_users = usage_counts[_USAGE_MODE_CLASSIC]
        custom_users = usage_counts[_USAGE_MODE_CUSTOM]

        adv_busy_elsewhere = bool(
            adv and (
                adv.status == _CUSTOM_QUEUE_STATUS
                or (
                    adv.status == 'building'
                    and adv.building_instance_id != inst.id
                )
            ),
        )
        adv_learning_here = bool(
            adv and adv.status == 'building' and adv.building_instance_id == inst.id,
        )
        classic_slots_full = (
            usage_slots > 0
            and classic_users >= usage_slots
            and not adv_learning_here
        )
        custom_queue = S1SkillSchoolService.get_custom_queue_state(
            inst.id,
            adv.id if adv else None,
        )
        queued_request = None
        if (
            adv and custom_queue['is_waiting']
            and adv.status == _CUSTOM_QUEUE_STATUS
            and adv.building_instance_id == inst.id
            and S1SkillSchoolService.is_custom_training_extra(adv.building_usage_extra)
        ):
            extra = adv.building_usage_extra
            # 仅返回本人提交的练法，不透传占位、费用或结算内部参数。
            queued_request = {
                'operation': extra['operation'].strip(),
                'direction': extra.get('direction') if isinstance(extra.get('direction'), str) else '',
                'target_skill_name': (
                    extra.get('target_skill_name')
                    if isinstance(extra.get('target_skill_name'), str) else None
                ),
            }

        if adv:
            balance, bal_err = S1CurrencyService.get_balance(adv.id, 'gold')
            if bal_err:
                return None, bal_err
            gold_balance = int(balance or 0)
            owned_names = S1SkillSchoolService._owned_skill_names(adv)
            owned_skills = [
                {
                    'id': skill.id,
                    'name': skill.name,
                    'description': skill.description or '',
                    'equipped': bool(skill.equipped),
                }
                for skill in sorted(adv.skill_items or [], key=lambda item: item.id)
            ]
            adv_summary = {
                'id': adv.id,
                'name': adv.name,
                'gold': gold_balance,
            }

        skills = []
        for item in S1SkillSchoolService.get_catalog():
            code = str(item.get('code') or '').strip()
            name = str(item.get('name') or '').strip()
            if not code or not name:
                continue
            gold_cost = max(0, int(item.get('gold_cost') or 0))
            learned = name in owned_names
            can_learn = (
                bool(adv)
                and not learned
                and gold_balance >= gold_cost
                and not adv_busy_elsewhere
                and not adv_learning_here
                and not classic_slots_full
                and not retry_pending
            )
            reject_reason = None
            if not adv:
                reject_reason = adv_err
            elif learned:
                reject_reason = f'已习得「{name}」'
            elif retry_pending:
                reject_reason = _RETRY_BLOCK_MESSAGE
            elif adv_learning_here:
                reject_reason = '正在阁里学另一门招'
            elif adv_busy_elsewhere:
                reject_reason = '冒险者正在其他建筑中'
            elif classic_slots_full:
                reject_reason = f'阁里的传艺席都坐满了（{classic_users}/{usage_slots}）'
            elif gold_balance < gold_cost:
                reject_reason = f'金币不足（需要 {gold_cost}，当前 {gold_balance}）'
            skills.append({
                'code': code,
                'name': name,
                'description': str(item.get('description') or '').strip(),
                'gold_cost': gold_cost,
                'learned': learned,
                'can_learn': can_learn,
                'reject_reason': reject_reason,
            })

        custom_config = S1SkillSchoolService.get_custom_training_config()
        cooldown_state = {'remaining_seconds': 0, 'available_at': None}
        current_stamina = 0
        dispatch_error = adv_err
        if adv:
            cooldown_state = S1SkillSchoolService.get_custom_cooldown_state(adv.id, inst.id)
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
            current_stamina = get_live_stamina(
                adv,
                max_stamina=int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100)),
            )
            can_dispatch, dispatch_error = adv.can_dispatch()
            if can_dispatch:
                dispatch_error = None

        retry_opportunity = None
        if adv and retry_pending:
            retry_opportunity = S1SkillSchoolService.build_retry_public_state(
                adv,
                inst.id,
                gold_balance=gold_balance,
                current_stamina=current_stamina,
                dispatch_error=dispatch_error,
            )

        shared_custom_error = None
        if not adv:
            shared_custom_error = adv_err
        elif retry_pending:
            shared_custom_error = _RETRY_BLOCK_MESSAGE
        elif adv_learning_here:
            shared_custom_error = '导师正在带你练功'
        elif custom_queue['is_waiting']:
            shared_custom_error = f'正在候场，前面还有 {max(0, custom_queue["position"] - 1)} 人'
        elif dispatch_error:
            shared_custom_error = dispatch_error
        elif cooldown_state['remaining_seconds'] > 0:
            remaining_minutes = max(1, ceil(cooldown_state['remaining_seconds'] / 60))
            shared_custom_error = f'导师刚替你开过一回小灶，还需歇 {remaining_minutes} 分钟'
        elif current_stamina < custom_config['stamina_cost']:
            shared_custom_error = (
                f'精力不足（需要 {custom_config["stamina_cost"]}，当前 {current_stamina}）'
            )

        custom_operations = {}
        for operation, operation_config in custom_config['operation_config'].items():
            reject_reason = shared_custom_error
            gold_cost = int(operation_config['gold_cost'])
            if reject_reason is None and operation == 'advance' and not owned_skills:
                reject_reason = '还没有能继续磨炼的旧招'
            elif reject_reason is None and gold_balance < gold_cost:
                reject_reason = f'金币不足（需要 {gold_cost}，当前 {gold_balance}）'
            custom_operations[operation] = {
                'gold_cost': gold_cost,
                'can_start': reject_reason is None,
                'reject_reason': reject_reason,
            }

        return {
            'building_code': inst.template.code if inst.template else _HANDLER_KEY,
            'usage_duration_minutes': usage_duration_minutes,
            'helper_slots': usage_slots,
            'current_users': classic_users,
            'adventurer': adv_summary,
            'skills': skills,
            'custom_training': {
                'direction_max_length': custom_config['direction_max_length'],
                'usage_duration_minutes': custom_config['usage_duration_minutes'],
                'stamina_cost': custom_config['stamina_cost'],
                'cooldown_hours': custom_config['cooldown_hours'],
                'cooldown_remaining_seconds': cooldown_state['remaining_seconds'],
                'helper_slots': usage_slots,
                'current_users': custom_users,
                'queue': custom_queue,
                'queued_request': queued_request,
                'operation_config': custom_operations,
                'skills': owned_skills,
                'retry_opportunity': retry_opportunity,
            },
        }, None
