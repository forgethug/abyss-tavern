"""深渊酒馆 S1 数据模型 —— 在 adventurer 插件基础上的"中度规范化"优化版结构。

与旧 adventurer 表（models 见 plugins/adventurer/models.py）相比，主要优化点：

1. 主表 ``adventurer_s1`` 只保留标量"热字段"。旧 ``adventurers`` 表把 warehouse(37KB)
   / traits(32KB) / equipment(13KB) / skills / investors 全塞在单行 JSON 里，单行平均
   ~80KB，导致列表查询一次性拉取大量 TOAST jsonb（5-8 秒慢查询）。这里把它们各自拆成
   独立子表，列表查询只读主表热字段，详情/仓库按需再查。
2. 装备 / 仓库物品的 ``desc`` 不再每个冒险者各存一份：通用描述统一来自图鉴表
   ``adventurer_s1_items``；只有"升级/附魔/改造"出来的个性化描述才写进 ``custom_desc``
   覆盖，避免上万件普通物品重复存描述。
3. 探险日志 ``status`` 从 ``full_log`` JSON 里提升为独立索引列。旧实现想找"卡在
   generating 的日志"只能全表拉出来在 Python 侧过滤 JSON，这里直接 SQL 走索引。
4. 特质改为纯按冒险者归属的实例表，并补齐 ``is_archived`` 等结构化列；旧的全局
   ``adventurer_traits`` 定义表在"特质按角色自己为准"后已无必要，S1 不再保留。
5. 全部表补齐 ``updated_at``（旧表只有 ``created_at``），符合项目模型规范。
"""
from runtime_core.time import utc_now as _portable_utc_now

import json
from datetime import datetime

from flask import current_app
from sqlalchemy.orm import deferred
from extensions import db
from plugins.adventurer_s1.cold_storage import ColdCompressedJSON, ColdCompressedText, ColdExpeditionLogJSON
from plugins.adventurer_s1.s1_item_types import S1_ITEM_TYPE_COMPANION
from utils.time_utils import to_utc_iso

# locked 阶段须管理员点「开放玩家捐献」后才对玩家可见（与宿舍楼相同机制）
_BUILDING_ADMIN_GATED_DONATION_CODES = frozenset()


def _clip_model_text(value, limit: int) -> str:
    text = str(value or '').strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def _round_money(value):
    """金额统一保留 2 位小数，None 视为 0.0。"""
    return round(float(value or 0), 2)


class AdventurerS1(db.Model):
    """冒险者主表：只保留标量热字段，重数据拆到子表。"""

    __tablename__ = 'adventurer_s1'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    gender = db.Column(db.String(10), nullable=False, default='未知')
    character_class = db.Column(db.String(50), nullable=True)  # 职业：创建时不选，后续在探险中随机获得
    is_player_created = db.Column(db.Boolean, nullable=False, default=False)  # 是否玩家自建（区别于系统自动生成）
    initial_rename_available = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )  # 抽中时因姓名冲突改为内部占位名；玩家仍有一次初始命名机会
    level = db.Column(db.Integer, nullable=False, default=1)
    # resting/in_party/exploring/activity/skill_school_queue/building_queue/
    # building/dead/archived；idle 为历史兼容值。
    status = db.Column(db.String(20), nullable=False, default='resting', index=True)
    hp = db.Column(db.Integer, nullable=False, default=100)
    max_hp = db.Column(db.Integer, nullable=False, default=100)
    strength = db.Column(db.Integer, nullable=False, default=10)
    agility = db.Column(db.Integer, nullable=False, default=10)
    intelligence = db.Column(db.Integer, nullable=False, default=10)
    luck = db.Column(db.Integer, nullable=False, default=10)
    personality = db.Column(db.String(100), nullable=False, default='')  # 性格，逗号分隔
    gold = db.Column(db.Float, nullable=False, default=100.0)
    exp = db.Column(db.Integer, nullable=False, default=0)
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    # 一区/二区是账号准入归属；第一阶段不改变世界、市场或组队等共享玩法数据。
    realm_code = db.Column(
        db.String(20), nullable=False, default='region_1',
        server_default=db.text("'region_1'"), index=True,
    )
    # 纹章小队只提供名片装饰，与探险组队 AdventurerS1Party 完全独立。
    squad_id = db.Column(
        db.Integer,
        db.ForeignKey(
            'adventurer_s1_squads.id',
            name='fk_adventurer_s1_squad_id',
            ondelete='SET NULL',
            use_alter=True,
        ),
        nullable=True,
        index=True,
    )
    active_title_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_titles.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    party_preference = db.Column(db.String(20), nullable=False, default='manual')  # auto/manual/idle/rest
    recovery_time = db.Column(db.DateTime, nullable=True)
    rest_start_time = db.Column(db.DateTime, nullable=True)
    trait_recovery_rest_seconds = db.Column(db.Integer, nullable=False, default=0)  # 累计宿舍床位静养秒数（跨休息段；与 rest_start_time 本次段相加）
    latest_score = db.Column(db.Float, nullable=True)
    praise_count = db.Column(db.Integer, nullable=False, default=0)
    closure_choice = db.Column(db.String(20), nullable=True)  # retire/defend/null
    closure_choice_at = db.Column(db.DateTime, nullable=True)
    closure_resolution = db.Column(db.String(20), nullable=True)
    _retired_dm_instruction_revision = db.Column('dm_instruction_revision', db.Integer, nullable=False, default=0)
    # 精力系统
    stamina = db.Column(db.Integer, nullable=False, default=100)  # 当前精力值
    stamina_updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)  # 上次精力更新时间
    hp_updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)  # 上次生命值更新时间（宿舍休息惰性回血）
    # 建造系统
    building_instance_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_building_instances.id', ondelete='SET NULL'), nullable=True, index=True)
    building_assigned_at = db.Column(db.DateTime, nullable=True)  # 开始建造的时间
    building_usage_extra = db.Column(db.JSON, nullable=True)  # 建筑使用时的额外参数（如治疗所选择的特质）
    skill_training_retry = db.Column(db.JSON, nullable=True)  # 百艺阁失败补课的服务端冻结快照
    # 训练系统：累计已用训练次数（三个训练场共用一池；上限 = TRAINING_CAP_BASE + 等级 × TRAINING_CAP_PER_LEVEL）
    training_count = db.Column(db.Integer, nullable=False, default=0)
    # 用餐加成：下次探险一次性临时数值，出发后消耗（见 s1_dining_service）
    expedition_buff = db.Column(db.JSON, nullable=True)
    # 宿舍区床位分配
    dorm_room_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_dorm_rooms.id', ondelete='SET NULL'), nullable=True, index=True)
    bed_index = db.Column(db.Integer, nullable=True)  # 0-based 床位编号
    last_bed_swap_at = db.Column(db.DateTime, nullable=True)  # 上次换床位时间（48h 冷却）
    # history_story 是单列长文本，PG TOAST 会自动外联存储，不属于列表慢查询的元凶，保留在主表。
    history_story = db.Column(db.Text, nullable=False, default='')
    # 保险系统：各保单独立购买，可同时生效；下次探险结算后分别判定并消耗
    insurance_active = db.Column(db.Boolean, nullable=False, default=False)
    empty_return_insurance_active = db.Column(db.Boolean, nullable=False, default=False)
    comrade_memorial_insurance_active = db.Column(db.Boolean, nullable=False, default=False)
    no_gold_insurance_active = db.Column(db.Boolean, nullable=False, default=False)
    no_item_insurance_active = db.Column(db.Boolean, nullable=False, default=False)
    # 封存清退：档案盒数据全保留，但清出酒馆编制、释放床位
    archived_at = db.Column(db.DateTime, nullable=True)
    archived_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    archive_reason = db.Column(db.String(200), nullable=True)
    archived_from_dorm_room_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_dorm_rooms.id', ondelete='SET NULL'), nullable=True)
    archived_from_bed_index = db.Column(db.Integer, nullable=True)
    # 玩家末次手动操作时间（自动组队/下本/建筑自动结算等 scheduler 不得刷新）
    last_owner_action_at = db.Column(db.DateTime, nullable=True)
    # 交易市场扩展格子（付费临时扩容）
    market_extra_slots = db.Column(db.Integer, nullable=False, default=0)  # 已购买的额外格子数
    market_extra_slots_expires_at = db.Column(db.DateTime, nullable=True)  # 扩容到期时间（UTC）
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    # 不占酒馆编制 / 床位 / 升级人数统计的终态
    TAVERN_EXCLUDED_STATUSES = ('dead', 'archived')

    __table_args__ = (
        # 自动建队按 status + 等级带筛选空闲冒险者，复合索引覆盖该路径。
        db.Index('idx_advs1_status_level', 'status', 'level'),
        {'extend_existing': True},
    )

    # 重数据子表：默认 selectin 批量加载，避免 N+1；列表接口后续可用 raiseload/noload 跳过仓库。
    traits = db.relationship(
        'AdventurerS1Trait', back_populates='adventurer',
        lazy='selectin', cascade='all, delete-orphan',
    )
    equipment_items = db.relationship(
        'AdventurerS1Equipment', back_populates='adventurer',
        lazy='selectin', cascade='all, delete-orphan',
    )
    skill_items = db.relationship(
        'AdventurerS1Skill', back_populates='adventurer',
        lazy='selectin', cascade='all, delete-orphan',
    )
    investor_items = db.relationship(
        'AdventurerS1Investor', back_populates='adventurer',
        lazy='selectin', cascade='all, delete-orphan',
    )
    # 隐藏剧情线只在探险 prompt 构建时按需读取，列表/详情默认不触发。
    story_threads = db.relationship(
        'AdventurerS1StoryThread', back_populates='adventurer',
        lazy='select', cascade='all, delete-orphan',
    )
    # 仓库可达数百件，是列表慢查询主因之一，默认 lazy='select'（访问时才查），
    # 列表接口不触碰它即可零成本跳过；详情/仓库接口再显式加载。
    warehouse_items = db.relationship(
        'AdventurerS1WarehouseItem', back_populates='adventurer',
        lazy='select', cascade='all, delete-orphan',
    )
    building_instance = db.relationship(
        'AdventurerS1BuildingInstance', foreign_keys=[building_instance_id],
        lazy='select', viewonly=True,
    )
    # 床位所属房间：恢复速率按房间等级倍率，selectin 批量加载避免列表页计算精力时 N+1。
    dorm_room = db.relationship(
        'AdventurerS1DormRoom', foreign_keys=[dorm_room_id],
        lazy='selectin', viewonly=True, overlaps='occupants',
    )
    active_title_config = db.relationship(
        # selectin keeps list/detail loading batched without injecting a LEFT JOIN into
        # AdventurerS1 FOR UPDATE statements (PostgreSQL cannot lock a nullable join side).
        'AdventurerS1Title', foreign_keys=[active_title_id], lazy='selectin', viewonly=True,
    )

    def get_recovery_rate(self, base_rate=1):
        '精力恢复速率（点/分钟）。\n\n        - 玩家宿舍楼（有关联 building_instance）：Lv2 起读 upgrade_config.handler_effects\n          的 recovery_rate_bonus_pct（默认 +10%）；room.level 仅作 UI 装修展示，不参与倍率。\n        - 种子房间等无建筑实例：仍按 room.level 倍率（预留未来装修等级）。\n        '
        rate = float(base_rate)
        if self.dorm_room and self.dorm_room.building_instance:
            bi = self.dorm_room.building_instance
            if int(getattr(bi, 'upgrade_level', 1) or 1) > 1:
                pct = 10
                tpl = bi.template
                if tpl and tpl.upgrade_config:
                    effects = tpl.upgrade_config.get('handler_effects') or {}
                    pct = int(effects.get('recovery_rate_bonus_pct', 10) or 10)
                rate *= 1 + pct / 100.0
            return rate
        if self.dorm_room:
            level = int(self.dorm_room.level or 1)
            rate *= max(1, level)
        return rate

    # ── 可派遣性检查（统一占位判断）────────────────────────────────────────

    # 允许被派遣去执行新行为的状态集合
    DISPATCHABLE_STATUSES = ('resting', 'idle')

    def can_dispatch(self) -> tuple[bool, str | None]:
        """检查冒险者当前是否可被派遣（组队 / 建造 / 任何新行为）。

        占位来源：
        1. status 不在 DISPATCHABLE_STATUSES → 已在做某事（in_party / exploring / building / dead）
        2. building_instance_id 非空 → 正在帮工建筑，即使 status 已回 resting 也算占位

        返回 (ok, reason)。ok=True 表示可派遣，reason 为拒绝原因。
        """
        if self.status not in self.DISPATCHABLE_STATUSES:
            _STATUS_LABELS = {
                'in_party': '已在小队中',
                'exploring': '探险中',
                'activity': '参加酒馆活动中',
                'skill_school_queue': '在百艺阁候场中',
                'building_queue': '在设施外候场中',
                'building': '建造中',
                'upgrading': '升级中',
                'dead': '已死亡',
                'archived': '已封存清退',
            }
            label = _STATUS_LABELS.get(self.status, self.status)
            if self.status == 'skill_school_queue':
                return False, '冒险者正在百艺阁等候导师指点，候场期间无法执行其他行为'
            if self.status == 'building_queue':
                return False, '冒险者正在设施外等候空位，候场期间无法执行其他行为'
            # building + 已建成建筑 → 用沉浸式标签替代笼统的"建造中"
            if self.status == 'building' and self.building_instance and self.building_instance.status == 'built':
                handler_key = (self.building_instance.template.handler_key
                               if self.building_instance.template else None)
                # handler_key 为空时用 template.code 兜底（休闲区建筑早期 seed 无 handler_key）
                lookup_key = handler_key or (
                    self.building_instance.template.code
                    if self.building_instance.template else None)
                _HANDLER_LABELS = {
                    'healing_house': '治疗中',
                    'blacksmith': '锻造中',
                    'tailor_workshop': '裁制中',
                    'alchemy_workshop': '炼金中',
                    'ember_kitchen': '烹饪中',
                    'training': '修炼中',
                    'herbalist_shop': '购物中',
                    'armory_shop': '购物中',
                    'forge_supply_shop': '购物中',
                    'task_board': '执行任务中',
                    'tavern_hall': '喝酒中',
                    'massage_parlor': '按摩中',
                    'foot_spa': '泡脚中',
                    'music_pavilion': '听曲中',
                    'hospital': '住院中',
                    'sanatorium': '疗养中',
                    'dining_hall': '用餐中',
                    'gambling_hall': '赌坊就座中',
                    'skill_school': '学艺中',
                }
                if lookup_key == 'skill_school':
                    return False, '冒险者正在百艺阁跟导师学艺，等这堂课结束再来找他吧'
                label = _HANDLER_LABELS.get(lookup_key, '工作中')
            return False, f'冒险者当前{label}，无法执行新行为'
        if self.building_instance_id:
            return False, '冒险者正在帮工建筑，请先召回'
        return True, None

    def _building_allows_rest_like_recovery(self) -> bool:
        """True 当冒险者正在可恢复精力/血量的建筑中。

        疗养所天然支持；普通建筑只有 settlement_config 明确
        rest_like_recovery=True 时才按初级宿舍速度恢复，避免误让建造/打工通用建筑回血回精力。
        """
        if self.status != 'building':
            return False
        inst = self.building_instance
        if inst is None:
            return False
        tpl = getattr(inst, 'template', None)
        if tpl is None:
            return False
        if getattr(tpl, 'handler_key', None) == 'sanatorium':
            return True
        cfg = getattr(tpl, 'settlement_config', None) or {}
        return bool(cfg.get('rest_like_recovery'))

    def _is_dorm_recovery_suspended(self) -> bool:
        """宿舍关联实例 upgrading 时床位暂停精力/血量/特质恢复（建造中无在住者，不在此拦截）。"""
        room = self.dorm_room
        if not room or not room.building_instance_id:
            return False
        inst = room.building_instance
        if not inst:
            return False
        return inst.status == 'upgrading'

    def _is_dorm_bed_at_home(self, *, tavern_seated: bool = False) -> bool:
        """冒险者是否身在宿舍床位上（非酒馆入座等外出态）。"""
        if tavern_seated:
            return False
        if self.status != 'resting':
            return False
        if self.dorm_room_id is None or self.bed_index is None:
            return False
        return True

    def _should_suspend_dorm_bed_recovery(self, *, tavern_seated: bool = False) -> bool:
        """宿舍 upgrading 时仅暂停「在家床位上」的恢复；外出/酒馆/茶舍等地不受影响。"""
        return (
            self._is_dorm_bed_at_home(tavern_seated=tavern_seated)
            and self._is_dorm_recovery_suspended()
        )

    def get_current_stamina(
        self,
        max_stamina=100,
        recovery_per_minute=1,
        now=None,
        *,
        tavern_seated: bool = False,
    ):
        """惰性计算当前精力值：床位休息按房间等级恢复，可恢复建筑按初级宿舍速度恢复，其余状态冻结。

        非休息态（in_party/exploring/building/dead/idle）一律返回当前已冻结的精力值，
        不随时间增长；离开休息态时由 service 的 leave_rest 把已恢复值落账并冻结。
        例外：疗养所 / rest_like_recovery 建筑中也按 1 分钟 1 点恢复精力（building_assigned_at 起算）。
        """
        base = max(0, min(int(self.stamina or 0), max_stamina))
        now = now or _portable_utc_now()

        # 疗养所 / 睡岗类建筑：按 building_assigned_at 起算，初级宿舍速度（1 分钟 1 点）
        if self._building_allows_rest_like_recovery():
            ref = self.building_assigned_at or self.stamina_updated_at
            if not ref:
                return base
            elapsed_minutes = max(0, int((now - ref).total_seconds() / 60))
            return max(0, min(base + elapsed_minutes, max_stamina))

        # 宿舍床位：按 stamina_updated_at 起算，速率 × 房间等级
        if self.status != 'resting' or not self.stamina_updated_at:
            return base
        if self._should_suspend_dorm_bed_recovery(tavern_seated=tavern_seated):
            return base
        elapsed_minutes = max(
            0,
            int((now - self.stamina_updated_at).total_seconds() / 60),
        )
        recovered = elapsed_minutes * self.get_recovery_rate(recovery_per_minute)
        return max(0, min(base + recovered, max_stamina))

    def get_current_hp(
        self,
        recovery_minutes_per_point=10,
        now=None,
        *,
        tavern_seated: bool = False,
    ):
        """惰性计算当前生命值：宿舍床位休息或可恢复建筑中按相同速率恢复，其余状态冻结。"""
        max_hp = int(self.max_hp or 100)
        base = max(0, min(int(self.hp or 0), max_hp))
        now = now or _portable_utc_now()

        # 疗养所 / 睡岗类建筑：按 building_assigned_at 起算，与初级宿舍同速
        if self._building_allows_rest_like_recovery():
            ref = self.building_assigned_at or self.hp_updated_at
            if not ref:
                return base
            elapsed_minutes = max(0, (now - ref).total_seconds() / 60)
            recovered = int(elapsed_minutes / max(1, recovery_minutes_per_point))
            return max(0, min(base + recovered, max_hp))

        # 宿舍床位
        if self.status != 'resting' or not self.dorm_room_id or not self.hp_updated_at:
            return base
        if self._should_suspend_dorm_bed_recovery(tavern_seated=tavern_seated):
            return base
        elapsed_minutes = max(0, (now - self.hp_updated_at).total_seconds() / 60)
        recovered = int(elapsed_minutes / max(1, recovery_minutes_per_point))
        return max(0, min(base + recovered, max_hp))

    def _resolve_item_desc(self, name, custom_desc, item_dict):
        """个性化 custom_desc 优先；否则回退图鉴通用描述；都没有则空串。"""
        if custom_desc:
            return custom_desc
        if item_dict and name in item_dict:
            return item_dict[name]
        return ''

    def to_dict(
        self,
        item_dict=None,
        item_clue_meta=None,
        current_user_id=None,
        is_admin=False,
        is_followed=None,
        mode='detail',
        tavern_seated=False,
        building_info=None,
        include_history_story=True,
        trait_loadout_states=None,
    ):
        """序列化为前端字段集。

        Args:
            item_dict: 图鉴 {物品名: 描述} 映射，用于补全装备/仓库通用描述。
            item_clue_meta: 图鉴 {物品名: 线索公开字段}，用于隐藏 Boss 线索展示。
            current_user_id: 已认证请求用户；控制本人私有字段与 is_followed。
            is_admin: 仅管理入口使用；允许私有库存及管理字段。
            is_followed: 显式传入则不再查库，便于批量列表预先算好关注关系。
            mode: 'detail'（默认）按查看者返回详情；'list' 仅返回卡片所需字段，
                  省略 history_story / warehouse / investors / recovery_time / closure_* 等
                  详情专属重数据，且不会触发 lazy='select' 的 warehouse_items 加载。
            tavern_seated: 是否正坐在酒馆大堂（status 仍为 resting，仅 UI 展示用）。
            building_info: 调用方批量预取的建筑状态，避免列表页逐行懒加载 building_instance。
            trait_loadout_states: 列表调用方批量计算的有效携带状态，隐藏已卸下的可选特质。
        """
        is_list = (mode == 'list')
        is_owner = current_user_id is not None and self.owner_id == current_user_id
        can_view_private = is_owner or is_admin
        expose_player_item_lock = bool(
            not is_list
            and is_owner
        )
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            attach_hidden_boss_clue_fields,
        )
        clue_meta = item_clue_meta or {}
        recovery_now = _portable_utc_now()

        def _attach_asset_recovery(entry, asset):
            minutes = max(0, int(getattr(asset, 'recovery_minutes', 0) or 0))
            ends_at = getattr(asset, 'recovery_ends_at', None)
            recovering = bool(ends_at and ends_at > recovery_now)
            entry['recovery_minutes'] = minutes
            entry['recovering'] = recovering
            if recovering:
                entry['recovery_ends_at'] = to_utc_iso(ends_at)

        equipment = []
        for eq in self.equipment_items:
            entry = {
                'id': eq.id,
                'name': eq.name,
                'rarity': eq.rarity,
            }
            _attach_asset_recovery(entry, eq)
            if eq.type == S1_ITEM_TYPE_COMPANION:
                entry['type'] = S1_ITEM_TYPE_COMPANION
                entry['evolution_stage'] = max(0, int(eq.evolution_stage or 0))
            clue_fields = clue_meta.get(eq.name)
            if not is_list:
                entry['type'] = eq.type
                entry['created_at'] = to_utc_iso(eq.created_at) if eq.created_at else None
            if clue_fields:
                attach_hidden_boss_clue_fields(entry, clue_fields)
            elif not is_list:
                entry['desc'] = self._resolve_item_desc(eq.name, eq.custom_desc, item_dict)
                if eq.uses_left is not None:
                    entry['uses_left'] = eq.uses_left
            if int(getattr(eq, 'quantity', 1) or 1) > 1:
                entry['quantity'] = int(eq.quantity or 1)
            if expose_player_item_lock:
                entry['is_player_locked'] = bool(eq.is_player_locked)
            if not is_list:
                if eq.damage_stage:
                    entry['damage_stage'] = eq.damage_stage
                if eq.damage_note:
                    entry['damage_note'] = eq.damage_note
            equipment.append(entry)

        visible_traits = []
        archived_traits = []
        for t in self.traits:
            if t.is_archived:
                if is_admin:
                    archived_traits.append(t.to_dict())
            else:
                loadout_state = (trait_loadout_states or {}).get(t.id, {})
                if is_list and loadout_state.get('equipped') is False:
                    continue
                entry = {'name': t.name}
                if not is_list:
                    entry['desc'] = t.description
                    policy = (t.recovery_policy or 'none').strip().lower()
                    stage = (t.recovery_stage or '').strip().lower()
                    if policy in ('rest', 'sleep') and stage in ('light', 'medium', 'trace'):
                        entry['recovery_policy'] = policy
                        entry['recovery_stage'] = stage
                        entry['recovery_progress'] = int(t.recovery_progress or 0)
                        entry['recovery_required'] = t.recovery_required
                        if stage == 'trace':
                            entry['recovery_hours'] = t.recovery_hours
                            if t.recovery_expires_at:
                                entry['recovery_expires_at'] = to_utc_iso(t.recovery_expires_at)
                visible_traits.append(entry)

        max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
        # 卡片渲染必需字段（list 与 detail 共享）
        if building_info is not None:
            building_status = building_info.get('status')
            building_handler_key = building_info.get('handler_key')
        else:
            building = self.building_instance
            building_status = building.status if building else None
            building_handler_key = (
                building.template.handler_key
                if building and building.template else None
            )

        from plugins.adventurer_s1.services.adventurer_s1_building_handler import get_training_cap

        res = {
            'id': self.id,
            'name': self.name,
            'gender': self.gender,
            'character_class': self.character_class,
            'level': self.level,
            'status': self.status,
            'hp': self.hp,
            'max_hp': self.max_hp,
            'hp_updated_at': to_utc_iso(self.hp_updated_at) if self.hp_updated_at else None,
            'dorm_room_id': self.dorm_room_id,
            'strength': self.strength,
            'agility': self.agility,
            'intelligence': self.intelligence,
            'luck': self.luck,
            'personality': self.personality.split(',') if self.personality else [],
            'gold': _round_money(self.gold),
            'traits': visible_traits,
            'equipment': equipment,
            'is_followed': self._compute_is_followed(current_user_id, is_followed),
            'is_player_created': self.is_player_created,
            'owner_id': self.owner_id,
            'realm_code': self.realm_code,
            'active_title': (
                self.active_title_config.to_public_dict()
                if self.active_title_config and self.active_title_config.is_enabled else None
            ),
            'latest_score': self.latest_score,
            'praise_count': int(self.praise_count or 0),
            'building_instance_id': self.building_instance_id,
            'building_instance_status': building_status,
            'building_handler_key': building_handler_key,
            'building_assigned_at': to_utc_iso(self.building_assigned_at) if self.building_assigned_at else None,
            # 返回数据库原始值，前端 liveStaminaOf 负责惰性恢复计算
            'stamina': self.stamina,
            'max_stamina': max_stamina,
            'stamina_updated_at': to_utc_iso(self.stamina_updated_at) if self.stamina_updated_at else None,
        }
        if can_view_private:
            res.update({
                'party_preference': self.party_preference,
                'training_count': int(self.training_count or 0),
                'training_cap': get_training_cap(self.level),
                'insurance_active': bool(self.insurance_active),
                'empty_return_insurance_active': bool(self.empty_return_insurance_active),
                'comrade_memorial_insurance_active': bool(self.comrade_memorial_insurance_active),
                'no_gold_insurance_active': bool(self.no_gold_insurance_active),
                'no_item_insurance_active': bool(self.no_item_insurance_active),
            })
        if is_owner:
            res['initial_rename_available'] = bool(self.initial_rename_available)
        if tavern_seated:
            res['tavern_seated'] = True
        if self.status == 'archived':
            res['archived_at'] = to_utc_iso(self.archived_at) if self.archived_at else None
            if is_admin:
                res['archived_from_dorm_room_id'] = self.archived_from_dorm_room_id
                res['archived_from_bed_index'] = self.archived_from_bed_index
        if isinstance(self.expedition_buff, dict):
            if is_admin:
                res['expedition_buff'] = self.expedition_buff
            elif isinstance(self.expedition_buff.get('label'), str):
                res['expedition_buff'] = {'label': self.expedition_buff['label']}
        # hp 返回数据库原始值，前端 liveHpOf 负责宿舍休息时的惰性恢复计算

        if not is_list and can_view_private:
            # 私人库存只在本人或管理投影中构建，不能依赖前端隐藏区块。
            warehouse = []
            for w in self.warehouse_items:
                item = {
                    'id': w.id,
                    'name': w.name,
                    'type': w.type,
                    'rarity': w.rarity,
                    'created_at': to_utc_iso(w.created_at) if w.created_at else None,
                }
                if w.type == S1_ITEM_TYPE_COMPANION:
                    item['evolution_stage'] = max(0, int(w.evolution_stage or 0))
                clue_fields = clue_meta.get(w.name)
                if clue_fields:
                    attach_hidden_boss_clue_fields(item, clue_fields)
                else:
                    item['desc'] = self._resolve_item_desc(w.name, w.custom_desc, item_dict)
                if w.uses_left is not None:
                    item['uses_left'] = w.uses_left
                if int(getattr(w, 'quantity', 1) or 1) > 1:
                    item['quantity'] = int(w.quantity or 1)
                if expose_player_item_lock:
                    item['is_player_locked'] = bool(w.is_player_locked)
                _attach_asset_recovery(item, w)
                warehouse.append(item)
            # 材料库存：和装备 / 普通仓库分开展示，明确"采集所得、不可装备、不可入副本"语义
            from plugins.adventurer_s1.services.adventurer_s1_material_service import MATERIAL_META
            materials = [
                {
                    'name': m.name,
                    'quantity': int(m.quantity),
                    'display_name': MATERIAL_META.get(m.name, {}).get('name', m.name),
                    'icon': MATERIAL_META.get(m.name, {}).get('icon', '📦'),
                }
                for m in self.materials
                if int(m.quantity or 0) > 0
            ]
            res.update({
                'warehouse': warehouse,
                'warehouse_capacity_limit': int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100) or 100),
                'materials': materials,
            })

        if not is_list:
            res.update({
                'recovery_time': to_utc_iso(self.recovery_time) if self.recovery_time else None,
                'rest_start_time': to_utc_iso(self.rest_start_time) if self.rest_start_time else None,
                'bed_index': self.bed_index,
                'trait_recovery_cycle_hours': int(
                    current_app.config.get('ADVENTURER_S1_TRAIT_RECOVERY_CYCLE_HOURS', 6) or 6
                ),
                'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            })
            if include_history_story:
                res['history_story'] = self.history_story
            if can_view_private and self.status == 'archived':
                res['archive_reason'] = self.archive_reason
            if is_admin:
                res.update({
                    'investors': {inv.investor_name: _round_money(inv.amount) for inv in self.investor_items},
                    'closure_choice': self.closure_choice,
                    'closure_choice_at': to_utc_iso(self.closure_choice_at) if self.closure_choice_at else None,
                    'closure_resolution': self.closure_resolution,
                })

        if not is_list:
            # 详情页：已携带技能对所有人可见（管理员额外看全部）
            equipped_skills = [
                {
                    'id': s.id,
                    'name': s.name,
                    'description': s.description,
                    'equipped': bool(s.equipped),
                    'recovery_minutes': max(0, int(s.recovery_minutes or 0)),
                    'recovering': bool(s.recovery_ends_at and s.recovery_ends_at > recovery_now),
                    **(
                        {'recovery_ends_at': to_utc_iso(s.recovery_ends_at)}
                        if s.recovery_ends_at and s.recovery_ends_at > recovery_now else {}
                    ),
                }
                for s in self.skill_items if s.equipped
            ]
            if is_admin:
                res['skills'] = [
                    {
                        'id': s.id,
                        'name': s.name,
                        'description': s.description,
                        'equipped': bool(s.equipped),
                        'recovery_minutes': max(0, int(s.recovery_minutes or 0)),
                        'recovering': bool(s.recovery_ends_at and s.recovery_ends_at > recovery_now),
                        **(
                            {'recovery_ends_at': to_utc_iso(s.recovery_ends_at)}
                            if s.recovery_ends_at and s.recovery_ends_at > recovery_now else {}
                        ),
                    }
                    for s in self.skill_items
                ]
            else:
                res['skills'] = equipped_skills

        if is_admin:
            res['exp'] = self.exp
            res['archived_traits'] = archived_traits
            res['exp_required'] = self._get_exp_required(self.level)

        return res

    @staticmethod
    def _get_exp_required(level):
        if level >= 10:
            return level * 100 + (level - 9) * 1000
        return level * 100

    def _compute_is_followed(self, current_user_id, is_followed):
        if is_followed is not None:
            return is_followed
        if not current_user_id:
            return False
        return AdventurerS1Follow.query.filter_by(
            user_id=current_user_id, adventurer_id=self.id
        ).first() is not None


class AdventurerS1Title(db.Model):
    """S1 冒险者称号目录；frame_key 只引用前端内置安全边框，不存任意 CSS。"""

    __tablename__ = 'adventurer_s1_titles'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    description = db.Column(db.String(200), nullable=False, default='')
    frame_key = db.Column(db.String(24), nullable=False, default='standard', index=True)
    is_system = db.Column(db.Boolean, nullable=False, default=False)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = {'extend_existing': True}

    def to_public_dict(self) -> dict:
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'description': self.description,
            'frame_key': self.frame_key,
        }

    def to_admin_dict(self) -> dict:
        return {
            **self.to_public_dict(),
            'is_system': bool(self.is_system),
            'is_enabled': bool(self.is_enabled),
            'created_at': to_utc_iso(self.created_at),
            'updated_at': to_utc_iso(self.updated_at),
        }


class AdventurerS1TitleGrant(db.Model):
    """永久称号授予记录；收藏馆兑换的 granted_by_user_id 为空，note 留存兑换来源。"""

    __tablename__ = 'adventurer_s1_title_grants'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    title_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_titles.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    # 审计快照字段不设 users FK：历史 PostgreSQL 库的 users.id 可能没有可引用的
    # UNIQUE/PK 约束，且管理员账号清理后仍应保留当时的授权证据。
    # None 表示收藏馆兑换；非空仍为管理员手工授予快照。
    granted_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    note = db.Column(db.String(200), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'adventurer_id', 'title_id', name='uq_advs1_title_grant_adv_title',
        ),
        db.Index('idx_advs1_title_grant_title_adv', 'title_id', 'adventurer_id'),
        {'extend_existing': True},
    )

    title = db.relationship('AdventurerS1Title', lazy='joined', viewonly=True)


class AdventurerS1FateCompanionCredit(db.Model):
    """陪同他人推进一次命途关键节点的幂等贡献台账。"""

    __tablename__ = 'adventurer_s1_fate_companion_credits'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expedition_log_id = db.Column(
        db.Integer,
        nullable=False,
        index=True,
    )
    # 旧 PostgreSQL 库中的探险日志/冒险者 id 没有可供新表引用的唯一约束；
    # 三个身份都保留为弱引用，由结算服务在写入时使用已加载实体保证有效性。
    fate_adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    companion_adventurer_id = db.Column(
        db.Integer,
        nullable=False,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'expedition_log_id',
            'companion_adventurer_id',
            name='uq_advs1_fate_companion_credit_log_adv',
        ),
        db.Index(
            'idx_advs1_fate_companion_credit_adv_log',
            'companion_adventurer_id',
            'expedition_log_id',
        ),
        {'extend_existing': True},
    )


class AdventurerS1Squad(db.Model):
    """玩家创建的纹章小队；仅用于冒险者名片静态装饰。"""

    __tablename__ = 'adventurer_s1_squads'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(50), nullable=False)
    # NFKC + casefold 后的唯一键，防止仅大小写/全半角不同的重名。
    name_key = db.Column(db.String(100), nullable=False, unique=True, index=True)
    # 旧 PostgreSQL 的 adventurer_s1.id 没有可供新表引用的唯一约束；沿用
    # S1 既有弱引用约定，由唯一索引、写事务与启动完整性检查共同维护。
    leader_adventurer_id = db.Column(
        db.Integer,
        nullable=False,
        unique=True,
        index=True,
    )
    emblem_frame = db.Column(db.String(20), nullable=False)
    emblem_pattern = db.Column(db.String(20), nullable=False)
    emblem_sigil = db.Column(db.String(20), nullable=False)
    emblem_primary = db.Column(db.String(20), nullable=False)
    emblem_secondary = db.Column(db.String(20), nullable=False)
    emblem_accent = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = {'extend_existing': True}

    def emblem_dict(self):
        return {
            'frame': self.emblem_frame,
            'pattern': self.emblem_pattern,
            'sigil': self.emblem_sigil,
            'primary': self.emblem_primary,
            'secondary': self.emblem_secondary,
            'accent': self.emblem_accent,
        }

    def to_public_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'emblem': self.emblem_dict(),
        }


class AdventurerS1Party(db.Model):
    """冒险小队。"""

    __tablename__ = 'adventurer_s1_parties'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='forming', index=True)  # forming/exploring/disbanded
    target_dungeon = db.Column(db.String(100), nullable=True)
    target_size = db.Column(db.Integer, nullable=False, default=4)
    departure_time = db.Column(db.DateTime, nullable=True)
    # 未满员时冻结的出发倒计时剩余秒数；满员恢复时续计，不重置为完整等待时长。
    departure_paused_seconds = db.Column(db.Integer, nullable=True)
    sponsor_fund = db.Column(db.Float, nullable=False, default=0.0)
    sponsor_reduced_minutes = db.Column(db.Integer, nullable=False, default=0)
    leader_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    members = db.relationship(
        'AdventurerS1', secondary='adventurer_s1_party_members', lazy='selectin',
        backref=db.backref('parties', lazy=True),
    )
    leader = db.relationship('AdventurerS1', foreign_keys=[leader_id])

    def to_dict(self, item_dict=None, *, departure_voted_adventurer_ids=None):
        from plugins.adventurer_s1.services.adventurer_s1_config_service import (
            format_s1_dungeon_level_desc,
        )

        payload = {
            'id': self.id,
            'name': self.name,
            'status': self.status,
            'target_dungeon': self.target_dungeon,
            'target_dungeon_level_desc': format_s1_dungeon_level_desc(self.target_dungeon),
            'target_size': self.target_size,
            'departure_time': to_utc_iso(self.departure_time) if self.departure_time else None,
            'member_count': len(self.members),
            # 玩家侧小队页：成员徽章仅 id + 姓名 + HP，不带职业/状态/装备/特质等。
            'members': [{
                'id': m.id,
                'name': m.name,
                'hp': m.hp,
                'max_hp': m.max_hp,
                'active_title': (
                    m.active_title_config.to_public_dict()
                    if m.active_title_config and m.active_title_config.is_enabled else None
                ),
            } for m in self.members],
        }
        if departure_voted_adventurer_ids is not None:
            payload['departure_voted_adventurer_ids'] = list(departure_voted_adventurer_ids)
            payload['departure_vote_count'] = len(departure_voted_adventurer_ids)
        return payload


class AdventurerS1SpecialPartySlot(db.Model):
    """特殊机缘副本 forming 队唯一占位；副本名主键负责跨 worker 防重。"""

    __tablename__ = 'adventurer_s1_special_party_slots'

    dungeon_name = db.Column(db.String(100), primary_key=True)
    party_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_parties.id', ondelete='CASCADE'),
        nullable=False,
        unique=True,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    def to_dict(self) -> dict:
        return {
            'dungeon_name': self.dungeon_name,
            'party_id': self.party_id,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1PartyMember(db.Model):
    """小队成员关联表（多对多）。补唯一约束防止同一冒险者重复入队。"""

    __tablename__ = 'adventurer_s1_party_members'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    party_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_parties.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('party_id', 'adventurer_id', name='uq_advs1_party_member'),
        {'extend_existing': True},
    )


class AdventurerS1PartyRejoinCooldown(db.Model):
    """离队后短期内禁止再次加入同一小队的冷却记录。"""

    __tablename__ = 'adventurer_s1_party_rejoin_cooldowns'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    party_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_parties.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    blocked_until = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('adventurer_id', 'party_id', name='uq_advs1_party_rejoin_cooldown'),
        {'extend_existing': True},
    )


class AdventurerS1PartyDepartureVote(db.Model):
    """小队满员集结期间，成员投票提前出发。"""

    __tablename__ = 'adventurer_s1_party_departure_votes'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    party_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_parties.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('party_id', 'adventurer_id', name='uq_advs1_party_departure_vote'),
        {'extend_existing': True},
    )


class AdventurerS1MatchTicketRejoinCooldown(db.Model):
    """公开匹配小队退队后短期内禁止再次加入同一报名。"""

    __tablename__ = 'adventurer_s1_match_ticket_rejoin_cooldowns'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    ticket_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_match_tickets.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    blocked_until = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('adventurer_id', 'ticket_id', name='uq_advs1_match_ticket_rejoin_cooldown'),
        {'extend_existing': True},
    )


class AdventurerS1MatchTicket(db.Model):
    """S1 匹配报名单。队伍成团前使用，成团后转为 AdventurerS1Party。"""

    __tablename__ = 'adventurer_s1_match_tickets'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='forming', index=True)  # forming/queued/matched/cancelled
    queue_type = db.Column(db.String(20), nullable=False, default='solo')  # solo/group
    access_mode = db.Column(db.String(20), nullable=False, default='public')  # public/invite
    allow_solo_fill = db.Column(db.Boolean, nullable=False, default=False)  # 是否允许匹配单排路人补位
    invite_code = db.Column(db.String(6), nullable=True, unique=True, index=True)  # 6 位随机小队邀请码
    preferred_dungeon = db.Column(db.String(100), nullable=True, index=True)  # NULL = 随机地图
    preferred_dungeons = db.Column(db.JSON, nullable=True)  # 单人匹配候选地图；NULL = 随机地图
    description = db.Column(db.String(500), nullable=False, default='')
    match_error = db.Column(db.String(300), nullable=False, default='')
    fate_risk_acknowledgements = db.Column(db.JSON, nullable=True)
    campaign_dispatch_id = db.Column(db.Integer, nullable=True, index=True)
    campaign_dispatch_revision = db.Column(db.Integer, nullable=True)
    campaign_task_title = db.Column(db.String(120), nullable=True)
    # 超时撤销的报名保留原成员，作为这些账号主动接任务冷却的持久化凭据。
    campaign_task_cooldown_until = db.Column(db.DateTime, nullable=True, index=True)
    matched_party_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_parties.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    queued_at = db.Column(db.DateTime, nullable=True, index=True)
    matched_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_match_status_created', 'status', 'created_at'),
        {'extend_existing': True},
    )

    members = db.relationship(
        'AdventurerS1MatchTicketMember', back_populates='ticket',
        lazy='selectin', cascade='all, delete-orphan',
    )
    matched_party = db.relationship('AdventurerS1Party', foreign_keys=[matched_party_id], lazy='select')

    def to_dict(self):
        return {
            'id': self.id,
            'owner_id': self.owner_id,
            'status': self.status,
            'queue_type': self.queue_type,
            'access_mode': self.access_mode,
            'allow_solo_fill': self.allow_solo_fill,
            'invite_code': self.invite_code,
            'preferred_dungeon': self.preferred_dungeon,
            'preferred_dungeons': self.preferred_dungeons,
            'description': self.description,
            'campaign_task_title': self.campaign_task_title,
            'match_error': self.match_error,
            'matched_party_id': self.matched_party_id,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'queued_at': to_utc_iso(self.queued_at) if self.queued_at else None,
            'matched_at': to_utc_iso(self.matched_at) if self.matched_at else None,
            'cancelled_at': to_utc_iso(self.cancelled_at) if self.cancelled_at else None,
            'member_count': len(self.members),
            'members': [m.to_dict() for m in self.members],
        }


class AdventurerS1MatchTicketMember(db.Model):
    """匹配报名成员。一个冒险者同时只能存在一条 queued 报名，业务层兜底校验。"""

    __tablename__ = 'adventurer_s1_match_ticket_members'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_match_tickets.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('ticket_id', 'adventurer_id', name='uq_advs1_match_ticket_member'),
        db.Index('idx_advs1_match_member_adv', 'adventurer_id'),
        {'extend_existing': True},
    )

    ticket = db.relationship('AdventurerS1MatchTicket', back_populates='members')
    adventurer = db.relationship('AdventurerS1', lazy='selectin')

    def to_dict(self):
        adv = self.adventurer
        return {
            'adventurer_id': self.adventurer_id,
            'name': adv.name if adv else None,
            'level': adv.level if adv else None,
            'status': adv.status if adv else None,
        }


class AdventurerS1MatchInvite(db.Model):
    """匹配小队邀请。队长发给目标冒险者，接受后加入 forming 小队。"""

    __tablename__ = 'adventurer_s1_match_invites'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_match_tickets.id', ondelete='CASCADE'), nullable=False, index=True)
    inviter_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    target_adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)  # pending/accepted/declined/cancelled
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    responded_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_match_invite_ticket_status', 'ticket_id', 'status'),
        {'extend_existing': True},
    )

    ticket = db.relationship('AdventurerS1MatchTicket', lazy='selectin')
    target_adventurer = db.relationship('AdventurerS1', foreign_keys=[target_adventurer_id], lazy='selectin')

    def to_dict(self):
        adv = self.target_adventurer
        return {
            'id': self.id,
            'ticket_id': self.ticket_id,
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'responded_at': to_utc_iso(self.responded_at) if self.responded_at else None,
            'target_adventurer': {
                'id': adv.id,
                'name': adv.name,
                'level': adv.level,
                'status': adv.status,
            } if adv else None,
            'ticket': self.ticket.to_dict() if self.ticket else None,
        }


class AdventurerS1StoryThread(db.Model):
    """冒险者隐藏剧情线：玩家不可见，仅供探险 AI 延续长期伏笔。"""

    __tablename__ = 'adventurer_s1_story_threads'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    title = db.Column(db.String(100), nullable=False)
    summary = db.Column(db.Text, nullable=False, default='')
    tags = db.Column(db.JSON, nullable=True)
    # 与长期世界实体的精确身份绑定；只保存稳定 ID/key，不从叙事文本反推。
    entity_refs = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)  # active/dormant/resolved
    weight = db.Column(db.Integer, nullable=False, default=50)
    source_log_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'), nullable=True, index=True)
    # 渐进兼容个人剧情链：旧数据为空时按独立 head 处理。
    chain_key = db.Column(db.String(64), nullable=True, index=True)
    predecessor_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_story_threads.id', ondelete='SET NULL'), nullable=True, index=True)
    superseded_by_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_story_threads.id', ondelete='SET NULL'), nullable=True, index=True)
    is_chain_head = db.Column(db.Boolean, nullable=True, default=True, index=True)
    last_touched_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('adventurer_id', 'title', name='uq_advs1_story_thread_adv_title'),
        db.Index('idx_advs1_story_thread_adv_status_weight', 'adventurer_id', 'status', 'weight'),
        {'extend_existing': True},
    )

    adventurer = db.relationship('AdventurerS1', back_populates='story_threads')
    source_log = db.relationship('AdventurerS1ExpeditionLog', foreign_keys=[source_log_id])

    def to_prompt_line(self) -> str:
        tag_text = ''
        tags = [str(t).strip() for t in (self.tags or []) if str(t).strip()]
        if tags:
            tag_text = f"（标签：{'、'.join(tags[:6])}）"
        return f"{self.title}: {self.summary}{tag_text}"

    def to_admin_dict(self) -> dict:
        """管理员专用序列化；玩家接口禁止返回。"""
        return {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'title': self.title,
            'summary': self.summary,
            'tags': self.tags or [],
            'entity_refs': self.entity_refs or {},
            'status': self.status,
            'weight': self.weight,
            'source_log_id': self.source_log_id,
            'chain_key': self.chain_key,
            'predecessor_id': self.predecessor_id,
            'superseded_by_id': self.superseded_by_id,
            'is_chain_head': self.is_chain_head,
            'last_touched_at': to_utc_iso(self.last_touched_at) if self.last_touched_at else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1WorldFactCandidate(db.Model):
    """探险 AI 产出的世界事实 / DM 便签候选。

    候选由异步大纲维护 AI 长期合并进公共世界情报与 DM 接力便签；管理员后台仅用于
    查看、纠错或偶发人工覆盖。
    """

    __tablename__ = 'adventurer_s1_world_fact_candidates'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expedition_log_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    target_dungeon = db.Column(db.String(100), nullable=False, index=True)
    world_fact_updates = db.Column(db.JSON, nullable=True)
    dm_handoff_note_candidate = db.Column(db.Text, nullable=True)
    # awaiting_completion 只保存未结算探险的草稿；完成事务内才原子发布为 pending。
    status = db.Column(db.String(20), nullable=False, default='awaiting_completion', index=True)
    outline_error = db.Column(db.Text, nullable=True)
    # 最近一次维护的稳定问题码与结构化消费回执仅供管理员审计；玩家接口禁止投影。
    maintenance_issue_code = db.Column(db.String(64), nullable=True)
    maintenance_receipt = db.Column(db.JSON, nullable=True)
    maintenance_recovery_kind = db.Column(
        db.String(32),
        nullable=True,
    )
    maintenance_recovery_attempts = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )
    processed_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('expedition_log_id', name='uq_advs1_world_fact_candidate_log'),
        db.Index('idx_advs1_world_fact_candidate_status_dungeon', 'status', 'target_dungeon'),
        db.Index(
            'idx_advs1_world_fact_candidate_issue',
            'maintenance_issue_code',
        ),
        db.Index(
            'idx_advs1_world_fact_candidate_recovery',
            'maintenance_recovery_kind',
        ),
        {'extend_existing': True},
    )

    log = db.relationship('AdventurerS1ExpeditionLog', lazy='select')

    def to_admin_dict(self) -> dict:
        outline_error = self.outline_error
        if self.status == 'processing' and str(outline_error or '').startswith('__lease__:'):
            outline_error = None
        return {
            'id': self.id,
            'expedition_log_id': self.expedition_log_id,
            'target_dungeon': self.target_dungeon,
            'world_fact_updates': self.world_fact_updates or [],
            'dm_handoff_note_candidate': self.dm_handoff_note_candidate or '',
            'status': self.status,
            'outline_error': outline_error,
            'maintenance_issue_code': self.maintenance_issue_code,
            'maintenance_receipt': self.maintenance_receipt,
            'maintenance_recovery_kind': self.maintenance_recovery_kind,
            'maintenance_recovery_attempts': int(
                self.maintenance_recovery_attempts or 0
            ),
            'processed_at': to_utc_iso(self.processed_at) if self.processed_at else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1DmHandoffState(db.Model):
    """按地牢维护的一段内部 DM 接力便签，供后续探险软注入。"""

    __tablename__ = 'adventurer_s1_dm_handoff_states'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    target_dungeon = db.Column(db.String(100), nullable=False, unique=True, index=True)
    note = db.Column(db.Text, nullable=False, default='')
    source_candidate_ids = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now, index=True)

    __table_args__ = {'extend_existing': True}

    def to_admin_dict(self) -> dict:
        return {
            'id': self.id,
            'target_dungeon': self.target_dungeon,
            'note': self.note or '',
            'source_candidate_ids': self.source_candidate_ids or [],
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1WorldOutlineDungeonState(db.Model):
    """世界大纲按地牢的维护租约、探索状态与乐观版本；不承载剧情正文。"""

    __tablename__ = 'adventurer_s1_world_outline_dungeon_states'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    target_dungeon = db.Column(db.String(100), nullable=False, unique=True, index=True)
    outline_revision = db.Column(db.Integer, nullable=False, default=0)
    # NULL 表示旧数据/未知，扫描器必须忽略；False 才表示整个地牢当前不可探索。
    is_explorable = db.Column(db.Boolean, nullable=True, default=None, index=True)
    cycle_reseed_retry_after = db.Column(db.DateTime, nullable=True, index=True)
    cycle_reseed_last_status = db.Column(db.String(32), nullable=True)
    cycle_reseed_last_attempted_at = db.Column(db.DateTime, nullable=True)
    cycle_reseed_last_result = db.Column(db.JSON, nullable=True)
    maintenance_claim_token = db.Column(db.String(96), nullable=True, index=True)
    maintenance_claim_expires_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}


class AdventurerS1WorldLocationState(db.Model):
    """同一地牢内可复用地点的权威现状与写入租约。"""

    __tablename__ = 'adventurer_s1_world_location_states'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    target_dungeon = db.Column(db.String(100), nullable=False, index=True)
    canonical_name = db.Column(db.String(120), nullable=False)
    current_state = db.Column(db.Text, nullable=False, default='')
    public_risk_notice = db.Column(db.Text, nullable=False, default='', server_default='')
    revision = db.Column(db.Integer, nullable=False, default=0)
    last_changed_log_id = db.Column(db.Integer, nullable=True, index=True)
    write_lease_log_id = db.Column(db.Integer, nullable=True, index=True)
    write_lease_expires_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint(
            'target_dungeon', 'canonical_name',
            name='uq_advs1_world_location_dungeon_name',
        ),
        db.Index('idx_advs1_world_location_dungeon_revision', 'target_dungeon', 'revision'),
        {'extend_existing': True},
    )


class AdventurerS1WorldLocationStateHistory(db.Model):
    """地点权威现状的不可覆盖版本历史。"""

    __tablename__ = 'adventurer_s1_world_location_state_history'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    location_state_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_world_location_states.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    revision = db.Column(db.Integer, nullable=False)
    previous_state = db.Column(db.Text, nullable=False, default='')
    new_state = db.Column(db.Text, nullable=False)
    previous_public_risk_notice = db.Column(db.Text, nullable=False, default='', server_default='')
    new_public_risk_notice = db.Column(db.Text, nullable=False, default='', server_default='')
    change_summary = db.Column(db.Text, nullable=False)
    source_log_id = db.Column(db.Integer, nullable=True, index=True)
    snapshot_revision = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.UniqueConstraint(
            'location_state_id', 'revision',
            name='uq_advs1_world_location_history_revision',
        ),
        {'extend_existing': True},
    )


class AdventurerS1CampaignDispatch(db.Model):
    """Current invitation; active_log_id represents the remaining squad owners."""
    __tablename__ = 'adventurer_s1_campaign_dispatches'
    id = db.Column(db.Integer, primary_key=True)
    campaign_root_id = db.Column(db.Integer, nullable=False, unique=True)
    revision = db.Column(db.Integer, nullable=False, default=1)
    public_title = db.Column(db.String(120), nullable=False, default='')
    public_summary = db.Column(db.Text, nullable=False, default='')
    current_world_state = db.Column(db.Text, nullable=False, default='')
    objective = db.Column(db.Text, nullable=False, default='')
    target_dungeons = db.Column(db.JSON, nullable=False, default=list)
    discovered_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    active_log_id = db.Column(db.Integer, nullable=True, unique=True)
    max_active_teams = db.Column(db.Integer, nullable=False, default=1, server_default='1')
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)
    __table_args__ = {'extend_existing': True},


class AdventurerS1CampaignDispatchClaim(db.Model):
    """Exact squad ownership through world publication, one squad per action site."""
    __tablename__ = 'adventurer_s1_campaign_dispatch_claims'
    expedition_log_id = db.Column(db.Integer, primary_key=True)
    dispatch_id = db.Column(db.Integer, nullable=False, index=True)
    target_dungeon = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    __table_args__ = (
        db.UniqueConstraint('dispatch_id', 'target_dungeon', name='uq_s1_dispatch_claim_site'),
        {'extend_existing': True},
    )


class AdventurerS1CampaignDispatchReport(db.Model):
    """Published history only; the expedition log owns pending frozen results."""
    __tablename__ = 'adventurer_s1_campaign_dispatch_reports'
    expedition_log_id = db.Column(db.Integer, primary_key=True)
    dispatch_id = db.Column(db.Integer, nullable=False, index=True)
    revision = db.Column(db.Integer, nullable=False)
    party_name = db.Column(db.String(100), nullable=False)
    report = db.Column(db.JSON, nullable=False)
    published_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    __table_args__ = (db.UniqueConstraint('dispatch_id', 'revision', name='uq_s1_dispatch_report_round'), {'extend_existing': True})


class AdventurerS1CampaignStageState(db.Model):
    """Bounded current stage projection; published reports retain its evidence history."""
    __tablename__ = 'adventurer_s1_campaign_stage_states'
    dispatch_id = db.Column(db.Integer, primary_key=True)
    plan = db.Column(db.JSON, nullable=False)
    stage_key = db.Column(db.String(64), nullable=False)
    stage_instance = db.Column(db.Integer, nullable=False, default=1)
    progress = db.Column(db.JSON, nullable=False, default=dict)
    status = db.Column(db.String(20), nullable=False, default='active')
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)
    __table_args__ = (
        db.Index('ix_s1_campaign_stage_ready', 'status', 'updated_at', 'dispatch_id'),
        {'extend_existing': True},
    )


class AdventurerS1CampaignContinuation(db.Model):
    """One durable preparation job per campaign; stage receipts remain immutable."""
    __tablename__ = 'adventurer_s1_campaign_continuations'
    dispatch_id = db.Column(db.Integer, primary_key=True)
    input_hash = db.Column(db.String(64), nullable=False)
    snapshot = db.Column(db.JSON, nullable=False)
    request = db.Column(db.JSON, nullable=False, default=dict)
    proposal = db.Column(db.JSON, nullable=True)
    review = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    owner_token = db.Column(db.String(64), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text, nullable=True)
    admin_user_id = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    __table_args__ = (db.Index('ix_s1_campaign_continuation_work', 'status', 'updated_at'),
                      {'extend_existing': True})


class AdventurerS1CampaignEvolution(db.Model):
    """Background job metadata; gameplay authority stays in existing world/stage rows."""
    __tablename__ = 'adventurer_s1_campaign_evolutions'
    dispatch_id = db.Column(db.Integer, primary_key=True)
    checked_at = db.Column(db.DateTime, nullable=True)
    next_check_at = db.Column(db.DateTime, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='waiting')
    snapshot = db.Column(db.JSON, nullable=True)
    proposal = db.Column(db.JSON, nullable=True)
    review = db.Column(db.JSON, nullable=True)
    owner_token = db.Column(db.String(64), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    interruptions = db.Column(db.Integer, nullable=False, default=0)
    consumer_failures = db.Column(db.Integer, nullable=False, default=0)
    phase = db.Column(db.String(20), nullable=False, default='plan')
    error = db.Column(db.Text, nullable=True)
    # Small projections read without loading the frozen AI job on departure/board.
    world_states = db.Column(db.JSON, nullable=False, default=dict)
    public_notice = db.Column(db.JSON, nullable=True)
    public_notice_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    __table_args__ = {'extend_existing': True},


class AdventurerS1CampaignStageOperation(db.Model):
    """Atomic stage receipt; admin_user_id=0 identifies automatic transitions."""
    __tablename__ = 'adventurer_s1_campaign_stage_operations'
    id = db.Column(db.Integer, primary_key=True)
    dispatch_id = db.Column(db.Integer, nullable=False, index=True)
    operation_key = db.Column(db.String(64), nullable=False, unique=True)
    kind = db.Column(db.String(32), nullable=False)
    admin_user_id = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    before = db.Column(db.JSON, nullable=False)
    after = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    __table_args__ = {'extend_existing': True},


class AdventurerS1WorldStoryThread(db.Model):
    """公共世界情报。覆盖实体/事件/势力/地点状态，可跨副本按名字和关键词召回。"""

    __tablename__ = 'adventurer_s1_world_story_threads'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(120), nullable=False, unique=True, index=True)
    thread_type = db.Column(db.String(30), nullable=False, default='event', index=True)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    # 后端权威的主地城归属；scope_keys 只描述实际影响范围，可仅包含子区域/势力/实体。
    base_dungeon = db.Column(db.String(100), nullable=True, index=True)
    scope_keys = db.Column(db.JSON, nullable=True)
    trigger_keywords = db.Column(db.JSON, nullable=True)
    summary = db.Column(db.Text, nullable=False, default='')
    next_reveal_hint = db.Column(db.Text, nullable=True)
    power_tier = db.Column(db.String(30), nullable=True, index=True)
    power_profile = db.Column(db.Text, nullable=True)
    weight = db.Column(db.Integer, nullable=False, default=50, index=True)
    # 权威版本用于校验冻结在发车快照里的结构化写入许可。
    revision = db.Column(db.Integer, nullable=False, default=1, server_default='1')
    injection_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    missed_injection_runs = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    # closure_debt 按同地牢已完成探险累计；有明确推进/收束时归零。
    closure_debt = db.Column(db.Integer, nullable=False, default=0, server_default='0', index=True)
    last_injected_log_id = db.Column(db.Integer, nullable=True, index=True)
    last_progress_log_id = db.Column(db.Integer, nullable=True, index=True)
    source_candidate_ids = db.Column(db.JSON, nullable=True)
    last_seen_log_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'), nullable=True, index=True)
    # 渐进兼容剧情链：旧数据字段为空时按“自身就是独立 head”处理。
    chain_key = db.Column(db.String(64), nullable=True, index=True)
    predecessor_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_world_story_threads.id', ondelete='SET NULL'), nullable=True, index=True)
    superseded_by_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_world_story_threads.id', ondelete='SET NULL'), nullable=True, index=True)
    is_chain_head = db.Column(db.Boolean, nullable=True, default=True, index=True)
    # 共同战役不另建状态机：锚点保存唯一 campaign_key，所有并行前线继续使用
    # world story thread 自身的状态、revision、剧情链和写租约。
    campaign_key = db.Column(db.String(64), nullable=True)
    # 生产存量约束兼容：与既有 faction/world-actor link 一样使用弱引用。
    campaign_root_id = db.Column(db.Integer, nullable=True, index=True)
    campaign_role = db.Column(db.String(24), nullable=True, index=True)
    campaign_agenda = db.Column(db.Text, nullable=True)
    # 仅锚点保存规范化启动合同的摘要，用于同 key 重放与冲突判定。
    campaign_spec_hash = db.Column(db.String(64), nullable=True)
    # 创建后不可变的活动地图目录；旧战役为 null，保留原世界规则。
    campaign_world = db.Column(db.JSON(none_as_null=True), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('idx_advs1_world_story_status_weight', 'status', 'weight', 'updated_at'),
        db.Index(
            'uq_advs1_world_campaign_key',
            'campaign_key',
            unique=True,
        ),
        db.Index(
            'idx_advs1_world_campaign_root_role',
            'campaign_root_id',
            'campaign_role',
        ),
        {'extend_existing': True},
    )

    def to_prompt_line(self) -> str:
        parts = [f"{self.title}: {self.summary or ''}", f"版本: {int(self.revision or 1)}"]
        if self.power_profile:
            parts.append(f"连续性边界: {self.power_profile}")
        if self.next_reveal_hint:
            parts.append(f"可选后续: {self.next_reveal_hint}")
        return '；'.join(parts)

    def to_admin_dict(self) -> dict:
        return {
            'id': self.id,
            'title': self.title,
            'thread_type': self.thread_type,
            'status': self.status,
            'base_dungeon': self.base_dungeon or '',
            'scope_keys': self.scope_keys or [],
            'trigger_keywords': self.trigger_keywords or [],
            'summary': self.summary or '',
            'next_reveal_hint': self.next_reveal_hint or '',
            'power_tier': self.power_tier or '',
            'power_profile': self.power_profile or '',
            'weight': self.weight,
            'revision': int(self.revision or 1),
            'injection_count': int(self.injection_count or 0),
            'missed_injection_runs': int(self.missed_injection_runs or 0),
            'closure_debt': int(self.closure_debt or 0),
            'last_injected_log_id': self.last_injected_log_id,
            'last_progress_log_id': self.last_progress_log_id,
            'source_candidate_ids': self.source_candidate_ids or [],
            'last_seen_log_id': self.last_seen_log_id,
            'chain_key': self.chain_key,
            'predecessor_id': self.predecessor_id,
            'superseded_by_id': self.superseded_by_id,
            'is_chain_head': self.is_chain_head,
            'campaign_key': self.campaign_key,
            'campaign_root_id': self.campaign_root_id,
            'campaign_role': self.campaign_role,
            'campaign_agenda': self.campaign_agenda or '',
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1FactionProfile(db.Model):
    """势力的有界当前档案；公共剧情阶段仍由 world story thread 承载。"""

    __tablename__ = 'adventurer_s1_faction_profiles'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    faction_key = db.Column(db.String(120), nullable=False, unique=True, index=True)
    canonical_name = db.Column(db.String(160), nullable=False, unique=True, index=True)
    aliases = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    identity_summary = db.Column(db.Text, nullable=False, default='')
    scope_keys = db.Column(db.JSON, nullable=True)
    trigger_keywords = db.Column(db.JSON, nullable=True)
    importance = db.Column(db.Integer, nullable=False, default=50, index=True)
    # {facet_key: {summary: str, visibility: public|rumor|restricted}}
    facets = db.Column(db.JSON, nullable=True)
    revision = db.Column(db.Integer, nullable=False, default=1, server_default='1')
    source_candidate_ids = db.Column(db.JSON, nullable=True)
    # 生产存量 expedition_logs.id 不保证可供 FK 引用；来源日志只作弱审计引用。
    first_seen_log_id = db.Column(db.Integer, nullable=True, index=True)
    last_seen_log_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
        index=True,
    )

    __table_args__ = (
        db.Index(
            'idx_advs1_faction_profile_status_importance',
            'status', 'importance', 'updated_at',
        ),
        {'extend_existing': True},
    )

    def to_admin_dict(self) -> dict:
        return {
            'id': self.id,
            'faction_key': self.faction_key,
            'canonical_name': self.canonical_name,
            'aliases': self.aliases or [],
            'status': self.status,
            'identity_summary': self.identity_summary or '',
            'scope_keys': self.scope_keys or [],
            'trigger_keywords': self.trigger_keywords or [],
            'importance': int(self.importance or 50),
            'facets': self.facets or {},
            'revision': int(self.revision or 1),
            'source_candidate_ids': self.source_candidate_ids or [],
            'first_seen_log_id': self.first_seen_log_id,
            'last_seen_log_id': self.last_seen_log_id,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1FactionProfileThread(db.Model):
    """势力档案与公共势力线的显式关联；禁止按名字或关键词猜关联。"""

    __tablename__ = 'adventurer_s1_faction_profile_threads'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    faction_profile_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_faction_profiles.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    # 与现有 world_story_thread_actors 一致使用弱引用，兼容生产存量约束。
    world_story_thread_id = db.Column(db.Integer, nullable=False, index=True)
    relation_role = db.Column(db.String(20), nullable=False, default='involved', index=True)
    source_log_id = db.Column(db.Integer, nullable=True, index=True)
    # 低频势力存在感时钟只绑定精确 profile + thread，不把档案本身变成剧情线。
    presence_debt = db.Column(
        db.Integer, nullable=False, default=0, server_default='0', index=True,
    )
    presence_cooldown_runs = db.Column(
        db.Integer, nullable=False, default=0, server_default='0',
    )
    last_presence_log_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'faction_profile_id', 'world_story_thread_id',
            name='uq_advs1_faction_profile_thread',
        ),
        db.Index(
            'idx_advs1_faction_thread_lookup',
            'world_story_thread_id', 'relation_role',
        ),
        db.Index(
            'idx_advs1_faction_presence_due',
            'presence_debt', 'presence_cooldown_runs',
        ),
        {'extend_existing': True},
    )


class AdventurerS1FactionProfileHistory(db.Model):
    """势力档案的不可覆盖版本快照，用于回看 AI 渐进补全的来源。"""

    __tablename__ = 'adventurer_s1_faction_profile_history'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    faction_profile_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_faction_profiles.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    revision = db.Column(db.Integer, nullable=False)
    snapshot = db.Column(db.JSON, nullable=False)
    change_summary = db.Column(db.Text, nullable=False, default='')
    source_candidate_ids = db.Column(db.JSON, nullable=True)
    source_log_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.UniqueConstraint(
            'faction_profile_id', 'revision',
            name='uq_advs1_faction_profile_history_revision',
        ),
        {'extend_existing': True},
    )


class AdventurerS1WorldActor(db.Model):
    """跨副本持续发展的世界角色；保存有界当前态，不保存流水账。"""

    __tablename__ = 'adventurer_s1_world_actors'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    canonical_name = db.Column(db.String(120), nullable=False, unique=True, index=True)
    aliases = db.Column(db.JSON, nullable=True)
    actor_type = db.Column(db.String(30), nullable=False, default='individual', index=True)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    identity_summary = db.Column(db.Text, nullable=False, default='')
    stable_principles = db.Column(db.Text, nullable=True)
    power_tier = db.Column(db.String(30), nullable=True, index=True)
    power_boundary = db.Column(db.Text, nullable=True)
    capability_profile = db.Column(db.JSON, nullable=True)
    capability_revision = db.Column(db.Integer, nullable=False, default=0)
    capability_source_log_id = db.Column(db.Integer, nullable=True, index=True)
    long_term_goal = db.Column(db.Text, nullable=True)
    current_agenda = db.Column(db.Text, nullable=True)
    current_state = db.Column(db.Text, nullable=True)
    scope_keys = db.Column(db.JSON, nullable=True)
    trigger_keywords = db.Column(db.JSON, nullable=True)
    importance = db.Column(db.Integer, nullable=False, default=50, index=True)
    source_candidate_ids = db.Column(db.JSON, nullable=True)
    # 旧生产库的 expedition_logs.id 不保证存在可供 FK 引用的唯一约束；审计来源使用弱引用。
    first_seen_log_id = db.Column(db.Integer, nullable=True, index=True)
    last_seen_log_id = db.Column(db.Integer, nullable=True, index=True)
    last_active_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('idx_advs1_world_actor_status_importance', 'status', 'importance', 'updated_at'),
        {'extend_existing': True},
    )

    def to_admin_dict(self) -> dict:
        return {
            'id': self.id,
            'canonical_name': self.canonical_name,
            'aliases': self.aliases or [],
            'actor_type': self.actor_type,
            'status': self.status,
            'identity_summary': self.identity_summary or '',
            'stable_principles': self.stable_principles or '',
            'power_tier': self.power_tier or '',
            'power_boundary': self.power_boundary or '',
            'capability_profile': self.capability_profile,
            'capability_revision': int(self.capability_revision or 0),
            'capability_source_log_id': self.capability_source_log_id,
            'long_term_goal': self.long_term_goal or '',
            'current_agenda': self.current_agenda or '',
            'current_state': self.current_state or '',
            'scope_keys': self.scope_keys or [],
            'trigger_keywords': self.trigger_keywords or [],
            'importance': self.importance,
            'source_candidate_ids': self.source_candidate_ids or [],
            'first_seen_log_id': self.first_seen_log_id,
            'last_seen_log_id': self.last_seen_log_id,
            'last_active_at': to_utc_iso(self.last_active_at) if self.last_active_at else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1FateJourney(db.Model):
    """15 级命途旅程。进度与突破资格由后端权威保存，叙事内容由 AI 生成。"""

    __tablename__ = 'adventurer_s1_fate_journeys'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True,
    )
    status = db.Column(db.String(24), nullable=False, default='awaiting_choice', index=True)
    route_type = db.Column(db.String(24), nullable=True, index=True)
    route_title = db.Column(db.String(120), nullable=True)
    route_summary = db.Column(db.Text, nullable=True)
    reflection = db.Column(db.Text, nullable=True)
    choice_options = db.Column(db.JSON, nullable=True)
    current_clue = db.Column(db.Text, nullable=True)
    candidate_dungeons = db.Column(db.JSON, nullable=True)
    chapter_index = db.Column(db.Integer, nullable=False, default=0)
    key_event_count = db.Column(db.Integer, nullable=False, default=0)
    related_expedition_count = db.Column(db.Integer, nullable=False, default=0)
    miss_count = db.Column(db.Integer, nullable=False, default=0)
    echo_count = db.Column(db.Integer, nullable=False, default=0)
    visited_dungeons = db.Column(db.JSON, nullable=True)
    breakthrough_completed = db.Column(db.Boolean, nullable=False, default=False, index=True)
    final_outcome = db.Column(db.JSON, nullable=True)
    final_expedition_log_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    choice_generated_at = db.Column(db.DateTime, nullable=True)
    choice_generation_token = db.Column(db.String(64), nullable=True, index=True)
    choice_generation_started_at = db.Column(db.DateTime, nullable=True)
    chosen_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_fate_status_chapter', 'status', 'chapter_index'),
        {'extend_existing': True},
    )

    def to_player_dict(self, *, include_events: bool = False) -> dict:
        data = {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'status': self.status,
            'route_type': self.route_type,
            'route_title': self.route_title,
            'route_summary': self.route_summary,
            'reflection': self.reflection,
            'choice_options': self.choice_options or [],
            'current_clue': self.current_clue,
            'candidate_dungeons': self.candidate_dungeons or [],
            'chapter_index': int(self.chapter_index or 0),
            'key_event_count': int(self.key_event_count or 0),
            'related_expedition_count': int(self.related_expedition_count or 0),
            'echo_count': int(self.echo_count or 0),
            'visited_dungeons': self.visited_dungeons or [],
            'breakthrough_completed': bool(self.breakthrough_completed),
            'final_outcome': self.final_outcome,
            'chosen_at': to_utc_iso(self.chosen_at) if self.chosen_at else None,
            'completed_at': to_utc_iso(self.completed_at) if self.completed_at else None,
        }
        if include_events:
            data['events'] = [row.to_player_dict() for row in self.events]
        return data


class AdventurerS1FateResetRequest(db.Model):
    """管理员发起、由绑定玩家确认的命途重置审计记录。"""

    __tablename__ = 'adventurer_s1_fate_reset_requests'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    journey_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_fate_journeys.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    requested_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    reason = db.Column(db.Text, nullable=True)
    journey_snapshot = db.Column(db.JSON, nullable=False, default=dict)
    requested_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    responded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_fate_reset_adv_status', 'adventurer_id', 'status'),
    )

    def to_player_dict(self) -> dict:
        return {
            'id': self.id,
            'status': self.status,
            'reason': self.reason or '',
            'requested_at': to_utc_iso(self.requested_at) if self.requested_at else None,
        }

    def to_admin_dict(self) -> dict:
        return {
            **self.to_player_dict(),
            'journey_id': self.journey_id,
            'adventurer_id': self.adventurer_id,
            'requested_by_user_id': self.requested_by_user_id,
            'responded_at': to_utc_iso(self.responded_at) if self.responded_at else None,
        }


class AdventurerS1FateEvent(db.Model):
    """命途旅程中的普通追查、线索回响与关键事件审计记录。"""

    __tablename__ = 'adventurer_s1_fate_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    journey_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_fate_journeys.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    expedition_log_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'),
        nullable=True, unique=True, index=True,
    )
    chapter_index = db.Column(db.Integer, nullable=False, default=0)
    dungeon_name = db.Column(db.String(100), nullable=False)
    event_type = db.Column(db.String(20), nullable=False, index=True)  # ordinary/echo/key
    result_status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    title = db.Column(db.String(120), nullable=True)
    summary = db.Column(db.Text, nullable=True)
    payload = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    journey = db.relationship('AdventurerS1FateJourney', backref=db.backref(
        'events', lazy='select', cascade='all, delete-orphan', order_by='AdventurerS1FateEvent.id',
    ))

    def to_player_dict(self) -> dict:
        return {
            'id': self.id,
            'expedition_log_id': self.expedition_log_id,
            'chapter_index': self.chapter_index,
            'dungeon_name': self.dungeon_name,
            'event_type': self.event_type,
            'result_status': self.result_status,
            'title': self.title,
            'summary': self.summary,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1WorldActorFactionRelation(db.Model):
    """世界角色对酒馆或其他势力的描述式复杂态度，不压缩成单一数值。"""

    __tablename__ = 'adventurer_s1_world_actor_faction_relations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_world_actors.id', ondelete='CASCADE'), nullable=False, index=True)
    faction_key = db.Column(db.String(120), nullable=False, index=True)
    faction_name = db.Column(db.String(160), nullable=False)
    public_stance = db.Column(db.Text, nullable=True)
    private_view = db.Column(db.Text, nullable=True)
    interests = db.Column(db.Text, nullable=True)
    concerns = db.Column(db.Text, nullable=True)
    dependencies = db.Column(db.Text, nullable=True)
    unresolved_matters = db.Column(db.Text, nullable=True)
    behavior_boundary = db.Column(db.Text, nullable=True)
    last_change_summary = db.Column(db.Text, nullable=True)
    source_log_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now, index=True)

    __table_args__ = (
        db.UniqueConstraint('actor_id', 'faction_key', name='uq_advs1_actor_faction_relation'),
        db.Index('idx_advs1_actor_faction_updated', 'actor_id', 'updated_at'),
        {'extend_existing': True},
    )

    def to_prompt_dict(self) -> dict:
        return {
            'faction': self.faction_name,
            'public_stance': self.public_stance or '',
            'private_view': self.private_view or '',
            'interests': self.interests or '',
            'concerns': self.concerns or '',
            'dependencies': self.dependencies or '',
            'unresolved_matters': self.unresolved_matters or '',
            'behavior_boundary': self.behavior_boundary or '',
        }


class AdventurerS1WorldStoryThreadActor(db.Model):
    """公共世界线与长期角色的显式关联；关键词只负责召回，不负责猜身份。"""

    __tablename__ = 'adventurer_s1_world_story_thread_actors'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 存量 world_story_threads.id 无数据库唯一约束；由唯一关联键与服务存在性校验保证引用。
    world_story_thread_id = db.Column(db.Integer, nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_world_actors.id', ondelete='CASCADE'), nullable=False, index=True)
    involvement = db.Column(db.String(20), nullable=False, default='direct', index=True)
    narrative_role = db.Column(db.String(40), nullable=True)
    lease_required = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('world_story_thread_id', 'actor_id', name='uq_advs1_world_thread_actor'),
        db.Index('idx_advs1_world_thread_actor_lease', 'world_story_thread_id', 'lease_required'),
        {'extend_existing': True},
    )


class AdventurerS1WorldActorInteraction(db.Model):
    """已结算的角色交集审计；完整历史不进入常规探险 prompt。"""

    __tablename__ = 'adventurer_s1_world_actor_interactions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_world_actors.id', ondelete='CASCADE'), nullable=False, index=True)
    expedition_log_id = db.Column(db.Integer, nullable=True, index=True)
    world_story_thread_id = db.Column(db.Integer, nullable=True, index=True)
    interaction_summary = db.Column(db.Text, nullable=False, default='')
    actor_change_summary = db.Column(db.Text, nullable=True)
    relation_change_summary = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('actor_id', 'expedition_log_id', 'world_story_thread_id', name='uq_advs1_actor_interaction_source'),
        db.Index('idx_advs1_actor_interaction_recent', 'actor_id', 'created_at'),
        {'extend_existing': True},
    )


class AdventurerS1WorldCountermeasureKnowledge(db.Model):
    """有学习能力的长期个体与其结构化关联受众的聚合适应知识。"""

    __tablename__ = 'adventurer_s1_world_countermeasure_knowledge'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    first_source_expedition_log_id = db.Column(db.Integer, nullable=False)
    last_source_expedition_log_id = db.Column(db.Integer, nullable=False)
    # Last encounter is a version/idempotency marker, not necessarily the log
    # supporting the retained highest-level evidence. Legacy provenance is unknown.
    evidence_source_expedition_log_id = db.Column(db.Integer, nullable=True)
    observer_world_actor_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_world_actors.id', ondelete='CASCADE'),
        nullable=False,
    )
    observer_capability_revision = db.Column(db.Integer, nullable=False)
    audience_key = db.Column(db.String(140), nullable=False)
    audience_type = db.Column(db.String(20), nullable=False)
    faction_key = db.Column(db.String(120), nullable=True)
    learning_fingerprint = db.Column(db.String(64), nullable=False)
    source_refs = db.Column(db.JSON, nullable=False)
    encountered_method = db.Column(db.Text, nullable=False)
    unresolved_reason = db.Column(db.Text, nullable=False)
    knowledge_level = db.Column(db.String(20), nullable=False)
    evidence_event_indexes = db.Column(db.JSON, nullable=False)
    evidence_event_quotes = db.Column(db.JSON, nullable=True)
    observed_effect = db.Column(db.Text, nullable=False)
    learned_boundary = db.Column(db.Text, nullable=False)
    countermeasure_direction = db.Column(db.Text, nullable=True)
    encounter_count = db.Column(db.Integer, nullable=False, default=1)
    last_opportunity_expedition_log_id = db.Column(db.Integer, nullable=True)
    last_opportunity_source_expedition_log_id = db.Column(
        db.Integer, nullable=True,
    )
    last_opportunity_at = db.Column(db.DateTime, nullable=True)
    opportunity_count = db.Column(
        db.Integer, nullable=False, default=0, server_default='0',
    )
    last_application_expedition_log_id = db.Column(db.Integer, nullable=True)
    last_applied_source_expedition_log_id = db.Column(db.Integer, nullable=True)
    application_count = db.Column(
        db.Integer, nullable=False, default=0, server_default='0',
    )
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'audience_key',
            'learning_fingerprint',
            name='uq_advs1_countermeasure_audience_method',
        ),
        db.Index(
            'idx_advs1_countermeasure_audience_recent',
            'audience_key',
            'updated_at',
        ),
        db.Index(
            'ix_advs1_countermeasure_first_source_log',
            'first_source_expedition_log_id',
        ),
        db.Index(
            'ix_advs1_countermeasure_last_source_log',
            'last_source_expedition_log_id',
        ),
        db.Index('ix_advs1_countermeasure_observer', 'observer_world_actor_id'),
        db.Index('ix_advs1_countermeasure_audience_type', 'audience_type'),
        db.Index('ix_advs1_countermeasure_faction', 'faction_key'),
        db.Index('ix_advs1_countermeasure_level', 'knowledge_level'),
        db.Index('ix_advs1_countermeasure_count', 'encounter_count'),
        db.Index(
            'ix_advs1_countermeasure_last_opportunity_log',
            'last_opportunity_expedition_log_id',
        ),
        db.Index(
            'ix_advs1_countermeasure_last_application_log',
            'last_application_expedition_log_id',
        ),
        db.Index(
            'idx_advs1_countermeasure_opportunity_due',
            'audience_type',
            'last_opportunity_at',
            'observer_world_actor_id',
        ),
        db.Index('ix_advs1_countermeasure_created', 'created_at'),
        db.Index('ix_advs1_countermeasure_updated', 'updated_at'),
        {'extend_existing': True},
    )


# 探险日志 generating 阶段的 AI 流水线标记（status=in_progress/completed/failed 时应为 null）
S1_EXPEDITION_GENERATION_PHASE_MAIN_AI = 'main_ai'
S1_EXPEDITION_GENERATION_PHASE_OBJECTIVE_OUTLINE_AI = 'objective_outline_ai'
S1_EXPEDITION_GENERATION_PHASE_EVENT_RESOLUTION_AI = 'event_resolution_ai'
S1_EXPEDITION_GENERATION_PHASE_REVIEW_AI = 'review_ai'
S1_EXPEDITION_GENERATION_PHASE_SETTLEMENT_GUARD_AI = 'settlement_guard_ai'
S1_EXPEDITION_GENERATION_PHASE_APPLY_RESULT = 'apply_result'
S1_EXPEDITION_ACTIVE_GENERATION_PHASES = frozenset({
    S1_EXPEDITION_GENERATION_PHASE_MAIN_AI,
    S1_EXPEDITION_GENERATION_PHASE_OBJECTIVE_OUTLINE_AI,
    S1_EXPEDITION_GENERATION_PHASE_EVENT_RESOLUTION_AI,
    S1_EXPEDITION_GENERATION_PHASE_REVIEW_AI,
    S1_EXPEDITION_GENERATION_PHASE_SETTLEMENT_GUARD_AI,
    S1_EXPEDITION_GENERATION_PHASE_APPLY_RESULT,
})


class AdventurerS1ExpeditionLog(db.Model):
    """探险日志。status 提升为独立索引列，便于直接 SQL 过滤卡住的 generating 日志。"""

    __tablename__ = 'adventurer_s1_expedition_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    party_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_parties.id', ondelete='SET NULL'), nullable=True, index=True)
    party_name = db.Column(db.String(100), nullable=True)
    target_dungeon = db.Column(db.String(100), nullable=False)
    # 常规副本仍保留为组队/委托归属；命中隐藏入口时，此列记录实际叙事与世界情报地点。
    hidden_dungeon = db.Column(db.String(100), nullable=True, index=True)
    temporary_location_name = db.Column(db.String(100), nullable=True)
    result_summary = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='generating', index=True)  # generating/in_progress/completed/failed
    # 从 full_log 的结构化特殊事件字段归一化而来，供列表筛选与冷压缩日志读取。
    # NULL 表示旧日志尚未分类；空字符串表示已分类且不是特殊事件。
    special_event_theme = db.Column(db.String(32), nullable=True, default='', index=True)
    # generating 期间记录大纲/事实裁决/正文/审核/Guard/写回阶段；worker 心跳刷新 generation_phase_at。
    generation_phase = db.Column(db.String(20), nullable=True, index=True)
    generation_phase_at = db.Column(db.DateTime, nullable=True, index=True)
    # 当前生成 worker 所属的后端启动实例；新实例可立即接管旧实例遗留请求。
    generation_owner_boot_id = db.Column(db.String(128), nullable=True)
    full_log = db.Column(ColdExpeditionLogJSON, nullable=False)  # 7 天后无损压缩，ORM 读取时透明还原
    cold_compressed_at = db.Column(db.DateTime, nullable=True, index=True)
    # PostgreSQL trigger maintains this internal hot/cold projection atomically.
    # Deferred so ordinary log reads/serializers do not fetch another JSON field.
    player_name_history = db.deferred(db.Column(db.JSON(none_as_null=True), nullable=True))
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_summary_dict(self):
        """列表/动态流用的轻量序列化：不含体积庞大的 full_log。"""
        location_visible = self.status in ('in_progress', 'completed')
        return {
            'id': self.id,
            'party_id': self.party_id,
            'party_name': self.party_name,
            'target_dungeon': self.target_dungeon if location_visible else '',
            'hidden_dungeon': self.hidden_dungeon if location_visible else None,
            'temporary_location_name': self.temporary_location_name if location_visible else None,
            'result_summary': self.result_summary,
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }

    def to_dict(self, is_admin=False):
        safe_full_log = self.full_log
        if not is_admin and isinstance(safe_full_log, dict) and any(
            key in safe_full_log
            for key in (
                'member_updates',
                'waiting_player_npc_injection',
                'cross_realm_mirror_injection',
                'waiting_npc_updates',
                'waiting_npc_persistence',
                'waiting_npc_review_quarantine',
                '_generation_checkpoint',
                '_member_id_to_name',
                '_reviewed_companion_stage_floors',
                '_companion_lineage_context',
                '_hidden_dungeon_reward_plan',
                '_ecology_identity_snapshot',
                '_ecology_identity_review',
                '_objective_mode_snapshot',
                '_expedition_locus_snapshot',
                '_campaign_departure_decision',
                '_temporary_location',
                '_countermeasure_application_receipts',
                '_countermeasure_learning_owner',
                '_countermeasure_learning_disposition',
                '_waiting_npc_review_disposition',
                'countermeasure_learning',
                '_settlement_conflict_repairs',
                '_same_name_gain_review_receipt',
                '_review_failure_quarantine_receipt',
                '_commission_reconciliation_receipt',
                'countermeasure_opportunity_persistence',
                'countermeasure_application_persistence',
                'countermeasure_learning_persistence',
                'objective_outline',
                'hidden_dungeon_context',
            )
        ):
            import copy
            safe_full_log = copy.deepcopy(self.full_log)
            for update in (safe_full_log.get('member_updates') or []):
                update.pop('exp_gain', None)
                update.pop('exp', None)
                # 好感度具体数值（delta）不向玩家暴露；玩家侧的变好/变坏
                # 走 get_log_detail 顶层 relationship_changes 的 direction 字段
                update.pop('relationship_changes', None)
            safe_full_log.pop('waiting_player_npc_injection', None)
            safe_full_log.pop('cross_realm_mirror_injection', None)
            safe_full_log.pop('waiting_npc_updates', None)
            safe_full_log.pop('waiting_npc_persistence', None)
            safe_full_log.pop('waiting_npc_review_quarantine', None)
            safe_full_log.pop('_generation_checkpoint', None)
            safe_full_log.pop('_member_id_to_name', None)
            safe_full_log.pop('_reviewed_companion_stage_floors', None)
            safe_full_log.pop('_companion_lineage_context', None)
            safe_full_log.pop('_hidden_dungeon_reward_plan', None)
            safe_full_log.pop('_ecology_identity_snapshot', None)
            safe_full_log.pop('_ecology_identity_review', None)
            safe_full_log.pop('_objective_mode_snapshot', None)
            safe_full_log.pop('_expedition_locus_snapshot', None)
            safe_full_log.pop('_campaign_departure_decision', None)
            safe_full_log.pop('_temporary_location', None)
            safe_full_log.pop('_countermeasure_application_receipts', None)
            safe_full_log.pop('_countermeasure_learning_owner', None)
            safe_full_log.pop('_countermeasure_learning_disposition', None)
            safe_full_log.pop('_waiting_npc_review_disposition', None)
            safe_full_log.pop('countermeasure_learning', None)
            safe_full_log.pop('_settlement_conflict_repairs', None)
            safe_full_log.pop('_same_name_gain_review_receipt', None)
            safe_full_log.pop('_review_failure_quarantine_receipt', None)
            safe_full_log.pop('_commission_reconciliation_receipt', None)
            safe_full_log.pop('countermeasure_opportunity_persistence', None)
            safe_full_log.pop('countermeasure_application_persistence', None)
            safe_full_log.pop('countermeasure_learning_persistence', None)
            safe_full_log.pop('objective_outline', None)
            hidden_context = safe_full_log.get('hidden_dungeon_context')
            if isinstance(hidden_context, dict):
                hidden_context.pop('code', None)

        return {
            'id': self.id,
            'party_id': self.party_id,
            'party_name': self.party_name,
            'target_dungeon': self.target_dungeon,
            'hidden_dungeon': self.hidden_dungeon,
            'temporary_location_name': self.temporary_location_name if self.status in ('in_progress', 'completed') else None,
            'result_summary': self.result_summary,
            'status': self.status,
            'full_log': safe_full_log,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1ExpeditionLogMember(db.Model):
    """探险日志参与者索引表，用于按冒险者快速查询历史日志。"""

    __tablename__ = 'adventurer_s1_expedition_log_members'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expedition_log_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('expedition_log_id', 'adventurer_id', name='uq_advs1_expedition_log_member'),
        db.Index('ix_advs1_expedition_log_member_adv_log', 'adventurer_id', 'expedition_log_id'),
        {'extend_existing': True},
    )


class AdventurerS1ExpeditionLike(db.Model):
    """玩家在最近一次探险结算中给队友的点赞记录。"""

    __tablename__ = 'adventurer_s1_expedition_likes'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    expedition_log_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    from_adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    to_adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('from_adventurer_id', 'expedition_log_id', name='uq_advs1_expedition_like_once'),
        db.Index('ix_advs1_expedition_like_to_adv', 'to_adventurer_id', 'created_at'),
        {'extend_existing': True},
    )

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'expedition_log_id': self.expedition_log_id,
            'from_adventurer_id': self.from_adventurer_id,
            'to_adventurer_id': self.to_adventurer_id,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1ExpeditionFavorite(db.Model):
    """玩家收藏的探险日志；每个玩家对同一日志最多一条记录。"""

    __tablename__ = 'adventurer_s1_expedition_favorites'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    expedition_log_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'expedition_log_id', name='uq_advs1_expedition_favorite_user_log'),
        db.Index('ix_advs1_expedition_favorite_user_created', 'user_id', 'created_at'),
        {'extend_existing': True},
    )


class AdventurerS1ExpeditionComment(db.Model):
    """玩家对探险事件段落或成员状态更新留下的评论。"""

    __tablename__ = 'adventurer_s1_expedition_comments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 旧 PostgreSQL 库中的探险日志 id 没有可供新表引用的 PK/UNIQUE；
    # 评论写入口会先精确加载并校验日志，因此这里保留为带复合索引的弱引用。
    expedition_log_id = db.Column(
        db.Integer,
        nullable=False,
    )
    target_type = db.Column(db.String(24), nullable=False)
    target_index = db.Column(db.Integer, nullable=False)
    # 本地遗留 users.id 同样没有可供 PostgreSQL 外键引用的 PK/UNIQUE。
    # 发表入口会在同一事务中锁定并验证作者；这里用弱引用兼容旧库。
    user_id = db.Column(db.Integer, nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    # 仅由已认证管理员软删除入口写入，保持弱引用以免新表依赖遗留约束。
    deleted_by = db.Column(db.Integer, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship(
        'User',
        primaryjoin='foreign(AdventurerS1ExpeditionComment.user_id) == User.id',
        viewonly=True,
        lazy='raise',
        uselist=False,
    )

    __table_args__ = (
        db.CheckConstraint(
            "target_type IN ('event', 'member_update')",
            name='ck_advs1_exp_comment_target_type',
        ),
        db.CheckConstraint('target_index >= 0', name='ck_advs1_exp_comment_target_index'),
        db.Index(
            'ix_advs1_exp_comment_target',
            'expedition_log_id',
            'is_deleted',
            'target_type',
            'target_index',
            'created_at',
            'id',
        ),
        db.Index(
            'ix_advs1_exp_comment_user_created',
            'user_id',
            'created_at',
            'id',
        ),
        {'extend_existing': True},
    )

    def to_public_dict(self):
        return {
            'id': self.id,
            'nickname': (self.user.nickname or self.user.username) if self.user else '未知',
            'active_title': (
                self.user.active_title
                if self.user and hasattr(self.user, 'active_title')
                else None
            ),
            'content': self.content,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1Item(db.Model):
    """物品图鉴（全局目录）。装备/仓库物品的通用描述统一来自这里，避免逐个冒险者重复存。"""

    __tablename__ = 'adventurer_s1_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    type = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=False, default='')
    rarity = db.Column(db.String(20), nullable=False, default='common')
    # AI 只声明一次激活后的统一恢复时长（分钟）；实际计时保存在物品实例上。
    recovery_minutes = db.Column(db.Integer, nullable=False, default=0)
    # 探险使用条件（JSON，不进 AI prompt）：如 {"expedition_dungeon": "哥布林矿坑"}
    use_condition = db.Column(db.JSON, nullable=True)
    is_starter = db.Column(db.Boolean, nullable=False, default=False, index=True)  # 是否为创建时可选的初始物品（如初始武器）
    world_unique = db.Column(db.Boolean, nullable=False, default=False, index=True)  # 全服唯一：活体伙伴/命名幼崽等
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    shop_item = db.relationship(
        'AdventurerS1ShopItem', back_populates='item',
        uselist=False, cascade='all, delete-orphan',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'description': self.description,
            'rarity': self.rarity,
            'recovery_minutes': max(0, int(self.recovery_minutes or 0)),
            'is_starter': self.is_starter,
            'world_unique': bool(self.world_unique),
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }

    def to_admin_dict(self):
        """管理员视图：额外返回 use_condition 等内部字段。"""
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            catalog_default_uses_left,
            catalog_has_default_uses_left,
            is_catalog_text_locked,
            is_hidden_boss_clue_item,
        )

        d = self.to_dict()
        d['use_condition'] = self.use_condition
        d['text_locked'] = is_catalog_text_locked(self.use_condition)
        if catalog_has_default_uses_left(self.use_condition):
            d['default_uses_left'] = catalog_default_uses_left(self.use_condition)
        if is_hidden_boss_clue_item(self.use_condition):
            d['is_hidden_boss_clue'] = True
        return d


class AdventurerS1DescriptionAiAssetVersion(db.Model):
    """携带资产最新需求指针；内容缓存与执行租约仍按 source_hash 共享。"""

    __tablename__ = 'adventurer_s1_description_ai_asset_versions'
    realm_code = db.Column(db.String(32), primary_key=True)
    asset_key = db.Column(db.String(128), primary_key=True)
    source_hash = db.Column(db.String(64), nullable=False)
    observed_at = db.Column(db.DateTime, nullable=False)
    __table_args__ = (
        db.Index('ix_advs1_desc_asset_source', 'realm_code', 'source_hash'),
        {'extend_existing': True},
    )


class AdventurerS1DescriptionAiBacklog(db.Model):
    """实际携带资产的持久待办；执行租约仍由 DescriptionAiCache 拥有。"""

    __tablename__ = 'adventurer_s1_description_ai_backlog'
    realm_code = db.Column(db.String(32), primary_key=True)
    source_hash = db.Column(db.String(64), primary_key=True)
    source_item = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    next_check_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    __table_args__ = (
        db.Index('ix_advs1_desc_backlog_due', 'realm_code', 'next_check_at', 'source_hash'),
        {'extend_existing': True},
    )


class AdventurerS1DescriptionAiCache(db.Model):
    """按原文哈希复用的 AI 专用描述摘要；玩家侧始终读取原始完整描述。"""

    __tablename__ = 'adventurer_s1_description_ai_cache'

    source_hash = db.Column(db.String(64), primary_key=True)
    description_kind = db.Column(db.String(32), nullable=False)
    description_ai = db.Column(db.Text, nullable=False)
    # 仅 trait 使用；绑定同一 source_hash，审核通过前不得进入选择器。
    trait_context_rule = db.Column(db.JSON, nullable=True)
    # 内部审核结果；绑定 source_hash，不进入玩家资产序列化。
    ai_input_policy = db.Column(db.JSON, nullable=True)
    # 描述疑点复查的独立租约/版本回执，不改变当前缓存的可读状态。
    description_feedback_receipt = db.Column(db.JSON, nullable=True)
    trait_context_audit_status = db.Column(
        db.String(24), nullable=False, default='not_sampled',
    )  # not_sampled|passed|rejected|error
    # 路由复审的结构化权威回执；同时承载独立租约状态，避免污染摘要状态机。
    trait_context_audit_receipt = db.Column(db.JSON, nullable=True)
    trait_effect_polarity_audit_status = db.Column(
        db.String(24), nullable=False, default='not_sampled',
    )  # not_sampled|passed|rejected|error；与路由审核独立激活
    status = db.Column(
        db.String(24), nullable=False, default='active', index=True,
    )  # pending|processing|active|keep_original|retryable|audit_rejected
    failure_count = db.Column(db.Integer, nullable=False, default=0)
    # 长特质语义终态（保留原文/摘要拒绝/路由拒绝）的后台复查轮次；
    # 与网络、解析、审核基础设施失败使用的 failure_count 独立。
    terminal_retry_count = db.Column(db.Integer, nullable=False, default=0)
    next_retry_at = db.Column(db.DateTime, nullable=True)
    processing_token = db.Column(db.String(64), nullable=True)
    model_used = db.Column(db.String(120), nullable=True)
    audit_status = db.Column(
        db.String(24), nullable=False, default='not_sampled', index=True,
    )  # not_sampled(待审核/无需摘要)|passed|rejected|error
    audit_detail = db.Column(db.Text, nullable=True)
    audited_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index('ix_advs1_desc_ai_kind', 'description_kind'),
        db.Index(
            'ix_advs1_desc_processing_token', 'processing_token',
            postgresql_where=db.text('processing_token IS NOT NULL'),
            sqlite_where=db.text('processing_token IS NOT NULL'),
            info={'online_migration': 'migrations.add_observed_query_indexes'},
        ),
        {'extend_existing': True},
    )


class AdventurerS1SkillTemplate(db.Model):
    """初始技能模板（玩家创建冒险者时从中选 1 个）。

    模板是"可选技能目录"，玩家选定后会复制成该冒险者自己的 AdventurerS1Skill 实例；
    后续探险中习得的新技能不依赖模板，直接写实例表。
    """

    __tablename__ = 'adventurer_s1_skill_templates'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=False, default='')
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)  # 下架后不再出现在创建选项
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'is_active': self.is_active,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1ShopItem(db.Model):
    """商店在售物品，引用图鉴条目。"""

    __tablename__ = 'adventurer_s1_shop_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    item_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_items.id', ondelete='CASCADE'), nullable=False, unique=True)
    price = db.Column(db.Integer, nullable=False, default=0)
    stock = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    item = db.relationship('AdventurerS1Item', back_populates='shop_item')

    def to_dict(self):
        return {
            'id': self.id,
            'item_id': self.item_id,
            'name': self.item.name if self.item else None,
            'type': self.item.type if self.item else None,
            'description': self.item.description if self.item else '',
            'rarity': self.item.rarity if self.item else 'common',
            'price': self.price,
            'stock': self.stock,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1DiningMenuItem(db.Model):
    """深渊食堂菜单项：固定价 + 下次探险一次性临时加成。"""

    __tablename__ = 'adventurer_s1_dining_menu_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    name = db.Column(db.String(50), nullable=False)
    icon = db.Column(db.String(10), nullable=False, default='🍽️')
    description = db.Column(db.String(200), nullable=False, default='')
    gold_cost = db.Column(db.Integer, nullable=False, default=0)
    buff_stat = db.Column(db.String(20), nullable=False)  # 主 buff（兼容/列表展示）
    buff_amount = db.Column(db.Integer, nullable=False, default=1)
    buffs = db.Column(db.JSON, nullable=True)  # [{stat, amount}, ...] 多属性加成
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_dict(self) -> dict:
        return {
            'code': self.code,
            'name': self.name,
            'icon': self.icon,
            'description': self.description,
            'gold_cost': int(self.gold_cost or 0),
            'buff_stat': self.buff_stat,
            'buff_amount': int(self.buff_amount or 0),
            'buffs': self.buffs if isinstance(self.buffs, list) else None,
            'sort_order': int(self.sort_order or 0),
        }


class AdventurerS1Trait(db.Model):
    """冒险者特质实例（按角色归属）。描述与归档/合并元数据均结构化为列。"""

    __tablename__ = 'adventurer_s1_traits'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False, default='')
    description_ai = db.Column(db.Text, nullable=True)  # 主探险 AI prompt 用机制摘要（≤150 字）
    description_ai_at = db.Column(db.DateTime, nullable=True)
    description_ai_source_hash = db.Column(db.String(64), nullable=True)  # description 全文 SHA256，用于失效检测
    lore_entries = db.Column(db.JSON, nullable=True)  # 追加式见证履历 [{text, source, log_id?, at}]
    # NULL 表示迁移前旧数据，发车时仅按玩家现有资产完整名称做确定性映射；
    # [] 表示生成侧已明确声明为独立特质。
    asset_dependencies = db.Column(db.JSON, nullable=True)
    is_archived = db.Column(db.Boolean, nullable=False, default=False)  # 旧 JSON 的 hidden
    equipped = db.Column(
        db.Boolean, nullable=False, default=True,
    )  # 仅审核通过的纯正面特质允许玩家设为 False；其它特质始终按生效处理
    is_merged_trait = db.Column(db.Boolean, nullable=False, default=False)
    merged_from = db.Column(db.JSON, nullable=True)   # 被合并来源特质名列表
    merged_into = db.Column(db.String(100), nullable=True)
    merge_generated_by = db.Column(db.String(50), nullable=True)
    recovery_policy = db.Column(db.String(20), nullable=False, default='none')
    recovery_stage = db.Column(db.String(20), nullable=True)
    recovery_progress = db.Column(db.Integer, nullable=False, default=0)
    recovery_required = db.Column(db.Integer, nullable=True)
    recovery_hours = db.Column(db.Integer, nullable=True)  # trace 级：1~4 小时墙钟后自行消退
    recovery_expires_at = db.Column(db.DateTime, nullable=True)
    recovery_rest_baseline_seconds = db.Column(db.Integer, nullable=True)  # 启用宿舍周期恢复时的静养秒快照，防止追溯消耗旧囤积
    recovery_rest_baseline_persistent = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    recovered_desc = db.Column(db.Text, nullable=True)
    archived_reason = db.Column(db.Text, nullable=True)
    archived_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_trait_adv_archived', 'adventurer_id', 'is_archived'),
        {'extend_existing': True},
    )

    adventurer = db.relationship('AdventurerS1', back_populates='traits')

    def to_dict(self):
        return {
            'name': self.name,
            'desc': self.description,
            'is_archived': self.is_archived,
            'is_merged_trait': self.is_merged_trait,
            'merged_from': self.merged_from or [],
            'merged_into': self.merged_into,
            'merge_generated_by': self.merge_generated_by,
            'recovery_policy': self.recovery_policy,
            'recovery_stage': self.recovery_stage,
            'recovery_progress': self.recovery_progress,
            'recovery_required': self.recovery_required,
            'recovery_hours': self.recovery_hours,
            'recovery_expires_at': to_utc_iso(self.recovery_expires_at) if self.recovery_expires_at else None,
            'recovered_desc': self.recovered_desc,
            'archived_reason': self.archived_reason,
            'archived_at': to_utc_iso(self.archived_at) if self.archived_at else None,
        }


class AdventurerS1Equipment(db.Model):
    """冒险者随身装备/道具实例。通用描述走图鉴，仅个性化描述写 custom_desc。"""

    __tablename__ = 'adventurer_s1_equipment'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False, default='')
    rarity = db.Column(db.String(20), nullable=False, default='common')
    custom_desc = db.Column(db.Text, nullable=True)  # 升级/附魔/改造的个性化描述；None 表示用图鉴通用描述
    uses_left = db.Column(db.Integer, nullable=True)  # 消耗品剩余次数
    damage_stage = db.Column(db.String(20), nullable=True)  # 完好/开裂/濒毁（历史兼容）
    evolution_stage = db.Column(db.Integer, nullable=False, default=0)  # companion 已完成的进化/孵化阶段
    recovery_minutes = db.Column(db.Integer, nullable=False, default=0)
    recovery_started_at = db.Column(db.DateTime, nullable=True)
    recovery_ends_at = db.Column(db.DateTime, nullable=True, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    # 仅保护玩家主动处置；不进入 AI 语义快照，也不限制剧情/系统权威写入。
    is_player_locked = db.Column(db.Boolean, nullable=False, default=False)
    damage_note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('ix_advs1_equipment_adv_name', 'adventurer_id', 'name'),
        {'extend_existing': True},
    )

    adventurer = db.relationship('AdventurerS1', back_populates='equipment_items')


class AdventurerS1WarehouseItem(db.Model):
    """冒险者收藏室（仓库）物品。结构同装备；同名允许多件，以 id 区分实例。"""

    __tablename__ = 'adventurer_s1_warehouse_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False, default='')
    rarity = db.Column(db.String(20), nullable=False, default='common')
    custom_desc = db.Column(db.Text, nullable=True)
    uses_left = db.Column(db.Integer, nullable=True)
    evolution_stage = db.Column(db.Integer, nullable=False, default=0)  # companion 伙伴进化阶位
    recovery_minutes = db.Column(db.Integer, nullable=False, default=0)
    recovery_started_at = db.Column(db.DateTime, nullable=True)
    recovery_ends_at = db.Column(db.DateTime, nullable=True, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    # 与随身物品一致；作为堆叠等价键，锁定堆不会并入未锁定堆。
    is_player_locked = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('ix_advs1_warehouse_adv_name', 'adventurer_id', 'name'),
        {'extend_existing': True},
    )

    adventurer = db.relationship('AdventurerS1', back_populates='warehouse_items')


class AdventurerS1Skill(db.Model):
    """冒险者技能实例（按角色归属）。"""

    __tablename__ = 'adventurer_s1_skills'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False, default='')
    equipped = db.Column(db.Boolean, nullable=False, default=True)  # 是否已携带（False 表示在「未携带」池里）
    recovery_minutes = db.Column(db.Integer, nullable=False, default=0)
    recovery_started_at = db.Column(db.DateTime, nullable=True)
    recovery_ends_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('adventurer_id', 'name', name='uq_advs1_skill_adv_name'),
        {'extend_existing': True},
    )

    adventurer = db.relationship('AdventurerS1', back_populates='skill_items')


class AdventurerS1Investor(db.Model):
    """冒险者投资人记录（旧 investors JSON {投资人名: 累计金额} 的规范化）。"""

    __tablename__ = 'adventurer_s1_investors'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    investor_name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False, default=0.0)  # 累计投资金额
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('adventurer_id', 'investor_name', name='uq_advs1_investor_adv_name'),
        {'extend_existing': True},
    )

    adventurer = db.relationship('AdventurerS1', back_populates='investor_items')


class AdventurerS1Follow(db.Model):
    """玩家关注冒险者。"""

    __tablename__ = 'adventurer_s1_follows'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'adventurer_id', name='uq_advs1_follow'),
        {'extend_existing': True},
    )


class AdventurerS1Claim(db.Model):
    """玩家认领冒险者（绑定为 owner）的申请。"""

    __tablename__ = 'adventurer_s1_claims'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending/approved/rejected
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    reject_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'adventurer_id', name='uq_advs1_claim'),
        {'extend_existing': True},
    )

    def to_dict(self, user_map: dict | None = None, adv_name_map: dict | None = None):
        """序列化。批量场景请预取 user_map={uid: User} 和 adv_name_map={aid: name} 传入，避免 N+1。"""
        if user_map is not None:
            user = user_map.get(self.user_id)
        else:
            from models.user import User
            user = User.query.session.get(User, self.user_id)

        if adv_name_map is not None:
            adv_name = adv_name_map.get(self.adventurer_id) if self.adventurer_id else None
        else:
            adv = AdventurerS1.query.session.get(AdventurerS1, self.adventurer_id) if self.adventurer_id else None
            adv_name = adv.name if adv else None

        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': user.username if user else '未知',
            'nickname': user.nickname if user else None,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': adv_name or '未知',
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'reviewed_at': to_utc_iso(self.reviewed_at) if self.reviewed_at else None,
            'reject_reason': self.reject_reason,
        }


class AdventurerS1ChatMessage(db.Model):
    """深渊酒馆 S1 聊天消息。"""

    __tablename__ = 'adventurer_s1_chat_messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    content = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    user = db.relationship('User')

    __table_args__ = {'extend_existing': True}

    def to_dict(self):
        return {
            'id': self.id,
            'nickname': (self.user.nickname or self.user.username) if self.user else '未知',
            'active_title': self.user.active_title if self.user else None,
            'content': self.content,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1TavernSeat(db.Model):
    """酒馆大堂座位：固定布局由 seed 写入，占座 / 点酒状态记录在行上。

    座位是全服共享资源：吧台单人座 + 多人桌（同 table_key 的座位在 UI 上画成一桌）。
    入座不改变冒险者 status（坐着喝酒仍算 resting 回精力）；冒险者被组队 / 探险 /
    建造拉走后由后台座位任务清座；列表接口只读。点酒纯娱乐无数值效果，但真扣金币。
    """

    __tablename__ = 'adventurer_s1_tavern_seats'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    seat_key = db.Column(db.String(50), nullable=False, unique=True)  # seed 幂等标识，如 bar_1 / table_a_1
    zone = db.Column(db.String(20), nullable=False)  # bar(吧台单人座) / table(多人桌座位)
    table_key = db.Column(db.String(50), nullable=True)  # 同桌分组标识；吧台座位为 None
    table_label = db.Column(db.String(50), nullable=True)  # 桌子展示名，如「双人桌 A」
    label = db.Column(db.String(50), nullable=False)  # 座位展示名，如「吧台 1 号」
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'), nullable=True, index=True)
    seated_at = db.Column(db.DateTime, nullable=True)
    drink_key = db.Column(db.String(50), nullable=True)  # 当前点的酒（S1_TAVERN_DRINKS 的 key）
    drink_text = db.Column(db.String(200), nullable=True)  # 点酒时从文案池随机抽到的神态文案
    ordered_at = db.Column(db.DateTime, nullable=True)
    food_key = db.Column(db.String(50), nullable=True)
    food_text = db.Column(db.String(200), nullable=True)
    food_started_at = db.Column(db.DateTime, nullable=True)
    food_expires_at = db.Column(db.DateTime, nullable=True, index=True)
    food_settled_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_dict(self, adventurer=None, drink=None, food=None):
        """座位信息。关联展示数据由 service 批量预构建传入，避免 N+1。"""
        return {
            'id': self.id,
            'zone': self.zone,
            'table_key': self.table_key,
            'table_label': self.table_label,
            'label': self.label,
            'adventurer': adventurer,
            'drink': drink,
            'food': food,
        }


class AdventurerS1TeaHouseSeat(db.Model):
    """深渊茶舍座位：两桌各 4 座，入座扣 30 金币，精力按 2/分钟恢复。

    与酒馆座位独立运营：茶舍按 2/分钟、酒馆按基础 1/分钟恢复精力，座位布局由 seed 写入。
    入座不改变冒险者 status（仍算 resting）；超 6 小时或精力回满时由 scheduler 清座。
    """

    __tablename__ = 'adventurer_s1_tea_house_seats'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    seat_key = db.Column(db.String(50), nullable=False, unique=True)  # seed 幂等标识
    table_key = db.Column(db.String(50), nullable=False)  # 桌分组 tea_a / tea_b
    table_label = db.Column(db.String(50), nullable=False)  # 桌展示名
    label = db.Column(db.String(50), nullable=False)  # 座位展示名
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'), nullable=True, index=True)
    seated_at = db.Column(db.DateTime, nullable=True)  # 入座时间（超时判定）
    stamina_settled_at = db.Column(db.DateTime, nullable=True)  # 上次茶舍精力结算时间
    entry_fee_paid = db.Column(db.Boolean, nullable=False, default=False)  # 是否已付入场费
    tea_key = db.Column(db.String(50), nullable=True)  # 当前点的茶（S1_TEA_HOUSE_TEAS 的 key）
    tea_text = db.Column(db.String(200), nullable=True)  # 点茶时从文案池随机抽到的神态文案
    ordered_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_dict(self, adventurer=None, tea=None, locked=False):
        """座位信息。adventurer / tea 由 service 批量预构建传入，避免 N+1。

        locked=True 表示该桌需茶舍升至 Lv2 才开放（由 service 按 upgrade_level 判定）。
        """
        return {
            'id': self.id,
            'table_key': self.table_key,
            'table_label': self.table_label,
            'label': self.label,
            'locked': locked,
            'adventurer': adventurer,
            'tea': tea,
        }


class AdventurerS1HotSpringSeat(db.Model):
    """深渊温泉座位：两个 4 人小池 + 一个 24 人大池，入座扣 200 金币，生命值 1/2 分钟、精力 1/1 分钟恢复。

    与茶舍座位独立运营：温泉同时回血回精力（茶舍仅回精力）。座位布局由 seed 写入。
    入座不改变冒险者 status（坐着仍算 resting）；超 6 小时、生命与精力均回满或被拉走后
    由后台恢复任务清座；列表接口只读。
    """

    __tablename__ = 'adventurer_s1_hot_spring_seats'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    seat_key = db.Column(db.String(50), nullable=False, unique=True)  # seed 幂等标识
    table_key = db.Column(db.String(50), nullable=False)  # 池分组 pool_a / pool_b / pool_big
    table_label = db.Column(db.String(50), nullable=False)  # 池展示名
    label = db.Column(db.String(50), nullable=False)  # 座位展示名
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'), nullable=True, index=True)
    seated_at = db.Column(db.DateTime, nullable=True)  # 入座时间（超时判定）
    hp_settled_at = db.Column(db.DateTime, nullable=True)  # 上次温泉回血结算基准
    stamina_settled_at = db.Column(db.DateTime, nullable=True)  # 上次温泉精力结算基准
    entry_fee_paid = db.Column(db.Boolean, nullable=False, default=False)  # 是否已付入场费
    drink_key = db.Column(db.String(50), nullable=True)  # 当前点的饮品（S1_HOT_SPRING_DRINKS 的 key）
    drink_text = db.Column(db.String(200), nullable=True)  # 点饮品时从文案池随机抽到的神态文案
    ordered_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_dict(self, adventurer=None, drink=None):
        """座位信息。adventurer / drink 由 service 批量预构建传入，避免 N+1。"""
        return {
            'id': self.id,
            'table_key': self.table_key,
            'table_label': self.table_label,
            'label': self.label,
            'adventurer': adventurer,
            'drink': drink,
        }


class AdventurerS1TavernTreatRequest(db.Model):
    """Tavern drink invitation. The payer is charged only when the target accepts."""

    __tablename__ = 'adventurer_s1_tavern_treat_requests'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    requester_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    payer_adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    payer_seat_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_tavern_seats.id', ondelete='CASCADE'), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    target_adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    target_seat_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_tavern_seats.id', ondelete='CASCADE'), nullable=False, index=True)
    drink_key = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)  # pending/accepted/declined/cancelled
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    responded_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('ix_s1_tavern_treat_target_status', 'target_user_id', 'status', 'created_at'),
        db.Index('ix_s1_tavern_treat_payer_status', 'requester_user_id', 'status', 'created_at'),
        {'extend_existing': True},
    )

    def to_dict(self, payer_name=None, target_name=None, drink=None):
        return {
            'id': self.id,
            'payer_adventurer_id': self.payer_adventurer_id,
            'payer_name': payer_name,
            'target_adventurer_id': self.target_adventurer_id,
            'target_name': target_name,
            'target_seat_id': self.target_seat_id,
            'drink_key': self.drink_key,
            'drink': drink,
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1DormRoom(db.Model):
    """宿舍房间：普通房可建造升级，room_key 非空者为系统专属房。"""

    __tablename__ = 'adventurer_s1_dorm_rooms'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    # NULL 表示普通宿舍；非空 key 仅供系统专属房间建立不可变身份。
    room_key = db.Column(db.String(50), nullable=True)
    capacity = db.Column(db.Integer, nullable=False)  # 床位数：4/2/1
    level = db.Column(db.Integer, nullable=False, default=1)  # 装修等级 1-5
    # 一次性命名权：宿舍楼完工时授予综合贡献最大的玩家（时长+材料各占 50%），用掉后 named_at 落时间戳
    naming_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    named_at = db.Column(db.DateTime, nullable=True)
    # Lv2 升级完工时：在住客中本次升级贡献最高者获得的一次性命名权
    upgrade_naming_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    upgrade_named_at = db.Column(db.DateTime, nullable=True)
    # 来源建筑实例（宿舍楼完工时记录，用于查看贡献者；旧数据为 NULL）
    building_instance_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_building_instances.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('uq_advs1_dorm_room_key', 'room_key', unique=True),
        {'extend_existing': True},
    )

    occupants = db.relationship(
        'AdventurerS1', foreign_keys='AdventurerS1.dorm_room_id',
        primaryjoin='AdventurerS1DormRoom.id == AdventurerS1.dorm_room_id',
        lazy='selectin', viewonly=True,
    )
    building_instance = db.relationship(
        'AdventurerS1BuildingInstance', foreign_keys=[building_instance_id],
        lazy='select', viewonly=True,
    )

    def to_dict(self, beds=None):
        """返回房间信息。beds 为预构建的床位列表时直接使用，否则从 occupants 构建。"""
        if beds is None:
            beds = [None] * self.capacity
            for adv in self.occupants:
                if adv.bed_index is not None and 0 <= adv.bed_index < self.capacity:
                    beds[adv.bed_index] = {
                        'adventurer_id': adv.id,
                        'adventurer_name': adv.name,
                        'owner_id': adv.owner_id,
                        'status': adv.status,
                        'building_instance_status': adv.building_instance.status if adv.building_instance else None,
                    }
        level = int(self.level or 1)
        result = {'id': self.id, 'name': self.name, 'capacity': self.capacity, 'level': level, 'beds': beds, 'naming_user_id': self.naming_user_id, 'naming_used': self.named_at is not None, 'upgrade_naming_user_id': self.upgrade_naming_user_id, 'upgrade_naming_used': self.upgrade_named_at is not None, 'building_instance_id': self.building_instance_id}
        bi = self.building_instance
        if bi:
            upgrade_level = int(getattr(bi, 'upgrade_level', 1) or 1)
            result['upgrade_level'] = upgrade_level
            if upgrade_level > level:
                result['level'] = upgrade_level
            result['building_status'] = bi.status
            upgrade_cfg = bi.template.upgrade_config if bi.template else None
            if upgrade_cfg:
                result['upgrade_description'] = upgrade_cfg.get('description')
        return result


class AdventurerS1BedSwapRequest(db.Model):
    """床位互换请求：玩家 A 发起请求，玩家 B 吡accept/reject。"""

    __tablename__ = 'adventurer_s1_bed_swap_requests'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    requester_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    requester_adv_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False)
    target_adv_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)  # pending/accepted/rejected/expired/cancelled
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)
    expires_at = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.Index('ix_bed_swap_req_target_status', 'target_adv_id', 'status'),
        {'extend_existing': True},
    )

    requester_adv = db.relationship('AdventurerS1', foreign_keys=[requester_adv_id])
    target_adv = db.relationship('AdventurerS1', foreign_keys=[target_adv_id])
    requester_user = db.relationship('User', foreign_keys=[requester_user_id])

    def to_dict(self):
        return {
            'id': self.id,
            'requester_user_id': self.requester_user_id,
            'requester_adv_id': self.requester_adv_id,
            'requester_adv_name': self.requester_adv.name if self.requester_adv else None,
            'requester_nickname': (self.requester_user.nickname or self.requester_user.username) if self.requester_user else None,
            'target_adv_id': self.target_adv_id,
            'target_adv_name': self.target_adv.name if self.target_adv else None,
            'target_room_id': self.target_adv.dorm_room_id if self.target_adv else None,
            'target_bed_index': self.target_adv.bed_index if self.target_adv else None,
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'expires_at': to_utc_iso(self.expires_at) if self.expires_at else None,
        }


class AdventurerS1Reservation(db.Model):
    """冒险者预约排队：玩家提交预约 → 管理员设优先级 → 抽取创建。"""

    __tablename__ = 'adventurer_s1_reservations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    gender = db.Column(db.String(10), nullable=False)
    personality = db.Column(db.String(100), nullable=False)
    skill_template_id = db.Column(db.Integer, nullable=False)
    item_ids = db.Column(db.JSON, nullable=False)  # [int, int]
    realm_code = db.Column(
        db.String(20), nullable=False, default='region_1',
        server_default=db.text("'region_1'"), index=True,
    )
    priority = db.Column(db.String(10), nullable=False, default='normal', index=True)  # high/elevated/normal/low
    priority_boost_until = db.Column(db.DateTime, nullable=True)  # 玩家临时提升优先级到期时间（仅 elevated 且由玩家触发时有值）
    last_owner_action_at = db.Column(db.DateTime, nullable=True)  # 玩家最近一次手动操作预约的时间
    restore_adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )  # 非空表示解封排队：抽中后恢复该封存冒险者，而非新建
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)  # pending/drawn/rejected/cancelled
    reject_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    user = db.relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': self.user.username if self.user else '未知',
            'nickname': (self.user.nickname or self.user.username) if self.user else None,
            'name': self.name,
            'gender': self.gender,
            'personality': self.personality,
            'skill_template_id': self.skill_template_id,
            'item_ids': self.item_ids or [],
            'realm_code': self.realm_code,
            'priority': self.priority,
            'priority_boost_until': to_utc_iso(self.priority_boost_until) if self.priority_boost_until else None,
            'restore_adventurer_id': self.restore_adventurer_id,
            'is_unarchive_restore': self.restore_adventurer_id is not None,
            'status': self.status,
            'reject_reason': self.reject_reason,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }

    def to_player_dict(self):
        """玩家侧预约状态：不暴露 priority 档位，仅返回临时提升相关可操作字段。"""
        from datetime import datetime

        now = _portable_utc_now()
        boost_active = (
            self.priority == 'elevated'
            and self.priority_boost_until is not None
            and self.priority_boost_until > now
        )
        can_boost = (
            self.status == 'pending'
            and self.priority == 'normal'
            and not boost_active
        )
        return {
            'id': self.id,
            'name': self.name,
            'gender': self.gender,
            'personality': self.personality,
            'skill_template_id': self.skill_template_id,
            'item_ids': self.item_ids or [],
            'realm_code': self.realm_code,
            'status': self.status,
            'is_unarchive_restore': self.restore_adventurer_id is not None,
            'restore_adventurer_id': self.restore_adventurer_id,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'priority_boost_active': boost_active,
            'priority_boost_until': to_utc_iso(self.priority_boost_until) if boost_active else None,
            'can_boost_priority': can_boost,
        }


class AdventurerS1RealmMembership(db.Model):
    """两区共享的玩家存活冒险者槽位。

    预约不会写入本表；只有抽中创建或解封时才先写入 ``pending`` 认领，
    随后以精确 claim token 完成 ``active``。每个 ``user_id`` 只有一行，
    因而一区与二区的独立调度器也不能同时生成两个存活角色。
    """

    __tablename__ = 'adventurer_s1_realm_memberships'

    # 历史 PostgreSQL 库的 users.id 没有可供新表引用的 PK/UNIQUE；中央槽位
    # 写入口会先锁定并验证 users 行。表自身以 user_id 为唯一主键。
    user_id = db.Column(db.Integer, primary_key=True)
    realm_code = db.Column(db.String(20), nullable=False, index=True)
    assignment_reason = db.Column(db.String(50), nullable=False)
    slot_state = db.Column(
        db.String(20), nullable=False, default='active',
        server_default=db.text("'active'"), index=True,
    )
    claim_token = db.Column(db.String(64), nullable=True, unique=True)
    claim_reservation_id = db.Column(db.Integer, nullable=True)
    active_adventurer_id = db.Column(db.Integer, nullable=True)
    # Stable full-inventory reconciliations must not rewrite every active row.
    # Only an actually missing slot records the start of its release grace.
    reconcile_absent_since = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
        server_default=db.func.now(),
    )

    __table_args__ = (
        db.CheckConstraint(
            "realm_code IN ('region_1', 'region_2')",
            name='ck_advs1_realm_membership_code',
        ),
        db.CheckConstraint(
            "slot_state IN ('pending', 'active')",
            name='ck_advs1_realm_membership_state',
        ),
        {'extend_existing': True},
    )


class AdventurerS1DeploymentIdentity(db.Model):
    """不可变的 S1 玩法数据库部署身份。

    该表与玩家/赛季数据无关。每个独立 S1 数据库只能存在 ``id=1`` 的一行，
    首次启动由部署进程认领；后续启动只能验证，不得把同一数据库改认成另一区服
    或另一个 Runtime。
    """

    __tablename__ = 'adventurer_s1_deployment_identity'

    id = db.Column(
        db.Integer,
        primary_key=True,
        default=1,
        server_default=db.text('1'),
    )
    realm_code = db.Column(db.String(20), nullable=False)
    deployment_id = db.Column(db.String(100), nullable=False)
    schema_revision = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.CheckConstraint('id = 1', name='ck_advs1_deployment_identity_singleton'),
        db.CheckConstraint(
            "realm_code IN ('region_1', 'region_2')",
            name='ck_advs1_deployment_identity_realm',
        ),
        db.CheckConstraint(
            'schema_revision >= 1',
            name='ck_advs1_deployment_identity_schema_revision',
        ),
        db.CheckConstraint(
            "length(deployment_id) BETWEEN 1 AND 100 "
            "AND deployment_id = trim(deployment_id) "
            "AND deployment_id = lower(deployment_id) "
            "AND deployment_id NOT LIKE '% %'",
            name='ck_advs1_deployment_identity_id_shape',
        ),
        {'extend_existing': True},
    )


class AdventurerS1RuntimeRelayNonce(db.Model):
    """跨 worker 原子消费的二区 Runtime relay 一次性 nonce。"""

    __tablename__ = 'adventurer_s1_runtime_relay_nonces'

    nonce_digest = db.Column(db.String(64), primary_key=True)
    expires_at = db.Column(db.BigInteger, nullable=False, index=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        server_default=db.func.now(),
    )

    __table_args__ = (
        db.CheckConstraint(
            "length(nonce_digest) = 64 "
            "AND nonce_digest = lower(nonce_digest)",
            name='ck_advs1_runtime_relay_nonce_digest',
        ),
        db.CheckConstraint(
            'expires_at > 0',
            name='ck_advs1_runtime_relay_nonce_expiry',
        ),
        {'extend_existing': True},
    )


class AdventurerS1WaitingNpcActor(db.Model):
    """待抽取玩家在世界中的稳定 NPC 身份；死亡后沿用 actor id 开启新一世。"""

    __tablename__ = 'adventurer_s1_waiting_npc_actors'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 本地/生产遗留 S1 表的 id 可能只有 NOT NULL、没有 PostgreSQL PK/UNIQUE；
    # 因此对旧表身份使用带索引的弱引用，并由服务层锁行后精确校验。
    reservation_id = db.Column(
        db.Integer,
        nullable=True,
        unique=True,
        index=True,
    )
    user_id = db.Column(db.Integer, nullable=False, index=True)
    display_name = db.Column(db.String(100), nullable=False)
    current_incarnation = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    faction_key = db.Column(db.String(120), nullable=True, index=True)
    faction_role = db.Column(db.String(30), nullable=True)
    current_profile = db.Column(db.JSON, nullable=False, default=dict)
    injection_count = db.Column(db.Integer, nullable=False, default=0)
    appearance_count = db.Column(db.Integer, nullable=False, default=0)
    last_injected_at = db.Column(db.DateTime, nullable=True, index=True)
    last_appeared_at = db.Column(db.DateTime, nullable=True)
    active_expedition_log_id = db.Column(db.Integer, nullable=True, index=True)
    active_lease_expires_at = db.Column(db.DateTime, nullable=True, index=True)
    materialized_adventurer_id = db.Column(
        db.Integer,
        nullable=True,
        index=True,
    )
    materialized_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index('ix_advs1_waiting_npc_status_faction', 'status', 'faction_key'),
        {'extend_existing': True},
    )


class AdventurerS1WaitingNpcRecord(db.Model):
    """待抽取玩家 NPC 的有界原文档案；incarnation 隔离同名角色的不同人生。"""

    __tablename__ = 'adventurer_s1_waiting_npc_records'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_waiting_npc_actors.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    incarnation = db.Column(db.Integer, nullable=False)
    expedition_log_id = db.Column(db.Integer, nullable=False, index=True)
    event_index = db.Column(db.Integer, nullable=False)
    record_index = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(30), nullable=False)
    excerpt = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint(
            'actor_id', 'incarnation', 'expedition_log_id', 'record_index',
            name='uq_advs1_waiting_npc_record_source',
        ),
        db.Index('ix_advs1_waiting_npc_record_actor_life', 'actor_id', 'incarnation'),
        {'extend_existing': True},
    )


class AdventurerS1WaitingNpcMaterialization(db.Model):
    """NPC 人生投影到正式冒险者的来源账本，供玩家一次性精确撤销。"""

    __tablename__ = 'adventurer_s1_waiting_npc_materializations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_waiting_npc_actors.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    incarnation = db.Column(db.Integer, nullable=False)
    adventurer_id = db.Column(
        db.Integer,
        nullable=False,
        unique=True,
        index=True,
    )
    profile_snapshot = db.Column(db.JSON, nullable=False, default=dict)
    inherited_history_entry = db.Column(db.Text, nullable=True)
    inherited_character_class = db.Column(db.String(100), nullable=True)
    equipment_ids = db.Column(db.JSON, nullable=False, default=list)
    skill_ids = db.Column(db.JSON, nullable=False, default=list)
    trait_ids = db.Column(db.JSON, nullable=False, default=list)
    status = db.Column(db.String(20), nullable=False, default='applied', index=True)
    reset_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'actor_id', 'incarnation',
            name='uq_advs1_waiting_npc_materialized_life',
        ),
        {'extend_existing': True},
    )


class AdventurerS1ArchiveAppeal(db.Model):
    """封存清退玩家的解封申请：管理员审核通过后进入预约队列，抽中后解封原冒险者。"""

    __tablename__ = 'adventurer_s1_archive_appeals'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True,
    )
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)  # pending/approved/rejected/cancelled
    reservation_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_reservations.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    reviewed_by_user_id = db.Column(db.Integer, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reject_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    user = db.relationship('User', foreign_keys=[user_id])
    adventurer = db.relationship('AdventurerS1', foreign_keys=[adventurer_id])

    def to_dict(self, *, include_admin: bool = False):
        adv = self.adventurer
        out = {
            'id': self.id,
            'user_id': self.user_id,
            'username': self.user.username if self.user else '未知',
            'nickname': (self.user.nickname or self.user.username) if self.user else None,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': adv.name if adv else None,
            'reason': self.reason,
            'status': self.status,
            'reservation_id': self.reservation_id,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
        if include_admin:
            out['reviewed_by_user_id'] = self.reviewed_by_user_id
            out['reviewed_at'] = to_utc_iso(self.reviewed_at) if self.reviewed_at else None
            out['reject_reason'] = self.reject_reason
            out['archive_reason'] = adv.archive_reason if adv else None
            out['archived_at'] = to_utc_iso(adv.archived_at) if adv and adv.archived_at else None
        return out

    def to_player_dict(self):
        adv = self.adventurer
        return {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': adv.name if adv else None,
            'reason': self.reason,
            'status': self.status,
            'reservation_id': self.reservation_id,
            'reject_reason': self.reject_reason if self.status == 'rejected' else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1Relationship(db.Model):
    """冒险者之间的好感度关系表。

    存储两个冒险者之间的关系值（-100 ~ 100），默认 0（陌生人）。
    通过共同下副本和探险事件改变。a_id 始终 < b_id 以避免重复。
    """

    __tablename__ = 'adventurer_s1_relationships'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_a_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_b_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    score = db.Column(db.Integer, nullable=False, default=0)  # 好感度 -100~100
    interaction_count = db.Column(db.Integer, nullable=False, default=0)  # 共同下副本次数
    last_interaction_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('adventurer_a_id', 'adventurer_b_id', name='uq_advs1_rel'),
        db.CheckConstraint('adventurer_a_id < adventurer_b_id', name='ck_advs1_rel_order'),
        {'extend_existing': True},
    )

    adventurer_a = db.relationship('AdventurerS1', foreign_keys=[adventurer_a_id])
    adventurer_b = db.relationship('AdventurerS1', foreign_keys=[adventurer_b_id])

    def to_dict(self):
        return {
            'id': self.id,
            'adventurer_a_id': self.adventurer_a_id,
            'adventurer_b_id': self.adventurer_b_id,
            'adventurer_a_name': self.adventurer_a.name if self.adventurer_a else None,
            'adventurer_b_name': self.adventurer_b.name if self.adventurer_b else None,
            'score': self.score,
            'interaction_count': self.interaction_count,
            'last_interaction_at': to_utc_iso(self.last_interaction_at) if self.last_interaction_at else None,
        }


class AdventurerS1AmbientNpcState(db.Model):
    """静态环境 NPC 与单个冒险者之间的隐藏关系状态。"""

    __tablename__ = 'adventurer_s1_ambient_npc_states'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 历史正式库的 adventurer_s1.id 可能缺可引用约束；沿用 S1 新表的弱整数引用策略。
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    npc_key = db.Column(db.String(64), nullable=False)
    affinity_score = db.Column(db.Integer, nullable=False, default=0)
    interaction_count = db.Column(db.Integer, nullable=False, default=0)
    # {"npc:action:tier": ["response-key", ...]}；服务层始终整体替换并限制长度。
    recent_text_keys = db.Column(db.JSON, nullable=False, default=dict)
    last_interaction_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'adventurer_id',
            'npc_key',
            name='uq_advs1_ambient_npc_state',
        ),
        db.CheckConstraint(
            'affinity_score BETWEEN -100 AND 100',
            name='ck_advs1_ambient_npc_affinity_range',
        ),
        {'extend_existing': True},
    )


class AdventurerS1AmbientNpcInteraction(db.Model):
    """环境 NPC 互动回执；保存完整文本与物品快照以实现幂等重放。"""

    __tablename__ = 'adventurer_s1_ambient_npc_interactions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 见 AmbientNpcState：由服务所有权校验与赛季重置维护引用完整性。
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    npc_key = db.Column(db.String(64), nullable=False)
    encounter_id = db.Column(db.String(64), nullable=False)
    surface_key = db.Column(db.String(32), nullable=False)
    action_key = db.Column(db.String(64), nullable=False)
    request_id = db.Column(db.String(64), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    template_revision = db.Column(db.Integer, nullable=False)
    # 仅带每日额度的动作填写；NULL 继续保留普通互动按偶遇幂等的语义。
    interaction_day = db.Column(db.Date, nullable=True)
    daily_limit_key = db.Column(db.String(32), nullable=True)
    response_key = db.Column(db.String(96), nullable=False)
    response_text = db.Column(db.Text, nullable=False)
    item_source = db.Column(db.String(20), nullable=True)
    item_id = db.Column(db.Integer, nullable=True)
    item_name = db.Column(db.String(100), nullable=True)
    item_type = db.Column(db.String(50), nullable=True)
    item_rarity = db.Column(db.String(20), nullable=True)
    item_snapshot_hash = db.Column(db.String(64), nullable=True)
    # 冻结实际被消耗的权威实例与完整语义文本；玩家投影不会返回此字段。
    item_snapshot = db.Column(db.JSON, nullable=True)
    item_remaining_quantity = db.Column(db.Integer, nullable=True)
    item_removed = db.Column(db.Boolean, nullable=True)
    # AI 只提议反应档位与展示文案；这些字段把回执接到缓存和 token 流水。
    ai_cache_key = db.Column(db.String(64), nullable=True)
    ai_result_source = db.Column(db.String(20), nullable=True)
    ai_affinity_effect = db.Column(db.String(24), nullable=True)
    ai_model = db.Column(db.String(100), nullable=True)
    ai_provider = db.Column(db.String(32), nullable=True)
    ai_profile_id = db.Column(db.String(64), nullable=True)
    ai_provider_request_id = db.Column(db.String(160), nullable=True)
    ai_failure_stage = db.Column(db.String(80), nullable=True)
    ai_response_texts = db.Column(db.JSON, nullable=True)
    # 仅保存无密钥运行元数据、Prompt hashes、finish/repair/error 等审计字段。
    ai_metadata = db.Column(db.JSON, nullable=True)
    affinity_delta = db.Column(db.Integer, nullable=False, default=0)
    affinity_before = db.Column(db.Integer, nullable=False)
    affinity_after = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint(
            'adventurer_id',
            'request_id',
            name='uq_advs1_ambient_npc_request',
        ),
        db.UniqueConstraint(
            'adventurer_id',
            'encounter_id',
            'action_key',
            name='uq_advs1_ambient_npc_action',
        ),
        db.UniqueConstraint(
            'adventurer_id',
            'npc_key',
            'daily_limit_key',
            'interaction_day',
            name='uq_advs1_ambient_npc_daily_action',
        ),
        db.Index(
            'ix_advs1_ambient_npc_encounter',
            'adventurer_id',
            'encounter_id',
        ),
        {'extend_existing': True},
    )


class AdventurerS1AmbientNpcGiftAiCache(db.Model):
    """环境 NPC 赠礼语义判断缓存；按模板、Prompt、好感档位与完整物品语义版本化。"""

    __tablename__ = 'adventurer_s1_ambient_npc_gift_ai_cache'

    cache_key = db.Column(db.String(64), primary_key=True)
    npc_key = db.Column(db.String(64), nullable=False, index=True)
    template_revision = db.Column(db.Integer, nullable=False)
    prompt_revision = db.Column(db.Integer, nullable=False)
    affinity_tier = db.Column(db.String(20), nullable=False)
    item_snapshot_hash = db.Column(db.String(64), nullable=False, index=True)
    # 这里仅保存发送给 AI 的语义投影，不保存玩家、库存行或来源位置标识。
    item_semantic_snapshot = db.Column(db.JSON, nullable=False)
    affinity_effect = db.Column(db.String(24), nullable=False)
    response_texts = db.Column(db.JSON, nullable=False)
    model = db.Column(db.String(100), nullable=True)
    provider = db.Column(db.String(32), nullable=True)
    profile_id = db.Column(db.String(64), nullable=True)
    provider_request_id = db.Column(db.String(160), nullable=True)
    origin_correlation_id = db.Column(db.String(64), nullable=True, index=True)
    prompt_hash = db.Column(db.String(64), nullable=True)
    system_prompt_hash = db.Column(db.String(64), nullable=True)
    contract_hash = db.Column(db.String(64), nullable=True)
    request_hash = db.Column(db.String(64), nullable=True)
    usage_snapshot = db.Column(db.JSON, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index(
            'ix_advs1_ambient_gift_cache_lookup',
            'npc_key',
            'affinity_tier',
            'item_snapshot_hash',
        ),
        {'extend_existing': True},
    )


class AdventurerS1MarketListing(db.Model):
    """个人交易市场挂单。

    挂单时物品从卖方仓库移除、字段快照托管在本行；取消退回仓库，成交转入买方仓库。
    两种模式：
    - barter（以物易物）：同稀有度物品互换，卖方手续费挂单时预扣，买方手续费成交时扣
    - sell（卖钱）：卖方标价，买方用金币购买，成交时收 20% 卖方手续费
    """

    __tablename__ = 'adventurer_s1_market_listings'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    seller_adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    # 托管物品快照（结构同 WarehouseItem）
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(50), nullable=False, default='')
    rarity = db.Column(db.String(20), nullable=False, default='common', index=True)
    custom_desc = db.Column(db.Text, nullable=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    uses_left = db.Column(db.Integer, nullable=True)
    evolution_stage = db.Column(db.Integer, nullable=False, default=0)
    recovery_minutes = db.Column(db.Integer, nullable=False, default=0)
    recovery_started_at = db.Column(db.DateTime, nullable=True)
    recovery_ends_at = db.Column(db.DateTime, nullable=True)
    # 挂单类型：barter=以物易物 / sell=卖钱
    listing_type = db.Column(db.String(20), nullable=False, default='barter', index=True)
    # sell 模式的售价（金币）；barter 模式为 NULL
    price = db.Column(db.Float, nullable=True)
    # barter 模式卖家备注想要什么（可选，max 200 字符）
    want_note = db.Column(db.String(200), nullable=True)
    # active=挂单中 / completed=已成交 / cancelled=已取消
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    # 成交信息
    buyer_adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'), nullable=True)
    traded_item_name = db.Column(db.String(100), nullable=True)  # 买方付出的物品名（barter）或 NULL（sell）
    seller_fee_paid = db.Column(db.Float, nullable=False, default=0)  # barter: 挂单时预扣；sell: 成交时写入
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index(
            'idx_advs1_market_listing_buyer_status',
            'buyer_adventurer_id', 'status',
        ),
        {'extend_existing': True},
    )

    seller = db.relationship('AdventurerS1', foreign_keys=[seller_adventurer_id])
    buyer = db.relationship('AdventurerS1', foreign_keys=[buyer_adventurer_id])

    def to_dict(self, item_dict=None, seller_name=None, include_trade_info=False, buyer_name=None):
        """市场列表用字段白名单；include_trade_info 仅「我的挂单」视图附带成交回执。"""
        desc = self.custom_desc or (item_dict or {}).get(self.item_name, '')
        data = {
            'id': self.id,
            'item_name': self.item_name,
            'item_type': self.item_type,
            'rarity': self.rarity,
            'quantity': int(self.quantity or 1),
            'uses_left': self.uses_left,
            'evolution_stage': int(self.evolution_stage or 0),
            'recovery_minutes': max(0, int(self.recovery_minutes or 0)),
            'desc': desc,
            'listing_type': self.listing_type or 'barter',
            'seller_adventurer_id': self.seller_adventurer_id,
            'seller_name': seller_name,
            'want_note': self.want_note,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
        if self.recovery_ends_at and self.recovery_ends_at > _portable_utc_now():
            data['recovering'] = True
            data['recovery_ends_at'] = to_utc_iso(self.recovery_ends_at)
        else:
            data['recovering'] = False
        if self.listing_type == 'sell' and self.price is not None:
            data['price'] = round(float(self.price), 2)
        if include_trade_info:
            data['status'] = self.status
            data['traded_item_name'] = self.traded_item_name
            data['buyer_name'] = buyer_name
            data['seller_fee_paid'] = round(float(self.seller_fee_paid or 0), 2)
            data['completed_at'] = to_utc_iso(self.completed_at) if self.completed_at else None
        return data


class AdventurerS1MarketOffer(db.Model):
    """以物易物出价记录。

    买家对 barter 挂单提交出价（物品快照留在本行，不从仓库移出）。
    卖家选定后原子校验仓库 + 互转物品。
    """
    __tablename__ = 'adventurer_s1_market_offers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    listing_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_market_listings.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    buyer_adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
                                    nullable=False, index=True)
    # 出价物品快照（结构同 WarehouseItem）
    offered_item_name = db.Column(db.String(100), nullable=False)
    offered_item_type = db.Column(db.String(50), nullable=False, default='')
    offered_rarity = db.Column(db.String(20), nullable=False, default='common')
    offered_custom_desc = db.Column(db.Text, nullable=True)
    offered_quantity = db.Column(db.Integer, nullable=False, default=1)
    # 期望换走的挂单数量；NULL=旧数据（成交时按 offered_quantity 计）
    listing_quantity = db.Column(db.Integer, nullable=True)
    offered_uses_left = db.Column(db.Integer, nullable=True)
    # NULL=新增该字段前的历史出价，不能伪装成 0 阶
    offered_evolution_stage = db.Column(db.Integer, nullable=True)
    # 出价时买家仓库物品 ID（accept 时用它原子校验+删除）
    source_warehouse_item_id = db.Column(db.Integer, nullable=True)
    # pending=待卖家选定 / accepted=已成交 / rejected=卖家选了别人 / cancelled=买家取消
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        # 部分唯一索引：同一买家对同一挂单只能有一个 pending 出价（PostgreSQL 部分索引）
        db.Index('uq_offer_listing_buyer_pending', 'listing_id', 'buyer_adventurer_id',
                 unique=True, postgresql_where=db.text("status = 'pending'")),
        {'extend_existing': True},
    )

    listing = db.relationship('AdventurerS1MarketListing', foreign_keys=[listing_id])
    buyer = db.relationship('AdventurerS1', foreign_keys=[buyer_adventurer_id])

    def to_dict(self, buyer_name=None, listing_summary=None):
        data = {
            'id': self.id,
            'listing_id': self.listing_id,
            'buyer_adventurer_id': self.buyer_adventurer_id,
            'buyer_name': buyer_name,
            'offered_item_name': self.offered_item_name,
            'offered_item_type': self.offered_item_type,
            'offered_rarity': self.offered_rarity,
            'offered_quantity': int(self.offered_quantity or 1),
            'listing_quantity': int(self.listing_quantity) if self.listing_quantity is not None else None,
            'offered_uses_left': self.offered_uses_left,
            'offered_evolution_stage': (
                max(0, int(self.offered_evolution_stage))
                if self.offered_evolution_stage is not None else None
            ),
            'offered_desc': self.offered_custom_desc or '',
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
        if listing_summary:
            data['listing'] = listing_summary
        return data


class AdventurerS1MarketBuyOrder(db.Model):
    """个人交易市场求购单。

    求购者按稀有度设置单件价格；物品不预先指定，由其他玩家展示仓库实例，
    求购者选中一条或多条展示后，在同一事务内完成扣款、物品转移和手续费结算。
    持续求购单在成交后保持 active，直到求购者手动取消。
    """

    __tablename__ = 'adventurer_s1_market_buy_orders'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 存量 PostgreSQL 的 adventurer_s1.id 可能只有 NOT NULL、没有 PK/UNIQUE；
    # 求购服务会在事务内锁定并校验冒险者，因此这里保留带索引的弱身份引用。
    buyer_adventurer_id = db.Column(
        db.Integer,
        nullable=False,
        index=True,
    )
    note = db.Column(db.String(200), nullable=True)
    common_price = db.Column(db.Float, nullable=True)
    rare_price = db.Column(db.Float, nullable=True)
    epic_price = db.Column(db.Float, nullable=True)
    legendary_price = db.Column(db.Float, nullable=True)
    keep_open = db.Column(db.Boolean, nullable=False, default=False)
    # active=求购中（含已成交但继续开放的持续求购）/ completed=一次性求购已成交 / cancelled=已取消
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    accepted_offer_id = db.Column(db.Integer, nullable=True)
    price_paid = db.Column(db.Float, nullable=True)
    seller_income = db.Column(db.Float, nullable=True)
    market_fee = db.Column(db.Float, nullable=False, default=0)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = {'extend_existing': True}

    def price_map(self):
        prices = {}
        for rarity in ('common', 'rare', 'epic', 'legendary'):
            value = getattr(self, f'{rarity}_price')
            if value is not None:
                prices[rarity] = round(float(value), 2)
        return prices

    def to_dict(
        self,
        buyer_name=None,
        accepted_offer=None,
        accepted_offers=None,
        include_trade_info=False,
    ):
        data = {
            'id': self.id,
            'buyer_adventurer_id': self.buyer_adventurer_id,
            'buyer_name': buyer_name,
            'note': self.note,
            'prices': self.price_map(),
            'keep_open': bool(self.keep_open),
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
        if include_trade_info:
            data.update({
                'status': self.status,
                'accepted_offer_id': self.accepted_offer_id,
                'price_paid': round(float(self.price_paid), 2) if self.price_paid is not None else None,
                'seller_income': (
                    round(float(self.seller_income), 2)
                    if self.seller_income is not None else None
                ),
                'market_fee': round(float(self.market_fee or 0), 2),
                'completed_at': (
                    to_utc_iso(self.completed_at)
                    if self.completed_at else None
                ),
            })
            if accepted_offer:
                data['accepted_offer'] = accepted_offer
            if accepted_offers is not None:
                data['accepted_offers'] = accepted_offers
        return data


class AdventurerS1MarketBuyOffer(db.Model):
    """向求购单展示的仓库物品。

    展示不移出仓库；同一仓库实例可同时展示给不同求购者。任一求购成交时，
    服务层锁住该仓库实例，并仅撤下展示数量已超过成交后剩余库存的跨单 pending 展示。
    """

    __tablename__ = 'adventurer_s1_market_buy_offers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    buy_order_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_market_buy_orders.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    seller_adventurer_id = db.Column(
        db.Integer,
        nullable=False,
        index=True,
    )
    source_warehouse_item_id = db.Column(db.Integer, nullable=False, index=True)
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(50), nullable=False, default='')
    rarity = db.Column(db.String(20), nullable=False, default='common', index=True)
    custom_desc = db.Column(db.Text, nullable=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    uses_left = db.Column(db.Integer, nullable=True)
    # NULL=新增该字段前的历史展示，不能伪装成 0 阶
    evolution_stage = db.Column(db.Integer, nullable=True)
    # pending=待求购者选择 / accepted=已成交 / rejected=同单未选中或剩余库存不足 / cancelled=展示者取消
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index(
            'uq_market_buy_offer_order_item_pending',
            'buy_order_id',
            'source_warehouse_item_id',
            unique=True,
            postgresql_where=db.text("status = 'pending'"),
            sqlite_where=db.text("status = 'pending'"),
        ),
        db.Index(
            'ix_market_buy_offer_source_status',
            'source_warehouse_item_id',
            'status',
        ),
        {'extend_existing': True},
    )

    buy_order = db.relationship('AdventurerS1MarketBuyOrder', foreign_keys=[buy_order_id])

    def to_dict(self, seller_name=None, order_summary=None):
        data = {
            'id': self.id,
            'buy_order_id': self.buy_order_id,
            'seller_adventurer_id': self.seller_adventurer_id,
            'seller_name': seller_name,
            'item_name': self.item_name,
            'item_type': self.item_type,
            'rarity': self.rarity,
            'desc': self.custom_desc or '',
            'quantity': int(self.quantity or 1),
            'uses_left': self.uses_left,
            'evolution_stage': (
                max(0, int(self.evolution_stage))
                if self.evolution_stage is not None else None
            ),
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
        if order_summary:
            data['buy_order'] = order_summary
        return data


class AdventurerS1SecretAuctionLot(db.Model):
    """隐秘拍卖行拍品（管理员上架，密封出价，到期结算）。"""

    __tablename__ = 'adventurer_s1_secret_auction_lots'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(50), nullable=False, default='')
    custom_desc = db.Column(db.Text, nullable=True)
    rarity = db.Column(db.String(20), nullable=False, default='common', index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    uses_left = db.Column(db.Integer, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=False, index=True)
    # 底价：最高出价须 >= 此值才成交；0 表示无底价
    reserve_price = db.Column(db.Float, nullable=False, default=0)
    # open=竞拍中 / settled_won=成交 / settled_pass=流拍或无出价 / cancelled=管理员取消
    status = db.Column(db.String(20), nullable=False, default='open', index=True)
    winner_adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'), nullable=True)
    winning_amount = db.Column(db.Float, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    winner = db.relationship('AdventurerS1', foreign_keys=[winner_adventurer_id])
    bids = db.relationship(
        'AdventurerS1SecretAuctionBid',
        back_populates='lot',
        lazy='select',
        cascade='all, delete-orphan',
    )

    def _reserve_price_value(self) -> float:
        return round(float(self.reserve_price or 0), 2)

    def to_public_dict(self, bid_count: int = 0, my_bid_amount=None):
        """进行中拍品（玩家）：他人只见 bid_count；仅本人可见 my_bid_amount。"""
        data = {
            'id': self.id,
            'item_name': self.item_name,
            'item_type': self.item_type or '',
            'desc': self.custom_desc or '',
            'rarity': self.rarity,
            'quantity': int(self.quantity or 1),
            'ends_at': to_utc_iso(self.ends_at) if self.ends_at else None,
            'bid_count': int(bid_count or 0),
            'reserve_price': self._reserve_price_value(),
        }
        if my_bid_amount is not None:
            data['my_bid_amount'] = round(float(my_bid_amount), 2)
        return data

    def to_history_dict(self, bid_count: int = 0, winner_name=None, my_result=None):
        """已结束拍品（玩家）：展示结果与中标者名称，不含任何价格或内部 ID。"""
        data = {
            'id': self.id,
            'item_name': self.item_name,
            'desc': self.custom_desc or '',
            'rarity': self.rarity,
            'quantity': int(self.quantity or 1),
            'ends_at': to_utc_iso(self.ends_at) if self.ends_at else None,
            'status': self.status,
            'bid_count': int(bid_count or 0),
        }
        if self.status == 'settled_won' and winner_name:
            data['winner_name'] = winner_name
        if my_result:
            data['my_result'] = my_result
        return data

    def to_admin_dict(self, bid_count: int = 0, bids=None):
        """管理员视图：含出价明细与成交价。"""
        if self.status == 'open':
            data = self.to_public_dict(bid_count=bid_count)
            data['status'] = self.status
        else:
            data = self.to_history_dict(bid_count=bid_count)
            data['item_type'] = self.item_type or ''
            data['reserve_price'] = self._reserve_price_value()
            data['winner_adventurer_id'] = self.winner_adventurer_id
            if self.winning_amount is not None:
                data['winning_amount'] = round(float(self.winning_amount), 2)
        data['created_by_user_id'] = self.created_by_user_id
        data['created_at'] = to_utc_iso(self.created_at) if self.created_at else None
        if self.uses_left is not None:
            data['uses_left'] = self.uses_left
        if bids is not None:
            data['bids'] = [b.to_admin_dict() for b in bids]
        return data


class AdventurerS1SecretAuctionBid(db.Model):
    """隐秘拍卖行出价记录（密封，仅本人可见金额）。"""

    __tablename__ = 'adventurer_s1_secret_auction_bids'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    lot_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_secret_auction_lots.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    # held=托管中 / won=中标扣款 / refunded=已退还
    status = db.Column(db.String(20), nullable=False, default='held', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('lot_id', 'adventurer_id', name='uq_advs1_secret_auction_bid'),
        {'extend_existing': True},
    )

    lot = db.relationship('AdventurerS1SecretAuctionLot', back_populates='bids')
    adventurer = db.relationship('AdventurerS1')

    def to_admin_dict(self):
        return {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': self.adventurer.name if self.adventurer else None,
            'amount': round(float(self.amount or 0), 2),
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1GamblingRound(db.Model):
    """深渊赌坊轮次：每建筑每分钟一轮，到期统一掷骰结算。"""

    __tablename__ = 'adventurer_s1_gambling_rounds'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    building_instance_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_building_instances.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    round_epoch = db.Column(db.BigInteger, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='betting', index=True)  # betting / settled / cancelled
    closes_at = db.Column(db.DateTime, nullable=False)
    dice1 = db.Column(db.Integer, nullable=True)
    dice2 = db.Column(db.Integer, nullable=True)
    dice_sum = db.Column(db.Integer, nullable=True)
    outcome = db.Column(db.String(10), nullable=True)  # high / low / house / cancelled
    settled_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('building_instance_id', 'round_epoch', name='uq_advs1_gambling_round'),
        {'extend_existing': True},
    )

    building_instance = db.relationship('AdventurerS1BuildingInstance')
    bets = db.relationship(
        'AdventurerS1GamblingBet',
        back_populates='round',
        lazy='selectin',
        cascade='all, delete-orphan',
    )


class AdventurerS1GamblingBet(db.Model):
    """深渊赌坊单轮下注记录（公开可见）。"""

    __tablename__ = 'adventurer_s1_gambling_bets'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    round_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_gambling_rounds.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    bet_amount = db.Column(db.Integer, nullable=False)
    choice = db.Column(db.String(10), nullable=False)  # high / low
    won = db.Column(db.Boolean, nullable=True)
    payout = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('round_id', 'adventurer_id', name='uq_advs1_gambling_bet'),
        {'extend_existing': True},
    )

    round = db.relationship('AdventurerS1GamblingRound', back_populates='bets')
    adventurer = db.relationship('AdventurerS1')

    def to_public_dict(self):
        adv = self.adventurer
        return {
            'adventurer_id': self.adventurer_id,
            'adventurer_name': adv.name if adv else None,
            'bet_amount': int(self.bet_amount or 0),
            'choice': self.choice,
            'choice_label': '大' if self.choice == 'high' else '小',
            'won': self.won,
            'payout': int(self.payout or 0),
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


# ── 建筑系统 ──────────────────────────────────────────────────────────────────


class AdventurerS1BuildingTemplate(db.Model):
    """建筑定义模板（管理员创建和管理）。"""

    __tablename__ = 'adventurer_s1_building_templates'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(50), nullable=False, unique=True, index=True)  # 稳定标识，改名不影响种子匹配
    name = db.Column(db.String(50), nullable=False, unique=True)
    icon = db.Column(db.String(10), nullable=False, default='🏗️')
    description = db.Column(db.Text, nullable=False, default='')
    building_type = db.Column(db.String(20), nullable=False, default='unique')  # unique=唯一建筑, multiple=可多个
    category = db.Column(db.String(20), nullable=False, default='general')  # general/dormitory/medical/leisure/equipment/memorial/...
    build_hours = db.Column(db.Integer, nullable=False, default=4)  # 建造所需小时数
    helper_slots = db.Column(db.Integer, nullable=False, default=2)  # 可参与帮忙的冒险者位置数
    stamina_cost_per_help = db.Column(db.Integer, nullable=False, default=20)  # 每次帮忙消耗精力
    speedup_minutes = db.Column(db.Integer, nullable=False, default=15)  # 每次帮忙减少的建造分钟数
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    settlement_config = db.Column(db.JSON, nullable=True)  # 结算奖励配置，null 时用分类默认值
    # 处理器标识：指向代码中的具体 handler，null 时走 settlement_config / category 默认值
    handler_key = db.Column(db.String(30), nullable=True, index=True)
    # 宿舍楼专用：完工后自动新增的床位数（非宿舍模板为 NULL）
    produced_beds = db.Column(db.Integer, nullable=True)
    # 宿舍楼专用：开工前需要扣减的材料，如 {'wood': 80, 'stone': 60}（非宿舍模板为 NULL）
    required_materials = db.Column(db.JSON, nullable=True)
    # 升级配置：根级字段兼容 Lv2；max_level + levels 可追加更高等级配置。
    # 为 None 表示该建筑不可升级。
    upgrade_config = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    instances = db.relationship('AdventurerS1BuildingInstance', back_populates='template', lazy='selectin')

    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'icon': self.icon,
            'description': self.description,
            'building_type': self.building_type,
            'category': self.category,
            'build_hours': self.build_hours,
            'helper_slots': self.helper_slots,
            'stamina_cost_per_help': self.stamina_cost_per_help,
            'speedup_minutes': self.speedup_minutes,
            'is_active': self.is_active,
            'sort_order': self.sort_order,
            'settlement_config': self.settlement_config,
            'handler_key': self.handler_key,
            'produced_beds': self.produced_beds,
            'required_materials': self.required_materials,
            'upgrade_config': self.upgrade_config,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1BuildingInstance(db.Model):
    """建筑实例：跟踪建造状态。唯一建筑每模板一个实例，可多个建筑每模板可多个实例。"""

    __tablename__ = 'adventurer_s1_building_instances'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    template_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_building_templates.id', ondelete='CASCADE'), nullable=False, index=True)
    custom_name = db.Column(db.String(50), nullable=True)  # 自定义名称（如宿舍的命名）
    status = db.Column(db.String(20), nullable=False, default='locked', index=True)  # locked/building/built
    build_started_at = db.Column(db.DateTime, nullable=True)
    build_complete_at = db.Column(db.DateTime, nullable=True)
    accumulated_progress_seconds = db.Column(db.Integer, nullable=False, default=0)  # 已累积的建造进度（秒）
    # 玩家自建宿舍楼的申请人；GM 创建或历史实例为 NULL
    applicant_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    disabled = db.Column(db.Boolean, nullable=False, default=False, index=True)
    # 建筑等级与升级状态
    upgrade_level = db.Column(db.Integer, nullable=False, default=1)  # 当前等级，上限由 upgrade_config 决定
    upgrade_started_at = db.Column(db.DateTime, nullable=True)  # 升级开始时间（status='upgrading' 时有效）
    upgrade_complete_at = db.Column(db.DateTime, nullable=True)  # 升级预计完成时间
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    template = db.relationship('AdventurerS1BuildingTemplate', back_populates='instances')
    helpers = db.relationship('AdventurerS1BuildingHelper', back_populates='building_instance', lazy='selectin', cascade='all, delete-orphan')
    # 捐献明细：locked 阶段累计、cancel 时退材料、自动转 building 时聚合判断进度
    donations = db.relationship('AdventurerS1BuildingDonation', backref='building_instance', lazy='select', cascade='all, delete-orphan')

    _SHOP_HANDLER_KEYS = frozenset({'herbalist_shop', 'armory_shop', 'forge_supply_shop', 'limited_shop'})

    _CATEGORY_DEFAULTS = {
        'gathering': {'reward_type': 'material', 'reward_amount': 10, 'usage_stamina_cost': 30, 'usage_duration_minutes': 30, 'description': '采集所得'},
        'training': {'reward_type': 'exp', 'reward_amount': 1, 'usage_stamina_cost': 60, 'usage_duration_minutes': 60, 'daily_limit': 1, 'description': '训练完成'},
        'leisure': {'reward_type': 'gold', 'reward_amount': 5, 'description': '休闲放松后获得零花钱'},
        'medical': {'reward_type': 'hp', 'reward_amount': 20, 'description': '治疗后恢复生命值'},
        'equipment': {'reward_type': 'gold', 'reward_amount': 8, 'description': '工作报酬'},
    }

    def _category_default_settlement(self):
        """当模板 settlement_config 为 null 时，按分类返回默认值。"""
        cat = self.template.category if self.template else 'general'
        return self._CATEGORY_DEFAULTS.get(cat, {}).copy() or {}

    def _effective_settlement_config(self):
        """序列化用的结算配置：模板显式配置 > 商店专用 > 分类默认。"""
        if not self.template:
            return {}
        if self.template.settlement_config:
            return dict(self.template.settlement_config)
        if self.template.handler_key in self._SHOP_HANDLER_KEYS:
            return {
                'reward_type': 'none',
                'reward_amount': 0,
                'description': '挑选商品购买，物品直接进入冒险者随身，无需派遣占位',
            }
        return self._category_default_settlement()

    def is_donation_open(self) -> bool:
        """locked 阶段是否对玩家开放材料捐献。

        宿舍楼、花店等须管理员点「开放玩家捐献」设 build_started_at；其余 seed 建筑
        （含个人交易市场）有 required_materials 即默认开放。
        """
        if self.status != 'locked' or not self.template or not self.template.required_materials:
            return False
        if (
            self.template.category == 'dormitory'
            or self.template.code in _BUILDING_ADMIN_GATED_DONATION_CODES
        ):
            return self.build_started_at is not None
        return True

    def _template_to_dict(
        self,
        reveal_usage: bool,
        for_list: bool = False,
        *,
        for_client: bool = True,
    ) -> dict:
        """模板子对象序列化。未建成且非管理员视图时不暴露 handler / 结算配置。

        Args:
            for_list: 列表接口轻量模式，省略 upgrade_config 等大字段；description 保留供卡片展示。
            for_client: 玩家 API 时裁剪 settlement_config 内部字段。
        """
        t = self.template
        data = {
            'id': t.id,
            'code': t.code,
            'name': t.name,
            'icon': t.icon,
            'description': t.description,
            'building_type': t.building_type,
            'category': t.category,
            'build_hours': t.build_hours,
            'helper_slots': t.helper_slots,
            'stamina_cost_per_help': t.stamina_cost_per_help,
            'speedup_minutes': t.speedup_minutes,
            'produced_beds': t.produced_beds,
            'required_materials': t.required_materials,
        }
        if not for_list:
            if for_client:
                from plugins.adventurer_s1.services.s1_building_client_sanitize import (
                    sanitize_upgrade_config_for_client,
                )
                data['upgrade_config'] = sanitize_upgrade_config_for_client(t.upgrade_config)
            else:
                data['upgrade_config'] = t.upgrade_config
        if reveal_usage:
            data['handler_key'] = t.handler_key
            cfg = self._effective_settlement_config()
            if for_client:
                from plugins.adventurer_s1.services.s1_building_client_sanitize import (
                    sanitize_settlement_config_for_client,
                )
                data['settlement_config'] = sanitize_settlement_config_for_client(
                    cfg,
                    handler_key=t.handler_key,
                )
            else:
                data['settlement_config'] = cfg
        return data

    def to_dict(
        self,
        include_helpers=False,
        helpers=None,
        active_helpers_count=None,
        reveal_usage=None,
        for_list=False,
        for_client: bool = True,
    ):
        """序列化建筑实例。

        Args:
            include_helpers: 是否带在位冒险者列表。
            helpers: 调用方预取好的在位冒险者列表（list 接口批量场景），未传则按需现查。
            active_helpers_count: 预取的占用数（built 建筑用），未传则按需现查。
            reveal_usage: 是否返回建成后的功能字段（handler_key / settlement_config）。
                建筑用途 description 始终返回。默认仅 status='built' 时为 True；管理员可显式传 True。
            for_list: 列表接口轻量模式，裁剪模板详情 + helpers 字段。
        """
        if reveal_usage is None:
            reveal_usage = self.status in ('built', 'upgrading')
        now = _portable_utc_now()
        remaining_seconds = 0
        progress_percent = 0
        total_build_seconds = 0

        if self.template:
            total_build_seconds = self.template.build_hours * 3600

        if self.status == 'building' and total_build_seconds > 0:
            remaining_seconds = max(0, total_build_seconds - self.accumulated_progress_seconds)
            progress_percent = min(100, round(self.accumulated_progress_seconds / total_build_seconds * 100))
        elif self.status == 'upgrading':
            # 升级进度：按当前等级读取对应的分级 upgrade_hours。
            from plugins.adventurer_s1.services.adventurer_s1_building_upgrade_config import (
                building_upgrade_stage_config,
            )
            root_cfg = self.template.upgrade_config if self.template else None
            upgrade_cfg = building_upgrade_stage_config(root_cfg, int(self.upgrade_level or 1))
            upgrade_hours = int(upgrade_cfg.get('upgrade_hours', 4))
            total_upgrade_seconds = upgrade_hours * 3600
            if total_upgrade_seconds > 0:
                remaining_seconds = max(0, total_upgrade_seconds - self.accumulated_progress_seconds)
                progress_percent = min(100, round(self.accumulated_progress_seconds / total_upgrade_seconds * 100))
            total_build_seconds = total_upgrade_seconds
        elif self.status == 'built':
            progress_percent = 100

        res = {
            'id': self.id,
            'template_id': self.template_id,
            'custom_name': self.custom_name,
            'status': self.status,
            'upgrade_level': self.upgrade_level,
            'applicant_user_id': self.applicant_user_id,
            'build_started_at': to_utc_iso(self.build_started_at) if self.build_started_at else None,
            'build_complete_at': to_utc_iso(self.build_complete_at) if self.build_complete_at else None,
            'upgrade_started_at': to_utc_iso(self.upgrade_started_at) if self.upgrade_started_at else None,
            'upgrade_complete_at': to_utc_iso(self.upgrade_complete_at) if self.upgrade_complete_at else None,
            'donation_open': self.is_donation_open(),
            'accumulated_progress_seconds': self.accumulated_progress_seconds,
            'remaining_seconds': int(remaining_seconds),
            'progress_percent': progress_percent,
            'total_build_seconds': int(total_build_seconds),
            'disabled': self.disabled,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
        if self.template:
            res['template'] = self._template_to_dict(
                reveal_usage,
                for_list=for_list,
                for_client=for_client,
            )
        if include_helpers:
            if helpers is None:
                helpers = AdventurerS1.query.filter_by(
                    building_instance_id=self.id, status='building'
                ).all()
            if for_list:
                res['helpers'] = [{
                    'adventurer_id': h.id,
                    'adventurer_name': h.name,
                    'owner_id': h.owner_id,
                } for h in helpers]
            else:
                from plugins.adventurer_s1.services.s1_item_use_condition_service import (
                    sanitize_building_usage_extra_for_client,
                )
                res['helpers'] = [{
                    'adventurer_id': h.id,
                    'adventurer_name': h.name,
                    'owner_id': h.owner_id,
                    'stamina': h.get_current_stamina(),
                    'building_assigned_at': to_utc_iso(h.building_assigned_at),
                    'building_usage_extra': sanitize_building_usage_extra_for_client(
                        h.building_usage_extra,
                    ),
                } for h in helpers]
            res['active_helpers_count'] = len(helpers)
        else:
            if active_helpers_count is not None:
                res['active_helpers_count'] = active_helpers_count
            elif self.status in ('built', 'upgrading'):
                res['active_helpers_count'] = AdventurerS1.query.filter_by(
                    building_instance_id=self.id, status='building'
                ).count()
        if reveal_usage and self.status == 'built' and self.template:
            cfg = self._effective_settlement_config()
            if cfg and not cfg.get('unlimited_usage'):
                from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
                slots = AdventurerS1Service._effective_usage_slots(self, cfg)
                if slots > 0:
                    res['effective_usage_slots'] = slots
        return res


class AdventurerS1BuildingHelper(db.Model):
    """冒险者帮忙建造记录。"""

    __tablename__ = 'adventurer_s1_building_helpers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    building_instance_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_building_instances.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    helped_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    left_at = db.Column(db.DateTime, nullable=True)  # 离场时间（召回/精力耗尽/建筑完工）
    # 本次帮忙实际贡献的建筑进度秒数：_tick_building_progress 结算时精确入账，
    # 全部帮手之和 == 建筑总工期。NULL = 该列上线前的历史记录（展示时按在场时长估算）。
    contributed_seconds = db.Column(db.Integer, nullable=True)
    # build=初次建造帮工；upgrade=Lv2 升级帮工
    purpose = db.Column(db.String(20), nullable=False, default='build', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_helper_inst_purpose', 'building_instance_id', 'purpose'),
        {'extend_existing': True},
    )

    building_instance = db.relationship('AdventurerS1BuildingInstance', back_populates='helpers')
    adventurer = db.relationship('AdventurerS1')

    def to_dict(self):
        return {
            'id': self.id,
            'building_instance_id': self.building_instance_id,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': self.adventurer.name if self.adventurer else None,
            'helped_at': to_utc_iso(self.helped_at) if self.helped_at else None,
            'left_at': to_utc_iso(self.left_at) if self.left_at else None,
            'purpose': self.purpose,
        }


class AdventurerS1SkillSchoolSlot(db.Model):
    """百艺阁数据库占位槽：经典传艺与导师指点各自拥有独立的有限槽位池。"""

    __tablename__ = 'adventurer_s1_skill_school_slots'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    building_instance_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_building_instances.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    usage_mode = db.Column(db.String(20), nullable=False, index=True)
    slot_index = db.Column(db.Integer, nullable=False)
    adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    claimed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'building_instance_id',
            'usage_mode',
            'slot_index',
            name='uq_advs1_skill_school_slot_position',
        ),
        db.UniqueConstraint(
            'adventurer_id',
            name='uq_advs1_skill_school_slot_adventurer',
        ),
        db.CheckConstraint(
            "usage_mode IN ('classic', 'custom')",
            name='ck_advs1_skill_school_slot_mode',
        ),
        db.CheckConstraint(
            'slot_index > 0',
            name='ck_advs1_skill_school_slot_index',
        ),
        {'extend_existing': True},
    )


class AdventurerS1SkillSchoolQueueEntry(db.Model):
    """百艺阁导师指点候场队列；按 queued_at、id 严格先进先出。"""

    __tablename__ = 'adventurer_s1_skill_school_queue_entries'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    building_instance_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_building_instances.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False,
        unique=True,
        index=True,
    )
    queued_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index(
            'ix_advs1_skill_school_queue_fifo',
            'building_instance_id',
            'queued_at',
            'id',
        ),
        {'extend_existing': True},
    )

    def to_dict(self):
        return {
            'id': self.id,
            'building_instance_id': self.building_instance_id,
            'adventurer_id': self.adventurer_id,
            'queued_at': to_utc_iso(self.queued_at) if self.queued_at else None,
        }


class AdventurerS1BuildingUsageQueueEntry(db.Model):
    """异步设施候场队列；按 queued_at、id 严格先进先出。"""

    __tablename__ = 'adventurer_s1_building_usage_queue_entries'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    building_instance_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_building_instances.id', ondelete='CASCADE'),
        nullable=False,
    )
    adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False,
        unique=True,
        index=True,
    )
    queued_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index(
            'ix_advs1_building_usage_queue_building',
            'building_instance_id',
        ),
        db.Index(
            'ix_advs1_building_usage_queue_fifo',
            'building_instance_id',
            'queued_at',
            'id',
        ),
        {'extend_existing': True},
    )

    def to_dict(self):
        return {
            'id': self.id,
            'building_instance_id': self.building_instance_id,
            'adventurer_id': self.adventurer_id,
            'queued_at': to_utc_iso(self.queued_at) if self.queued_at else None,
        }


class AdventurerS1BuildingUsageLog(db.Model):
    """冒险者建筑使用记录：用于每日次数限制和使用历史。"""

    __tablename__ = 'adventurer_s1_building_usage_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    building_instance_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_building_instances.id', ondelete='CASCADE'), nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    result = db.Column(db.JSON, nullable=True)  # AI 治疗/锻造结果（healed/modified/narrative）
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_usage_log_adv_recent', 'adventurer_id', 'created_at', 'id'),
        db.Index(
            'idx_advs1_assembly_source_name',
            result['new_name'].as_string(), 'id',
            postgresql_where=result['type'].as_string() == 'item_assembly_workshop',
            sqlite_where=result['type'].as_string() == 'item_assembly_workshop',
            info={'online_migration': 'migrations.add_s1_assembly_source_index'},
        ),
        {'extend_existing': True},
    )

    adventurer = db.relationship('AdventurerS1')
    building_instance = db.relationship('AdventurerS1BuildingInstance')

    def to_dict(self):
        from plugins.adventurer_s1.services.s1_player_projection_service import project_building_result
        return {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'building_instance_id': self.building_instance_id,
            'used_at': to_utc_iso(self.used_at) if self.used_at else None,
            'result': project_building_result(self.result),
        }


class AdventurerS1BuildingAiFailureAudit(db.Model):
    """异步建筑系统中止审计；不参与建筑次数、冷却或玩家动态。"""

    __tablename__ = 'adventurer_s1_building_ai_failure_audits'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    building_instance_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_building_instances.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    handler_key = db.Column(db.String(50), nullable=False, index=True)
    usage_session_id = db.Column(db.String(64), nullable=True)
    failure_code = db.Column(db.String(100), nullable=False, index=True)
    diagnostics = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'handler_key',
            'usage_session_id',
            name='uq_advs1_building_ai_failure_session',
        ),
        {'extend_existing': True},
    )

    def to_dict(self):
        return {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'building_instance_id': self.building_instance_id,
            'handler_key': self.handler_key,
            'usage_session_id': self.usage_session_id,
            'failure_code': self.failure_code,
            'diagnostics': dict(self.diagnostics) if isinstance(self.diagnostics, dict) else {},
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1TaskInstance(db.Model):
    """冒险者任务实例：跟踪任务接取、完成状态。"""

    __tablename__ = 'adventurer_s1_task_instances'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    task_template_id = db.Column(db.String(50), nullable=False)  # 对应 JSON 中的 task id
    building_instance_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_building_instances.id', ondelete='SET NULL'), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='accepted', index=True)  # accepted / completed / abandoned / expired
    started_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.Index('ix_advs1_task_adv_status', 'adventurer_id', 'status'),
        db.Index('ix_advs1_task_status_template', 'status', 'task_template_id'),
    )

    adventurer = db.relationship('AdventurerS1', backref=db.backref('task_instances', lazy='select', cascade='all, delete-orphan'))
    building_instance = db.relationship('AdventurerS1BuildingInstance', backref=db.backref('task_instances', lazy='select'))

    def to_dict(self, template: dict = None):
        res = {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'task_template_id': self.task_template_id,
            'building_instance_id': self.building_instance_id,
            'status': self.status,
            'started_at': to_utc_iso(self.started_at) if self.started_at else None,
            'completed_at': to_utc_iso(self.completed_at) if self.completed_at else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
        if template:
            res['template'] = template
        return res


class AdventurerS1TaskOverride(db.Model):
    """任务栏模板运行时覆盖：启用/停用、运营字段热更新、以及从 JSON 模板派生的新任务版本。

    - JSON 模板（``s1_task_templates.json``）对应一行 override 时，仅 ``overrides_json`` 为增量覆盖。
    - ``source_template_id`` 非空表示从某 JSON 模板派生的**新任务 ID**（如 ``gathering_contract_1h__r2``），
      名额与完成记录按新 ID 独立统计，与旧版互不影响。
    """

    __tablename__ = 'adventurer_s1_task_overrides'

    task_template_id = db.Column(db.String(50), primary_key=True)
    source_template_id = db.Column(db.String(50), nullable=True, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    quota_reset_at = db.Column(db.DateTime, nullable=True)  # legacy，新逻辑请用派生新 ID
    overrides_json = db.Column(db.JSON, nullable=True)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    def to_dict(self):
        return {
            'task_template_id': self.task_template_id,
            'source_template_id': self.source_template_id,
            'enabled': bool(self.enabled),
            'quota_reset_at': to_utc_iso(self.quota_reset_at) if self.quota_reset_at else None,
            'overrides_json': self.overrides_json or {},
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1HiddenBossState(db.Model):
    """S1 hidden boss global state, one row per dungeon."""

    __tablename__ = 'adventurer_s1_hidden_boss_states'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    dungeon = db.Column(db.String(100), nullable=False, unique=True, index=True)
    boss_name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='available', index=True)  # available/active/defeated
    active_party_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_parties.id', ondelete='SET NULL'), nullable=True, index=True)
    active_expedition_log_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'), nullable=True, index=True)
    defeated_party_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_parties.id', ondelete='SET NULL'), nullable=True, index=True)
    defeated_expedition_log_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'), nullable=True, index=True)
    defeated_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_dict(self):
        return {
            'dungeon': self.dungeon,
            'boss_name': self.boss_name,
            'status': self.status,
            'defeated_at': to_utc_iso(self.defeated_at) if self.defeated_at else None,
        }


class AdventurerS1Currency(db.Model):
    """冒险者货币余额表。每种货币一行，支持未来扩展多种货币。"""

    __tablename__ = 'adventurer_s1_currencies'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    currency_code = db.Column(db.String(20), nullable=False)  # tavern_coin / gold / icu_fund / ...
    amount = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('adventurer_id', 'currency_code', name='uq_advs1_currency'),
    )

    adventurer = db.relationship('AdventurerS1', backref=db.backref('currencies', lazy='select', cascade='all, delete-orphan'))

    def to_dict(self):
        # 仅用于偶发调试/审计；正常货币读取走 S1CurrencyService.get_all_balances 的 {code: amount} 形态。
        return {
            'currency_code': self.currency_code,
            'amount': _round_money(self.amount),
        }


class AdventurerS1MaterialItem(db.Model):
    """冒险者材料库存：独立于装备槽和普通仓库，专门存放采集到的建造材料（木材/石料等）。

    设计取向：
    - 材料**不允许装备**、**不允许带入副本**（探险 AI prompt 不读取本表，结算不消耗）
    - 一种材料一行，name 是稳定的内部 code（如 'wood' / 'stone'），quantity 是当前持有量
    - 不挂图鉴（AdventurerS1Item.type='material'），name 自身是唯一标识，描述/icon 在常量映射里
    - 主要消耗场景：宿舍楼开工时按 owner 名下汇总扣
    """

    __tablename__ = 'adventurer_s1_material_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(30), nullable=False)  # 内部 code，如 'wood' / 'stone'
    quantity = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('adventurer_id', 'name', name='uq_advs1_material'),
    )

    adventurer = db.relationship('AdventurerS1', backref=db.backref('materials', lazy='select', cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'name': self.name,
            'quantity': int(self.quantity or 0),
        }


class AdventurerS1MaterialLog(db.Model):
    """冒险者材料变动日志。审计与历史追溯用。"""

    __tablename__ = 'adventurer_s1_material_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(30), nullable=False, index=True)
    change_amount = db.Column(db.Integer, nullable=False)  # 正数=入库，负数=扣减
    balance_after = db.Column(db.Integer, nullable=False)
    source_type = db.Column(db.String(30), nullable=False, index=True)  # gathering_reward / dormitory_build_start / admin_adjust / migration_from_currency
    related_id = db.Column(db.String(50), nullable=True)
    related_type = db.Column(db.String(30), nullable=True)
    description = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('idx_advs1_material_log_adv_name', 'adventurer_id', 'name'),
        db.Index('idx_advs1_material_log_adv_recent', 'adventurer_id', 'created_at', 'id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'change_amount': int(self.change_amount),
            'balance_after': int(self.balance_after),
            'source_type': self.source_type,
            'description': self.description,
            'created_at': to_utc_iso(self.created_at),
        }


class AdventurerS1CurrencyLog(db.Model):
    """冒险者货币变动日志。所有增减都留审计记录。"""

    __tablename__ = 'adventurer_s1_currency_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    currency_code = db.Column(db.String(20), nullable=False, index=True)
    change_amount = db.Column(db.Float, nullable=False)  # 正数=收入，负数=支出
    balance_after = db.Column(db.Float, nullable=False)
    source_type = db.Column(db.String(30), nullable=False, index=True)  # expedition_reward / shop_buy / admin_adjust / ...
    related_id = db.Column(db.String(50), nullable=True)
    related_type = db.Column(db.String(30), nullable=True)
    description = db.Column(db.String(200), nullable=True)
    # 仅供服务端业务恢复/审计使用；普通玩家流水接口不得投影该字段。
    business_payload = db.Column(db.JSON, nullable=True)
    business_expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('idx_advs1_currency_log_adv_code', 'adventurer_id', 'currency_code'),
        # Backward scans serve recent activity without scanning other players.
        db.Index('idx_advs1_currency_log_adv_recent', 'adventurer_id', 'created_at', 'id'),
    )

    def to_dict(self):
        # 接口路径已含 adventurer_id 与 currency_code，故不再回显；related_* 当前前端未用故省略。
        return {
            'id': self.id,
            'change_amount': _round_money(self.change_amount),
            'balance_after': _round_money(self.balance_after),
            'source_type': self.source_type,
            'description': self.description,
            'created_at': to_utc_iso(self.created_at),
        }


class AdventurerS1LowConsumptionOvercreditRepair(db.Model):
    """旧版低消费临时金重复增发的逐笔修复凭证。"""

    __tablename__ = 'adventurer_s1_low_consumption_overcredit_repairs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.String(32), nullable=False, index=True)
    legacy_accrual_log_id = db.Column(db.Integer, nullable=False)
    paired_gold_log_id = db.Column(db.Integer, nullable=False)
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    original_source_type = db.Column(db.String(30), nullable=False)
    pre_income_gold = db.Column(db.Float, nullable=False)
    was_in_debt = db.Column(db.Boolean, nullable=False)
    recovery_currency_code = db.Column(db.String(20), nullable=False)
    reclaim_amount = db.Column(db.Float, nullable=False)
    currency_balance_before = db.Column(db.Float, nullable=False)
    currency_balance_after = db.Column(db.Float, nullable=False)
    currency_log_frontier_before = db.Column(db.Integer, nullable=False, default=0)
    reclaim_currency_log_id = db.Column(db.Integer, nullable=False)
    admin_user_id = db.Column(db.Integer, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='executed')
    restored_by = db.Column(db.Integer, nullable=True)
    restored_at = db.Column(db.DateTime, nullable=True, index=True)
    restore_currency_log_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'legacy_accrual_log_id',
            name='uq_advs1_low_consume_repair_legacy_log',
        ),
        db.Index(
            'idx_advs1_low_consume_repair_batch_status',
            'batch_id',
            'status',
        ),
        {'extend_existing': True},
    )


class AdventurerS1SpecialDungeonTicketPenalty(db.Model):
    """特殊副本未扣门票追缴台账；每个探险成员最多追缴一次。"""

    __tablename__ = 'adventurer_s1_special_dungeon_ticket_penalties'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 存量 PostgreSQL 的 expedition_logs.id 没有可供新表 FK 引用的唯一约束；唯一台账键已保证幂等。
    expedition_log_id = db.Column(db.Integer, nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    dungeon_name = db.Column(db.String(50), nullable=False)
    ticket_item = db.Column(db.String(100), nullable=False)
    penalty_gold = db.Column(db.Float, nullable=False)
    admin_user_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.UniqueConstraint('expedition_log_id', 'adventurer_id', name='uq_advs1_special_ticket_penalty_log_adv'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'expedition_log_id': self.expedition_log_id,
            'adventurer_id': self.adventurer_id,
            'dungeon_name': self.dungeon_name,
            'ticket_item': self.ticket_item,
            'penalty_gold': _round_money(self.penalty_gold),
            'created_at': to_utc_iso(self.created_at),
        }


class AdventurerS1ExpeditionConsumptionRepair(db.Model):
    """历史探险物品漏扣追回台账；唯一键保证重复执行不会重复扣。"""

    __tablename__ = 'adventurer_s1_expedition_consumption_repairs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expedition_log_id = db.Column(db.Integer, nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    item_name = db.Column(db.String(100), nullable=False)
    equipment_id = db.Column(db.Integer, nullable=True)
    reclaimed_quantity = db.Column(db.Integer, nullable=False)
    item_snapshot = db.Column(db.JSON, nullable=False)
    admin_user_id = db.Column(db.Integer, nullable=False)
    restored_at = db.Column(db.DateTime, nullable=True, index=True)
    restored_by_user_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'expedition_log_id', 'adventurer_id', 'item_name',
            name='uq_advs1_expedition_consumption_repair',
        ),
    )


class AdventurerS1ItemLog(db.Model):
    """冒险者物品变动日志。装备/仓库物品的获得与消耗记录，供最近动态时间线展示。"""

    __tablename__ = 'adventurer_s1_item_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(50), nullable=False, default='')
    item_rarity = db.Column(db.String(20), nullable=False, default='common')
    change_type = db.Column(db.String(20), nullable=False)  # gain / consume / overflow / equip / unequip
    quantity = db.Column(db.Integer, nullable=False, default=1)
    source_type = db.Column(db.String(30), nullable=False, index=True)  # expedition / shop / building / admin / transfer / data_appeal_ai
    related_id = db.Column(db.String(50), nullable=True)
    related_type = db.Column(db.String(30), nullable=True)
    description = db.Column(db.String(200), nullable=True)
    # 隐身：True=写库可查 + 申诉AI证据(_build_recent_item_activity)可见，但不进玩家「我的动态」(管理员后台删物用)
    hidden_from_player = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('idx_advs1_item_log_adv_type', 'adventurer_id', 'change_type'),
        db.Index('idx_advs1_item_log_adv_name_time', 'adventurer_id', 'item_name', 'created_at',
                 info={'online_migration': 'migrations.add_observed_query_indexes'}),
        db.Index('idx_advs1_item_log_adv_recent', 'adventurer_id', 'created_at', 'id'),
    )

    def to_dict(self):
        from plugins.adventurer_s1.services.s1_visible_name_policy import (
            resolve_legacy_visible_name,
        )
        public_item_name = resolve_legacy_visible_name(self.item_name)
        public_description = self.description
        if public_description and public_item_name != self.item_name:
            public_description = public_description.replace(self.item_name, public_item_name)
        return {
            'id': self.id,
            'item_name': public_item_name,
            'item_type': self.item_type,
            'item_rarity': self.item_rarity,
            'change_type': self.change_type,
            'quantity': int(self.quantity or 1),
            'source_type': self.source_type,
            'description': public_description,
            'created_at': to_utc_iso(self.created_at),
        }


class AdventurerS1GainAnomaly(db.Model):
    """S1 金币/物品异常增长监控记录；只留审计，不自动处罚或回收。"""

    __tablename__ = 'adventurer_s1_gain_anomalies'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_key = db.Column(db.String(200), nullable=False, unique=True)
    asset_kind = db.Column(db.String(20), nullable=False, index=True)
    detector_rule = db.Column(db.String(40), nullable=False, index=True)
    severity = db.Column(db.String(20), nullable=False, default='medium', index=True)
    status = db.Column(db.String(20), nullable=False, default='open', index=True)
    is_historical = db.Column(db.Boolean, nullable=False, default=False, index=True)

    # 不设 FK：冒险者被归档/清理后，管理员仍需保留当时的审计证据。
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    adventurer_name = db.Column(db.String(100), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=True, index=True)
    owner_username = db.Column(db.String(64), nullable=True)
    owner_nickname = db.Column(db.String(64), nullable=True)

    source_log_id = db.Column(db.Integer, nullable=True, index=True)
    currency_code = db.Column(db.String(20), nullable=True)
    item_name = db.Column(db.String(100), nullable=True)
    item_type = db.Column(db.String(50), nullable=True)
    item_rarity = db.Column(db.String(20), nullable=True)
    gain_amount = db.Column(db.Float, nullable=False)
    balance_after = db.Column(db.Float, nullable=True)
    source_type = db.Column(db.String(30), nullable=False)
    related_id = db.Column(db.String(50), nullable=True)
    related_type = db.Column(db.String(30), nullable=True)
    description = db.Column(db.String(200), nullable=True)
    evidence = db.Column(db.JSON, nullable=False, default=dict)

    occurred_at = db.Column(db.DateTime, nullable=False, index=True)
    detected_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    resolved_by_user_id = db.Column(db.Integer, nullable=True)
    resolution_note = db.Column(db.String(500), nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index('idx_advs1_gain_anomaly_status_detected', 'status', 'detected_at'),
        db.Index('idx_advs1_gain_anomaly_adv_occurred', 'adventurer_id', 'occurred_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'asset_kind': self.asset_kind,
            'detector_rule': self.detector_rule,
            'severity': self.severity,
            'status': self.status,
            'is_historical': bool(self.is_historical),
            'adventurer_id': self.adventurer_id,
            'adventurer_name': self.adventurer_name,
            'owner_user_id': self.owner_user_id,
            'owner_username': self.owner_username,
            'owner_nickname': self.owner_nickname,
            'source_log_id': self.source_log_id,
            'currency_code': self.currency_code,
            'item_name': self.item_name,
            'item_type': self.item_type,
            'item_rarity': self.item_rarity,
            'gain_amount': _round_money(self.gain_amount),
            'balance_after': (
                _round_money(self.balance_after) if self.balance_after is not None else None
            ),
            'source_type': self.source_type,
            'related_id': self.related_id,
            'related_type': self.related_type,
            'description': self.description,
            'evidence': self.evidence or {},
            'occurred_at': to_utc_iso(self.occurred_at),
            'detected_at': to_utc_iso(self.detected_at),
            'resolved_by_user_id': self.resolved_by_user_id,
            'resolution_note': self.resolution_note,
            'resolved_at': to_utc_iso(self.resolved_at),
            'created_at': to_utc_iso(self.created_at),
            'updated_at': to_utc_iso(self.updated_at),
        }


class AdventurerS1Activity(db.Model):
    """酒馆限时活动；玩法差异收敛在 activity_type 与冻结配置中。"""

    __tablename__ = 'adventurer_s1_activities'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    activity_type = db.Column(
        db.String(40), nullable=False, default='narrative_tournament', index=True,
    )
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False, default='')
    status = db.Column(
        db.String(24), nullable=False, default='draft', index=True,
    )  # draft/registration/running/completed/cancelled
    current_stage = db.Column(db.Integer, nullable=False, default=0)
    config_snapshot = db.Column(db.JSON, nullable=False, default=dict)
    registration_open_at = db.Column(db.DateTime, nullable=True)
    registration_close_at = db.Column(db.DateTime, nullable=True)
    first_stage_start_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index('idx_advs1_activity_status_created', 'status', 'created_at'),
        {'extend_existing': True},
    )


class AdventurerS1ActivityRegistration(db.Model):
    """一次活动的报名与最终赛事状态。"""

    __tablename__ = 'adventurer_s1_activity_registrations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    activity_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_activities.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    # S1 存量 PostgreSQL 身份约束不稳定，新活动仅保存弱引用和冻结身份快照。
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    owner_user_id = db.Column(db.Integer, nullable=True, index=True)
    adventurer_name = db.Column(db.String(100), nullable=False)
    status = db.Column(
        db.String(24), nullable=False, default='registered', index=True,
    )  # registered/active/advanced/eliminated/forfeit/champion/runner_up
    final_rank = db.Column(db.Integer, nullable=True)
    registered_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    eliminated_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'activity_id', 'adventurer_id', name='uq_advs1_activity_registration_adv',
        ),
        db.UniqueConstraint(
            'activity_id', 'owner_user_id', name='uq_advs1_activity_registration_owner',
        ),
        db.Index(
            'idx_advs1_activity_registration_status',
            'activity_id', 'status', 'registered_at',
        ),
        {'extend_existing': True},
    )


class AdventurerS1ActivityMatch(db.Model):
    """活动某阶段的固定编号组；抽签后成员不会因签到顺序变化。"""

    __tablename__ = 'adventurer_s1_activity_matches'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    activity_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_activities.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    stage_number = db.Column(db.Integer, nullable=False, index=True)
    group_number = db.Column(db.Integer, nullable=False)
    is_final = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(
        db.String(24), nullable=False, default='scheduled', index=True,
    )  # scheduled/checkin_open/quota_wait/generating/awaiting_reveal/settled/failed/cancelled
    checkin_open_at = db.Column(db.DateTime, nullable=False, index=True)
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    reveal_at = db.Column(db.DateTime, nullable=False, index=True)
    ai_requested_at = db.Column(db.DateTime, nullable=True, index=True)
    generation_started_at = db.Column(db.DateTime, nullable=True)
    generation_finished_at = db.Column(db.DateTime, nullable=True)
    generation_attempts = db.Column(db.Integer, nullable=False, default=0)
    prompt_version = db.Column(db.String(40), nullable=True)
    result_data = db.Column(db.JSON, nullable=True)
    story = db.Column(ColdCompressedText, nullable=True)
    raw_response = db.Column(ColdCompressedText, nullable=True)
    review_data = db.Column(db.JSON, nullable=True)
    review_raw_response = db.Column(ColdCompressedText, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    settled_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'activity_id', 'stage_number', 'group_number',
            name='uq_advs1_activity_match_stage_group',
        ),
        db.Index(
            'idx_advs1_activity_match_due',
            'status', 'checkin_open_at', 'starts_at', 'reveal_at',
        ),
        {'extend_existing': True},
    )


class AdventurerS1ActivityParticipant(db.Model):
    """固定分组成员及其签到、体力扣除、排名与晋级结果。"""

    __tablename__ = 'adventurer_s1_activity_participants'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    match_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_activity_matches.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    registration_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_activity_registrations.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    owner_user_id = db.Column(db.Integer, nullable=True, index=True)
    adventurer_name = db.Column(db.String(100), nullable=False)
    personality_snapshot = db.Column(db.String(200), nullable=False, default='')
    draw_slot = db.Column(db.Integer, nullable=False)
    status = db.Column(
        db.String(24), nullable=False, default='assigned', index=True,
    )  # assigned/checked_in/forfeit/advanced/eliminated/finalist
    checked_in_at = db.Column(db.DateTime, nullable=True)
    stamina_charged = db.Column(db.Integer, nullable=False, default=0)
    forfeit_reason = db.Column(db.String(80), nullable=True)
    rank = db.Column(db.Integer, nullable=True)
    advanced = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'match_id', 'adventurer_id', name='uq_advs1_activity_participant_adv',
        ),
        db.UniqueConstraint(
            'match_id', 'draw_slot', name='uq_advs1_activity_participant_slot',
        ),
        db.Index(
            'idx_advs1_activity_participant_match_status', 'match_id', 'status',
        ),
        {'extend_existing': True},
    )

    def to_dict(self):
        return {
            'id': self.id,
            'match_id': self.match_id,
            'registration_id': self.registration_id,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': self.adventurer_name,
            'draw_slot': self.draw_slot,
            'status': self.status,
            'checked_in_at': to_utc_iso(self.checked_in_at),
            'forfeit_reason': self.forfeit_reason,
            'rank': self.rank,
            'advanced': bool(self.advanced),
            'updated_at': to_utc_iso(self.updated_at),
        }


class AdventurerS1CeremonyProfile(db.Model):
    """玩家在星灯礼堂登记的纪念日资料；不记录出生年份。"""

    __tablename__ = 'adventurer_s1_ceremony_profiles'

    # 本地遗留 users.id 没有可供 PostgreSQL 外键引用的 PK/UNIQUE；
    # 礼堂入口只接受已鉴权用户，这里用弱引用兼容旧库。
    user_id = db.Column(db.Integer, primary_key=True)
    birthday_month = db.Column(db.Integer, nullable=False)
    birthday_day = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.CheckConstraint(
            'birthday_month BETWEEN 1 AND 12',
            name='ck_advs1_ceremony_birthday_month',
        ),
        db.CheckConstraint(
            'birthday_day BETWEEN 1 AND 31',
            name='ck_advs1_ceremony_birthday_day',
        ),
        {'extend_existing': True},
    )


class AdventurerS1CeremonyAttendance(db.Model):
    """星灯礼堂年度入席礼帖；唯一键保证同类仪式每年只落印一次。"""

    __tablename__ = 'adventurer_s1_ceremony_attendances'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 与礼堂档案采用同一兼容边界；年度唯一约束仍负责防止重复落印。
    user_id = db.Column(db.Integer, nullable=False, index=True)
    ceremony_type = db.Column(db.String(40), nullable=False, index=True)
    occurrence_year = db.Column(db.Integer, nullable=False, index=True)
    birthday_month = db.Column(db.Integer, nullable=False)
    birthday_day = db.Column(db.Integer, nullable=False)
    wish_key = db.Column(db.String(32), nullable=False)
    celebrant_name = db.Column(db.String(64), nullable=False)
    # 寿星主位、同行名单、菜肴与酒馆祝词按入席当夜冻结，后续变化不改写纪念。
    host_snapshot = db.Column(db.JSON, nullable=True)
    guest_snapshot = db.Column(db.JSON, nullable=True)
    feast_snapshot = db.Column(db.JSON, nullable=True)
    tavern_blessing = db.Column(db.Text, nullable=True)
    # 串门入口只使用不透明 token；是否公开由寿星在生日当夜主动选择。
    visit_token = db.Column(db.String(32), nullable=True, unique=True)
    visitors_open = db.Column(db.Boolean, nullable=False, default=False)
    visitors_opened_at = db.Column(db.DateTime, nullable=True)
    # 年度赠礼的领取凭据跟随礼帖长期保存，不依赖冒险者物品日志的生命周期。
    gift_claimed_at = db.Column(db.DateTime, nullable=True)
    gift_destination = db.Column(db.String(20), nullable=True)
    gift_adventurer_id = db.Column(db.Integer, nullable=True)
    attended_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint(
            'user_id', 'ceremony_type', 'occurrence_year',
            name='uq_advs1_ceremony_attendance_year',
        ),
        db.Index(
            'idx_advs1_ceremony_user_attended', 'user_id', 'attended_at',
        ),
        db.Index(
            'idx_advs1_ceremony_open_tables',
            'ceremony_type', 'visitors_open', 'attended_at',
        ),
        {'extend_existing': True},
    )


class AdventurerS1CeremonyVisit(db.Model):
    """生日桌串门留影；只记录玩家选择的文字动作与可选祝福。"""

    __tablename__ = 'adventurer_s1_ceremony_visits'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    attendance_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_ceremony_attendances.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    # 延续礼堂对本地遗留 users 表的弱引用兼容边界；昵称同时冻结为快照。
    visitor_user_id = db.Column(db.Integer, nullable=False, index=True)
    visitor_name = db.Column(db.String(64), nullable=False)
    action_key = db.Column(db.String(32), nullable=False)
    message = db.Column(db.String(160), nullable=True)
    # 旧表兼容为可空；新写入由服务强制提供，并由复合唯一约束保证幂等。
    request_token = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint(
            'attendance_id', 'visitor_user_id', 'request_token',
            name='uq_advs1_ceremony_visit_request',
        ),
        db.Index(
            'idx_advs1_ceremony_visit_table_time',
            'attendance_id', 'created_at', 'id',
        ),
        db.Index(
            'idx_advs1_ceremony_visit_user_table_time',
            'visitor_user_id', 'attendance_id', 'created_at',
        ),
        {'extend_existing': True},
    )


class AdventurerS1LuckyWheelSpin(db.Model):
    """幸运大转盘成功抽取记录，覆盖金币与抽奖券两种支付方式。"""

    __tablename__ = 'adventurer_s1_lucky_wheel_spins'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False,
    )
    building_instance_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_building_instances.id', ondelete='CASCADE'),
        nullable=False,
    )
    source_currency_log_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_currency_logs.id', ondelete='SET NULL'),
        nullable=True,
        unique=True,
    )
    item_name = db.Column(db.String(100), nullable=False)
    item_rarity = db.Column(db.String(20), nullable=False)
    payment = db.Column(db.String(10), nullable=False)
    gold_cost = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index(
            'idx_advs1_wheel_spin_adv_created',
            'adventurer_id',
            'created_at',
        ),
        db.Index(
            'idx_advs1_wheel_spin_building_created',
            'building_instance_id',
            'created_at',
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'item_name': self.item_name,
            'item_rarity': self.item_rarity,
            'payment': self.payment,
            'gold_cost': int(self.gold_cost or 0),
            'created_at': to_utc_iso(self.created_at),
        }


class AdventurerS1BuildingDonation(db.Model):
    """建筑实例的材料捐献明细。

    用于「全服共建」模式：所有玩家从自己冒险者名下捐材料给某个 locked 实例，
    捐到 required_materials 阈值时自动转 building。管理员停建时按本表原路退还。
    """

    __tablename__ = 'adventurer_s1_building_donations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    building_instance_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1_building_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    adventurer_id = db.Column(
        db.Integer,
        db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    material_name = db.Column(db.String(30), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    # build=建造募集；upgrade=Lv2；upgrade_lvN=建筑更高等级募集
    purpose = db.Column(db.String(20), nullable=False, default='build', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('idx_advs1_donation_inst_mat', 'building_instance_id', 'material_name'),
        db.Index('idx_advs1_donation_inst_purpose_mat', 'building_instance_id', 'purpose', 'material_name'),
    )

    adventurer = db.relationship('AdventurerS1')

    def to_dict(self):
        return {
            'id': self.id,
            'building_instance_id': self.building_instance_id,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': self.adventurer.name if self.adventurer else None,
            'user_id': self.user_id,
            'material_name': self.material_name,
            'amount': self.amount,
            'purpose': self.purpose,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1Settings(db.Model):
    """S1 专属运行时配置（key-value）。

    用于管理员可热修改、需跨重启保留的运营参数（如宿舍楼并发上限）。
    与全局 SystemConfig 区分：S1 的配置一律走这里，避免污染全局命名空间。
    """

    __tablename__ = 'adventurer_s1_settings'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    key = db.Column(db.String(50), nullable=False, unique=True, index=True)
    value = db.Column(db.Text, nullable=True)  # 统一存字符串，service 层负责类型解析
    description = db.Column(db.String(200), nullable=True)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    def to_dict(self):
        return {
            'key': self.key,
            'value': self.value,
            'description': self.description,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1TokenUsage(db.Model):
    """S1 AI 调用 token 用量统计。每次 AI 调用记录一行。

    统计维度：
    - prompt_tokens_total：输入 token 总数
    - prompt_cache_hit_tokens：命中缓存的输入 token（DeepSeek prompt caching）
    - prompt_cache_miss_tokens：未命中缓存的输入 token
    - completion_tokens：输出 token（推理 + 最终内容）
    - completion_reasoning_tokens：provider 明确返回的推理 token；NULL 表示未拆分
    - visible_reasoning_chars：响应中可见 reasoning_content 字符数；NULL 表示未提供
    - reasoning_first_sentence：主探险 provider 推理首句；NULL 表示未记录
    - total_tokens：prompt + completion 总和
    """

    __tablename__ = 'adventurer_s1_token_usages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    call_type = db.Column(db.String(50), nullable=False, index=True)  # 如 'expedition_ai'
    model = db.Column(db.String(100), nullable=True)  # 实际使用的模型名
    profile_id = db.Column(db.String(100), nullable=True)  # 实际路由配置 ID（不含密钥/URL）
    provider = db.Column(db.String(80), nullable=True)  # 实际 provider 标识
    # 关联上下文（可选，便于按队伍/日志/申诉聚合）
    party_id = db.Column(db.Integer, nullable=True, index=True)
    expedition_log_id = db.Column(db.Integer, nullable=True, index=True)
    appeal_id = db.Column(db.Integer, nullable=True, index=True)
    adventurer_id = db.Column(db.Integer, nullable=True, index=True)
    # 跨业务表的稳定关联键，例如环境 NPC 赠礼的 request_id。
    correlation_id = db.Column(db.String(64), nullable=True, index=True)
    # token 统计
    prompt_tokens_total = db.Column(db.Integer, nullable=False, default=0)
    prompt_cache_hit_tokens = db.Column(db.Integer, nullable=False, default=0)
    prompt_cache_miss_tokens = db.Column(db.Integer, nullable=False, default=0)
    completion_tokens = db.Column(db.Integer, nullable=False, default=0)
    completion_reasoning_tokens = db.Column(db.Integer, nullable=True)
    visible_reasoning_chars = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=False, default=0)
    # 请求元信息
    attempt_number = db.Column(db.Integer, nullable=True)  # 第几次尝试（重试场景）
    is_success = db.Column(db.Boolean, nullable=False, default=True)  # 本次 AI 调用是否成功
    # 结构化失败诊断（仅管理员查账；正文只保留失败响应的有界摘录）
    failure_stage = db.Column(db.String(80), nullable=True)
    validation_error_code = db.Column(db.String(160), nullable=True)
    finish_reason = db.Column(db.String(40), nullable=True)
    json_object_mode = db.Column(db.Boolean, nullable=True)
    provider_request_id = db.Column(db.String(160), nullable=True)
    # 统一网关非敏感路由回执；只保存目标 hostname，不保存 API Key 或完整 URL。
    api_key_slot = db.Column(db.String(24), nullable=True)
    api_endpoint_slot = db.Column(db.String(24), nullable=True)
    endpoint_host = db.Column(db.String(253), nullable=True)
    transport_attempt = db.Column(db.Integer, nullable=True)
    failover_reason = db.Column(db.String(160), nullable=True)
    failover_scope = db.Column(db.String(40), nullable=True)
    failover_attempt_count = db.Column(db.Integer, nullable=True)
    repair_applied = db.Column(db.Boolean, nullable=False, default=False)
    # 仅主探险保存 provider reasoning_content 的有界首句，管理员可见。
    reasoning_first_sentence = db.Column(db.Text, nullable=True)
    failed_response_excerpt = db.Column(db.Text, nullable=True)
    has_failure_detail = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # Admin-only, explicitly loaded detail; lists and aggregates keep their small projection.
    failure_detail = deferred(db.Column(db.Text, nullable=True))
    # 申诉 AI：本次调用所属 process_ai 入队原因（initial/requeue_stuck/duplicate_concurrent 等）
    invoke_reason = db.Column(db.String(40), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('idx_advs1_token_usage_party', 'party_id', 'created_at'),
        db.Index('idx_advs1_token_usage_adv', 'adventurer_id', 'created_at'),
        db.Index(
            'idx_advs1_token_usage_detail_expiry',
            'has_failure_detail', 'created_at', 'id',
            info={'online_migration': 'migrations.add_observed_query_indexes'},
        ),
    )

    def to_dict(self):
        reasoning_tokens = self.completion_reasoning_tokens
        content_tokens = (
            max(0, int(self.completion_tokens or 0) - int(reasoning_tokens))
            if reasoning_tokens is not None
            else None
        )
        return {
            'id': self.id,
            'call_type': self.call_type,
            'model': self.model,
            'profile_id': self.profile_id,
            'provider': self.provider,
            'party_id': self.party_id,
            'expedition_log_id': self.expedition_log_id,
            'appeal_id': self.appeal_id,
            'adventurer_id': self.adventurer_id,
            'correlation_id': self.correlation_id,
            'prompt_tokens_total': self.prompt_tokens_total,
            'prompt_cache_hit_tokens': self.prompt_cache_hit_tokens,
            'prompt_cache_miss_tokens': self.prompt_cache_miss_tokens,
            'completion_tokens': self.completion_tokens,
            'completion_reasoning_tokens': reasoning_tokens,
            'visible_reasoning_chars': self.visible_reasoning_chars,
            'completion_content_tokens': content_tokens,
            'completion_unattributed_tokens': (
                0 if reasoning_tokens is not None else self.completion_tokens
            ),
            'total_tokens': self.total_tokens,
            'attempt_number': self.attempt_number,
            'is_success': self.is_success,
            'failure_stage': self.failure_stage,
            'validation_error_code': self.validation_error_code,
            'finish_reason': self.finish_reason,
            'json_object_mode': self.json_object_mode,
            'provider_request_id': self.provider_request_id,
            'api_key_slot': self.api_key_slot,
            'api_endpoint_slot': self.api_endpoint_slot,
            'endpoint_host': self.endpoint_host,
            'transport_attempt': self.transport_attempt,
            'failover_reason': self.failover_reason,
            'failover_scope': self.failover_scope,
            'failover_attempt_count': self.failover_attempt_count,
            'repair_applied': bool(self.repair_applied),
            'reasoning_first_sentence': self.reasoning_first_sentence,
            'failed_response_excerpt': self.failed_response_excerpt,
            'has_failure_detail': bool(self.has_failure_detail),
            'invoke_reason': self.invoke_reason,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1AiFeedback(db.Model):
    """玩家对一项 S1 AI 成品的轻量双维评分及管理员来源快照。"""

    __tablename__ = 'adventurer_s1_ai_feedback'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 历史 PostgreSQL 库的 users.id 没有可供新表引用的 PK/UNIQUE；提交入口
    # 已按鉴权用户校验目标归属，管理员读路径也会显式联表，因此这里沿用
    # RealmMembership / CeremonyProfile 的弱引用兼容边界。
    user_id = db.Column(db.Integer, nullable=False, index=True)
    target_type = db.Column(db.String(40), nullable=False, index=True)
    target_id = db.Column(db.Integer, nullable=False, index=True)
    # 同一批历史库的 token usage 表也缺少可被引用的 PK/UNIQUE。反馈行已经
    # 冻结所有展示与统计需要的回执字段，token_usage_id 只保留追溯定位用途。
    token_usage_id = db.Column(db.Integer, nullable=True, index=True)
    quality_score = db.Column(db.SmallInteger, nullable=False)
    preference_score = db.Column(db.SmallInteger, nullable=False)

    # 以下均为提交时由服务端 AI 回执复制的管理员专属快照。
    call_type = db.Column(db.String(50), nullable=False, index=True)
    model = db.Column(db.String(100), nullable=True, index=True)
    profile_id = db.Column(db.String(100), nullable=True)
    provider = db.Column(db.String(80), nullable=True, index=True)
    endpoint_host = db.Column(db.String(253), nullable=True, index=True)
    api_key_slot = db.Column(db.String(24), nullable=True)
    api_endpoint_slot = db.Column(db.String(24), nullable=True)
    provider_request_id = db.Column(db.String(160), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_portable_utc_now,
        onupdate=_portable_utc_now,
        index=True,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'user_id',
            'target_type',
            'target_id',
            name='uq_advs1_ai_feedback_user_target',
        ),
        db.CheckConstraint(
            'quality_score BETWEEN 1 AND 5',
            name='ck_advs1_ai_feedback_quality',
        ),
        db.CheckConstraint(
            'preference_score BETWEEN 1 AND 5',
            name='ck_advs1_ai_feedback_preference',
        ),
        db.Index(
            'ix_advs1_ai_feedback_target',
            'target_type',
            'target_id',
        ),
        db.Index(
            'ix_advs1_ai_feedback_route_time',
            'model',
            'provider',
            'created_at',
        ),
        {'extend_existing': True},
    )

    def to_player_dict(self) -> dict:
        """严格玩家白名单；不得加入任何 AI 路由或请求诊断字段。"""
        return {
            'target_type': self.target_type,
            'target_id': self.target_id,
            'quality_score': self.quality_score,
            'preference_score': self.preference_score,
        }

    def to_admin_dict(self) -> dict:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'token_usage_id': self.token_usage_id,
            'quality_score': self.quality_score,
            'preference_score': self.preference_score,
            'call_type': self.call_type,
            'model': self.model,
            'profile_id': self.profile_id,
            'provider': self.provider,
            'endpoint_host': self.endpoint_host,
            'api_key_slot': self.api_key_slot,
            'api_endpoint_slot': self.api_endpoint_slot,
            'provider_request_id': self.provider_request_id,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1NarrativeLease(db.Model):
    """公共剧情素材注入租约：同一结构化素材在未结算前只给一个探险使用。"""

    __tablename__ = 'adventurer_s1_narrative_leases'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    target_dungeon = db.Column(db.String(100), nullable=False, index=True)
    plot_signature = db.Column(db.String(180), nullable=False, index=True)
    lease_type = db.Column(db.String(30), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='active', index=True)
    party_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_parties.id', ondelete='SET NULL'), nullable=True, index=True)
    expedition_log_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'), nullable=True, index=True)
    world_story_thread_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_world_story_threads.id', ondelete='SET NULL'), nullable=True, index=True)
    world_actor_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_world_actors.id', ondelete='SET NULL'), nullable=True, index=True)
    dm_handoff_state_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_dm_handoff_states.id', ondelete='SET NULL'), nullable=True, index=True)
    source_log_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'), nullable=True, index=True)
    required_adventurer_ids = db.Column(db.JSON, nullable=True)
    required_item_names = db.Column(db.JSON, nullable=True)
    title = db.Column(db.String(160), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    released_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('idx_advs1_narrative_lease_active_sig', 'target_dungeon', 'plot_signature', 'status'),
        db.Index('idx_advs1_narrative_lease_log_status', 'expedition_log_id', 'status'),
        db.Index(
            'uq_advs1_narrative_lease_active_actor',
            'world_actor_id',
            unique=True,
            postgresql_where=db.text("status = 'active' AND world_actor_id IS NOT NULL"),
            sqlite_where=db.text("status = 'active' AND world_actor_id IS NOT NULL"),
        ),
        db.Index(
            'uq_advs1_narrative_lease_active_thread',
            'world_story_thread_id',
            unique=True,
            postgresql_where=db.text("status = 'active' AND world_story_thread_id IS NOT NULL AND lease_type = 'world_story_thread'"),
            sqlite_where=db.text("status = 'active' AND world_story_thread_id IS NOT NULL AND lease_type = 'world_story_thread'"),
        ),
        {'extend_existing': True},
    )


class AdventurerS1AppealFactSheet(db.Model):
    """按探险副本缓存的数据申诉事实卡，避免同副本多次申诉重复读取全文。"""

    __tablename__ = 'adventurer_s1_appeal_fact_sheets'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expedition_log_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    model = db.Column(db.String(100), nullable=True)
    fact_sheet = db.Column(ColdCompressedJSON, nullable=False)
    raw_response = db.Column(ColdCompressedText, nullable=True)
    cold_compressed_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'expedition_log_id': self.expedition_log_id,
            'model': self.model,
            'fact_sheet': self.fact_sheet,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1DataAppeal(db.Model):
    """玩家数据异常申诉：主 AI 出 patch，独立 AI 复核高权限操作并自动落库。"""

    __tablename__ = 'adventurer_s1_data_appeals'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'), nullable=False, index=True)
    player_note = db.Column(db.Text, nullable=False, default='')
    appeal_category = db.Column(db.String(20), nullable=False, default='other')  # data_fix / other
    # 全文申诉实际扣次的北京时间日期；NULL 兼容上线前按 created_at 当日计次的记录。
    quota_bucket_date = db.Column(db.Date, nullable=True)
    # daily=当天基础/临时次数，balance=持久余额池；NULL 兼容旧记录并按 daily 处理。
    quota_source = db.Column(db.String(20), nullable=True)
    selected_targets = db.Column(db.JSON, nullable=False)  # [{"type":"trait","id":1}, ...]
    snapshot_before = db.Column(ColdCompressedJSON, nullable=False)   # 选中条目的完整快照
    expedition_evidence = db.Column(ColdCompressedJSON, nullable=True)  # 可选：最新探险段落证据快照
    # 关联探险副本 id（从 expedition_evidence 反范式出来并索引）：用于同副本跨玩家申诉去重参考
    expedition_log_id = db.Column(db.Integer, nullable=True, index=True)

    ai_status = db.Column(db.String(20), nullable=False, default='pending')  # pending/processing/done/failed
    # 长 AI 任务的数据库级独占租约。队列 dedupe 只是性能层，最终写回必须同时持有 claim_token。
    ai_claim_token = db.Column(db.String(64), nullable=True)
    ai_claimed_at = db.Column(db.DateTime, nullable=True)
    ai_heartbeat_at = db.Column(db.DateTime, nullable=True, index=True)
    ai_owner_boot_id = db.Column(db.String(128), nullable=True)
    ai_raw_response = db.Column(ColdCompressedText, nullable=True)
    ai_summary = db.Column(db.Text, nullable=True)
    proposed_patch = db.Column(ColdCompressedJSON, nullable=True)
    review_ai_status = db.Column(db.String(20), nullable=False, default='pending')
    review_ai_model = db.Column(db.String(100), nullable=True)
    review_ai_summary = db.Column(db.Text, nullable=True)
    review_ai_raw_response = db.Column(ColdCompressedText, nullable=True)
    # 陪审团 2 号补做且已获逐操作 verdict allow 的修复 patch（后端规范化、过白名单+上限）
    review_supplement_patch = db.Column(ColdCompressedJSON, nullable=True)
    player_review_requests = db.Column(ColdCompressedJSON, nullable=True)
    player_review_status = db.Column(db.String(20), nullable=False, default='none')  # none/pending/done

    status = db.Column(db.String(30), nullable=False, default='processing')  # processing/awaiting_review/approved/rejected/reverted
    # 核心诉求是否被实质满足：approved 但本次只做了无关数据整理（未发放玩家真正想要的收益）时为 False，
    # 用于把"假通过"如实告知玩家（status 仍 approved，因为确实做了数据清理）。None=未判定（旧数据/纯整理类申诉）
    core_request_satisfied = db.Column(db.Boolean, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reject_reason = db.Column(db.Text, nullable=True)
    applied_at = db.Column(db.DateTime, nullable=True)
    reverted_at = db.Column(db.DateTime, nullable=True)  # 管理员一键还原时间
    reverted_by = db.Column(db.Integer, nullable=True)  # 还原操作管理员 user_id（不设 FK，便于 auto_migrate 直接补列）
    # 跨角色违规申诉若涉及正在探险的角色，先登记，结算离本后再自动撤销。
    cross_character_revert_status = db.Column(
        db.String(20), nullable=False, default='none', index=True,
    )  # none/pending/completed
    cross_character_revert_requested_at = db.Column(db.DateTime, nullable=True)
    cross_character_revert_requested_by = db.Column(db.Integer, nullable=True)
    cross_character_revert_last_error = db.Column(db.String(500), nullable=True)
    revert_offer_status = db.Column(db.String(20), nullable=False, default='none')  # none/pending/accepted/declined
    revert_offer_sent_at = db.Column(db.DateTime, nullable=True)
    revert_offer_responded_at = db.Column(db.DateTime, nullable=True)
    revert_offer_note = db.Column(db.Text, nullable=True)  # 管理员附言，展示在玩家个人页
    revert_target_appeal_id = db.Column(db.Integer, nullable=True, index=True)  # revert_request 额度行指向的原申诉
    player_revert_request_status = db.Column(db.String(20), nullable=False, default='none')  # none/processing/allowed/rejected/confirmed/declined
    player_revert_request_note = db.Column(db.Text, nullable=True)
    player_revert_request_ai_summary = db.Column(db.Text, nullable=True)
    player_revert_request_stub_id = db.Column(db.Integer, nullable=True)  # 消耗额度的 stub 申诉 id
    player_revert_request_submitted_at = db.Column(db.DateTime, nullable=True)
    player_revert_request_responded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)
    cold_compressed_at = db.Column(db.DateTime, nullable=True, index=True)

    __table_args__ = (
        db.Index('ix_advs1_data_appeal_status_created', 'status', 'created_at'),
        db.Index('ix_advs1_appeal_user_recent', 'user_id', 'created_at', 'id',
                 info={'online_migration': 'migrations.add_observed_query_indexes'}),
        db.Index(
            'ix_advs1_appeal_pending_revert', 'user_id', 'updated_at', 'id',
            postgresql_where=db.text(
                "(status = 'approved' AND revert_offer_status = 'pending') "
                "OR player_revert_request_status = 'allowed'"
            ),
            sqlite_where=db.text(
                "(status = 'approved' AND revert_offer_status = 'pending') "
                "OR player_revert_request_status = 'allowed'"
            ),
            info={'online_migration': 'migrations.add_observed_query_indexes'},
        ),
        db.Index(
            'ix_advs1_data_appeal_user_quota_bucket',
            'user_id',
            'quota_bucket_date',
        ),
        db.Index(
            'ix_advs1_data_appeal_user_quota_source_date',
            'user_id',
            'quota_source',
            'quota_bucket_date',
        ),
        {'extend_existing': True},
    )

    def _latest_player_review_result(self) -> dict | None:
        """返回最近一条已完成的玩家独立复核结果（不含 request 文本，减少 payload）。"""
        _DECISION_MAP = {
            'original_ai_correct': 'rejected',
            'player_may_be_right': 'second_review_disagreed',
        }
        for r in reversed(self.player_review_requests or []):
            if isinstance(r, dict) and r.get('status') == 'done' and isinstance(r.get('result'), dict):
                result = r['result']
                raw_decision = str(result.get('decision') or '').strip().lower()
                return {
                    'decision': _DECISION_MAP.get(raw_decision, raw_decision),
                    'message': result.get('reason') or result.get('message'),
                    'created_at': r.get('created_at'),
                }
        return None

    @staticmethod
    def _parse_json_text(text: str | None) -> dict | None:
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _first_review_reason(self) -> str | None:
        raw = self._parse_json_text(self.ai_raw_response)
        if raw:
            reason = (
                raw.get('player_reply_summary')
                or raw.get('player_reject_reason')
                or raw.get('reject_reason_for_player')
            )
            if isinstance(reason, str) and reason.strip():
                return reason.strip()[:800]
        return None

    def _second_review_reason(self) -> str | None:
        raw = self._parse_json_text(self.review_ai_raw_response)
        if raw:
            reason = raw.get('player_reason')
            if isinstance(reason, str) and reason.strip():
                return reason.strip()[:500]
        return None

    def _player_final_reason(self, *, include_review: bool = True) -> str | None:
        """玩家端主展示文案：终态执行结果优先，其次二审结论，再回退一审文案。"""
        # 终态未通过/取消时，真实执行结果必须先于任何执行前或业务分支文案。
        # 否则二审虽判断“应通过”，最终写入失败后仍会向玩家展示“已补发”。
        if self.status in ('rejected', 'cancelled'):
            reason = str(self.reject_reason or '').strip()
            if reason:
                return reason[:800]

        if (self.appeal_category or '') == 'tavern_relief':
            evidence = self.expedition_evidence if isinstance(self.expedition_evidence, dict) else {}
            result = evidence.get('tavern_relief_result')
            if isinstance(result, dict):
                reason = str(result.get('message') or '').strip()
                if reason:
                    return reason[:800]

        review_raw = self._parse_json_text(self.review_ai_raw_response) if include_review else None
        if review_raw:
            execution_note = str(review_raw.get('execution_note') or '').strip()
            if execution_note:
                return execution_note[:800]
            reason = str(review_raw.get('player_reason') or '').strip()
            if reason:
                return reason[:800]

        raw = self._parse_json_text(self.ai_raw_response)
        if raw:
            reason = str(
                raw.get('player_reply_summary')
                or raw.get('player_reject_reason')
                or raw.get('reject_reason_for_player')
                or ''
            ).strip()
            if reason:
                return reason[:800]

        reason = str(self.reject_reason or '').strip()
        return reason[:800] if reason else None

    def _target_labels(self) -> list[str]:
        """从快照解析玩家选中条目的显示名。"""
        type_to_section = {
            'trait': 'traits',
            'skill': 'skills',
            'equipment': 'equipment',
            'warehouse': 'warehouse',
        }
        snap = self.snapshot_before if isinstance(self.snapshot_before, dict) else {}
        labels: list[str] = []
        for t in self.selected_targets or []:
            if not isinstance(t, dict):
                continue
            section = type_to_section.get(t.get('type'))
            if not section:
                continue
            try:
                tid = int(t.get('id'))
            except (TypeError, ValueError):
                continue
            rec = next((r for r in snap.get(section, []) if r.get('id') == tid), None)
            if rec and rec.get('name'):
                labels.append(str(rec['name']))
        return labels

    def _public_expedition_evidence(self) -> dict | None:
        """玩家/列表侧只返回摘要，不含 member_update 等 AI 上下文。"""
        raw = self.expedition_evidence if isinstance(self.expedition_evidence, dict) else None
        if not raw:
            return None
        return {
            'expedition_log_id': raw.get('expedition_log_id'),
            'event_index': raw.get('event_index'),
            'auto_fallback': bool(raw.get('auto_fallback')),
            'target_dungeon': raw.get('target_dungeon'),
            'party_name': raw.get('party_name'),
            'event_text': raw.get('event_text'),
        }

    def _public_tavern_relief_result(self) -> dict | None:
        raw = self.expedition_evidence if isinstance(self.expedition_evidence, dict) else {}
        result = raw.get('tavern_relief_result')
        if not isinstance(result, dict):
            return None
        return {
            'decision': result.get('decision'),
            'target_per_person': result.get('target_per_person'),
            'max_target_per_person': result.get('max_target_per_person'),
            'payout_per_person': result.get('payout_per_person'),
            'recipient_count': result.get('recipient_count'),
            'team_target_total': result.get('team_target_total'),
            'team_grant_total': result.get('team_grant_total'),
            'applicant_bonus_target': result.get('applicant_bonus_target'),
            'max_applicant_bonus_target': result.get('max_applicant_bonus_target'),
            'applicant_bonus_grant': result.get('applicant_bonus_grant'),
            'grant_total': result.get('grant_total'),
            'already_covered': result.get('already_covered'),
            'message': result.get('message'),
        }

    def to_dict(
        self, *, include_patch=False, include_ai_fields=False, is_admin=False,
        user_map=None, adv_name_map=None,
    ):
        include_identity = is_admin or include_ai_fields or include_patch
        if not include_identity:
            user = None
        elif user_map is not None:
            user = user_map.get(self.user_id)
        else:
            from models.user import User
            user = User.query.session.get(User, self.user_id)

        if adv_name_map is not None:
            adv_name = adv_name_map.get(self.adventurer_id)
        else:
            adv = AdventurerS1.query.session.get(AdventurerS1, self.adventurer_id) if self.adventurer_id else None
            adv_name = adv.name if adv else None

        include_internal_review = include_ai_fields or include_patch
        processing = self.status not in ('approved', 'rejected', 'cancelled', 'reverted')
        show_review_reply = include_internal_review or (
            not processing and self.review_ai_status in ('done', 'manual_ready')
        )
        data = {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': adv_name or '未知',
            'player_note': self.player_note,
            'appeal_category': self.appeal_category or 'other',
            'target_labels': self._target_labels(),
            'expedition_evidence': self._public_expedition_evidence(),
            'tavern_relief_result': self._public_tavern_relief_result(),
            'ai_status': self.ai_status,
            'status': self.status,
            'core_request_satisfied': self.core_request_satisfied,
            'player_review_status': self.player_review_status or 'none',
            'player_review_result': self._latest_player_review_result(),
            'first_review_reason': self._first_review_reason(),
            'second_review_reason': self._second_review_reason() if show_review_reply else None,
            'player_final_reason': (
                self._player_final_reason(include_review=show_review_reply)
                if include_internal_review or not processing else None
            ),
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'reviewed_at': to_utc_iso(self.reviewed_at) if self.reviewed_at else None,
            'reject_reason': self.reject_reason,
            'applied_at': to_utc_iso(self.applied_at) if self.applied_at else None,
            'reverted_at': to_utc_iso(self.reverted_at) if self.reverted_at else None,
            'revert_offer_status': self.revert_offer_status or 'none',
            'revert_offer_pending': (
                self.status == 'approved'
                and (self.revert_offer_status or 'none') == 'pending'
            ),
            'revert_offer_note': (
                (self.revert_offer_note or '').strip()[:500]
                if (self.revert_offer_status or 'none') == 'pending' and self.revert_offer_note
                else None
            ),
            'player_revert_request_status': self.player_revert_request_status or 'none',
            'player_revert_request_pending_confirm': (
                self.status in ('approved', 'rejected')
                and (self.player_revert_request_status or 'none') == 'allowed'
            ),
            'player_revert_request_ai_summary': (
                (self.player_revert_request_ai_summary or '').strip()[:500] or None
                if (self.player_revert_request_status or 'none') in ('allowed', 'rejected')
                and self.player_revert_request_ai_summary
                else None
            ),
            'can_request_player_revert': False,  # list_my_appeals 内覆写
        }
        raw_public = self._parse_json_text(self.ai_raw_response)
        if raw_public:
            checklist = raw_public.get('request_checklist')
            if isinstance(checklist, list) and checklist:
                review_public = self._parse_json_text(self.review_ai_raw_response) or {}
                final_request_results = review_public.get('request_results')
                has_request_results = (
                    isinstance(final_request_results, list) and bool(final_request_results)
                )
                projected_rows = (
                    final_request_results
                    if has_request_results
                    else checklist
                )
                has_create_item_execution_skip = bool(
                    isinstance(review_public.get('create_item_execution_skips'), list)
                    and any(
                        isinstance(row, dict)
                        for row in review_public['create_item_execution_skips']
                    )
                )
                public_checklist = []
                for row in projected_rows[:8]:
                    if not isinstance(row, dict) or not row.get('label'):
                        continue
                    category = str(row.get('category') or 'other')[:20]
                    status = str(row.get('status') or 'pending')[:20]
                    outcome = str(row.get('outcome') or '')[:24]
                    detail = str(row.get('detail') or '').strip()
                    if (
                        has_create_item_execution_skip
                        and category in ('item', 'quantity')
                        and status in ('done', 'partial')
                    ):
                        status = 'partial'
                        detail = (
                            f'{detail}；' if detail else ''
                        ) + '最终发放有物品项未落地，请以执行说明为准'
                    if not show_review_reply and (processing or has_request_results):
                        # 重试说明可能引用尚未定稿的二审正文，不向玩家发布。
                        # 已有逐项执行回执保留；一审候选清单不能冒充落账结果。
                        detail = ''
                        if processing and not has_request_results:
                            status = 'pending'
                            outcome = ''
                    public_checklist.append({
                        'label': str(row.get('label') or '')[:120],
                        'status': status,
                        'category': category,
                        'is_core': row.get('is_core') is True,
                        **({'outcome': outcome} if outcome else {}),
                        **({'detail': detail[:160]} if detail else {}),
                    })
                data['request_checklist'] = public_checklist
        if include_identity:
            data.update({
                'user_id': self.user_id,
                'username': user.username if user else '未知',
                'nickname': user.nickname if user else None,
                'selected_targets': self.selected_targets or [],
                'cross_character_revert_status': self.cross_character_revert_status or 'none',
                'cross_character_revert_requested_at': (
                    to_utc_iso(self.cross_character_revert_requested_at)
                    if self.cross_character_revert_requested_at else None
                ),
            })
        if include_ai_fields or include_patch:
            data['ai_summary'] = self.ai_summary
            data['review_ai_status'] = self.review_ai_status
            data['review_ai_summary'] = self.review_ai_summary
            data['player_review_requests'] = self.player_review_requests or []
        if include_patch:
            data['snapshot_before'] = self.snapshot_before
            data['proposed_patch'] = self.proposed_patch
            data['review_supplement_patch'] = self.review_supplement_patch
            data['ai_raw_response'] = self.ai_raw_response
            data['review_ai_raw_response'] = self.review_ai_raw_response
            if isinstance(self.expedition_evidence, dict):
                data['expedition_evidence'] = self.expedition_evidence
        return data


class AdventurerS1DataAppealAuditLog(db.Model):
    """数据申诉每一步落库变更的审计记录，支持管理员一键还原整单修复。"""

    __tablename__ = 'adventurer_s1_data_appeal_audit_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    appeal_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_data_appeals.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    op_seq = db.Column(db.Integer, nullable=False, default=0)  # 单内执行顺序，还原时倒序回放
    op_type = db.Column(db.String(20), nullable=False)  # update / delete / create_item / create_trait / create_skill / currency / team_pool / tavern_subsidy_team / tavern_subsidy_self
    target_type = db.Column(db.String(20), nullable=False)  # trait/equipment/warehouse/skill/item/currency
    target_id = db.Column(db.Integer, nullable=True)  # update/delete 的记录 id
    before_json = db.Column(db.JSON, nullable=True)  # update/delete 的完整原值快照
    after_json = db.Column(db.JSON, nullable=True)  # 应用字段 / 补发载荷 / 货币 {code,amount,balance_after}
    # AI/管理员理由可能包含完整证据链；启动 auto_migrate 会将生产库已有
    # VARCHAR 安全扩展为 TEXT，避免审计写入失败导致整笔申诉事务回滚。
    note = db.Column(db.Text, nullable=True)  # reason / 备注
    reverted = db.Column(db.Boolean, nullable=False, default=False)
    revert_result = db.Column(db.String(200), nullable=True)  # 还原结果（成功/跳过原因）
    reverted_at = db.Column(db.DateTime, nullable=True)
    reverted_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = (
        db.Index('ix_advs1_appeal_audit_appeal_seq', 'appeal_id', 'op_seq'),
        db.Index('ix_advs1_appeal_audit_target', 'target_type', 'target_id',
                 info={'online_migration': 'migrations.add_observed_query_indexes'}),
        db.Index('ix_advs1_appeal_audit_adv_op', 'adventurer_id', 'op_type', 'id',
                 info={'online_migration': 'migrations.add_observed_query_indexes'}),
    )

    _AUDIT_DISPLAY_FIELDS = frozenset({
        'name', 'type', 'rarity', 'quantity', 'custom_desc', 'description',
        'description_ai', 'level', 'currency_code', 'amount', 'balance_after',
        'item_name', 'removed_qty', 'shortage_qty', 'total_amount', 'per_member',
        'mode', 'reason', 'destination', 'requested_destination', 'uses_left',
        'evolution_stage', 'was_new', 'sourced_from_catalog',
        'recovery_policy', 'recovery_stage', 'recovery_required', 'recovery_hours',
    })

    def _audit_target_name(self) -> str:
        before = self.before_json if isinstance(self.before_json, dict) else {}
        after = self.after_json if isinstance(self.after_json, dict) else {}
        return str(before.get('name') or after.get('name') or after.get('item_name') or '').strip()

    def _audit_pick_display_record(self, data: dict | None) -> dict | None:
        if not isinstance(data, dict):
            return None
        picked = {k: data[k] for k in self._AUDIT_DISPLAY_FIELDS if k in data and data[k] not in (None, '', [])}
        if data.get('name') and 'name' not in picked:
            picked['name'] = data.get('name')
        return picked or None

    def _audit_display_changes(self) -> list[dict]:
        before = self.before_json if isinstance(self.before_json, dict) else {}
        after = self.after_json if isinstance(self.after_json, dict) else {}
        op = self.op_type or ''

        if op == 'delete':
            return []

        if op == 'update' and after:
            changes = []
            for k, v in after.items():
                if k in ('id', 'adventurer_id', 'created_at', 'updated_at'):
                    continue
                if before.get(k) == v:
                    continue
                changes.append({'field': k, 'before': before.get(k), 'after': v})
            return changes

        if op in ('create_item', 'create_trait', 'create_skill'):
            return []

        if op == 'currency' and after:
            code = after.get('currency_code') or 'gold'
            amt = after.get('amount')
            bal = after.get('balance_after')
            return [{
                'field': 'amount',
                'before': None,
                'after': f'+{amt} {code}' + (f'（余额 {bal}）' if bal is not None else ''),
            }]

        if op in ('reclaim_remove', 'reclaim_item', 'reclaim_compensation') and after:
            return [
                {'field': k, 'before': before.get(k), 'after': v}
                for k, v in after.items()
                if k not in ('id', 'adventurer_id') and v not in (None, '', [])
            ]

        if after:
            return [
                {'field': k, 'before': before.get(k), 'after': v}
                for k, v in after.items()
                if k not in ('id', 'adventurer_id', 'created_at', 'updated_at') and v not in (None, '', [])
            ]
        return []

    def to_admin_dict(self) -> dict:
        """管理员面板用：含可读 diff，便于核对删改内容。"""
        op = self.op_type or ''
        before = self.before_json if isinstance(self.before_json, dict) else {}
        after = self.after_json if isinstance(self.after_json, dict) else {}
        return {
            'id': self.id,
            'op_seq': self.op_seq,
            'op_type': op,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'target_name': self._audit_target_name() or None,
            'note': self.note,
            'reverted': bool(self.reverted),
            'revert_result': self.revert_result,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'changes': self._audit_display_changes(),
            'deleted_record': self._audit_pick_display_record(before) if op == 'delete' else None,
            'created_record': self._audit_pick_display_record(after) if op in ('create_item', 'create_trait', 'create_skill') else None,
        }


class AdventurerS1DataAppealQuota(db.Model):
    """单个玩家的申诉额度调整与全文申诉余额池检查点。

    既有 reset_at / bonus / bonus_at 保留给其余申诉池；other_* 字段只作用于
    全文申诉池。两组临时调整都在北京 0 点自然失效；other_balance 保存当日
    开始时的余额池检查点，实际扣次仍由申诉行动态统计，便于技术失败原路返还。
    """

    __tablename__ = 'adventurer_s1_data_appeal_quota'

    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
        primary_key=True,
    )
    reset_at = db.Column(db.DateTime, nullable=True)   # 重置锚点：此刻前的申诉不计入已用
    bonus = db.Column(db.Integer, nullable=False, default=0)  # 本窗口额外额度
    bonus_at = db.Column(db.DateTime, nullable=True)   # bonus 发放时刻（跨北京 0 点自动失效）
    other_reset_at = db.Column(db.DateTime, nullable=True)
    other_bonus = db.Column(db.Integer, nullable=False, default=0)
    other_bonus_at = db.Column(db.DateTime, nullable=True)
    other_balance = db.Column(db.Integer, nullable=False, default=0)
    other_balance_date = db.Column(db.Date, nullable=True)
    other_daily_limit_snapshot = db.Column(db.Integer, nullable=True)
    updated_by = db.Column(db.Integer, nullable=True)  # 操作管理员 user_id（不设 FK，便于 auto_migrate 补列）
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )


class AdventurerS1BugReclaimBatch(db.Model):
    """管理员回收数据申诉历史漏洞收益的批次台账。"""

    __tablename__ = 'adventurer_s1_bug_reclaim_batches'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_user_id = db.Column(db.Integer, nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='preview')  # preview/executed/dismissed/failed
    criteria_json = db.Column(db.JSON, nullable=True)
    summary_json = db.Column(db.JSON, nullable=True)
    note = db.Column(db.String(200), nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_bug_reclaim_batch_status', 'status', 'created_at'),
        {'extend_existing': True},
    )

    items = db.relationship(
        'AdventurerS1BugReclaimItem', back_populates='batch',
        lazy='select', cascade='all, delete-orphan',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'admin_user_id': self.admin_user_id,
            'status': self.status,
            'criteria': self.criteria_json or {},
            'summary': self.summary_json or {},
            'note': self.note,
            'executed_at': to_utc_iso(self.executed_at) if self.executed_at else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1BugReclaimScanState(db.Model):
    """异常回收扫描水位、可续跑批次与数据库领取状态。"""

    __tablename__ = 'adventurer_s1_bug_reclaim_scan_states'

    id = db.Column(db.Integer, primary_key=True, default=1)
    detection_mode = db.Column(db.String(80), nullable=False)
    last_audit_log_id = db.Column(db.Integer, nullable=False, default=0, index=True)
    dirty_adventurer_ids_json = db.Column(db.JSON, nullable=False, default=list)
    dirty_revision = db.Column(db.Integer, nullable=False, default=0)
    scan_status = db.Column(db.String(20), nullable=False, default='ready')
    scan_mode = db.Column(db.String(20), nullable=False, default='incremental')
    scan_base_audit_log_id = db.Column(db.Integer, nullable=False, default=0)
    scan_target_audit_log_id = db.Column(db.Integer, nullable=False, default=0)
    scan_cursor_adventurer_id = db.Column(db.Integer, nullable=False, default=0)
    scan_total_adventurers = db.Column(db.Integer, nullable=False, default=0)
    scan_processed_adventurers = db.Column(db.Integer, nullable=False, default=0)
    scan_claim_token = db.Column(db.String(64), nullable=True)
    scan_claimed_at = db.Column(db.DateTime, nullable=True)
    scan_last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = ({'extend_existing': True},)


class AdventurerS1BugReclaimCandidate(db.Model):
    """异常回收待处理候选；按产生候选的源冒险者精确替换。"""

    __tablename__ = 'adventurer_s1_bug_reclaim_candidates'

    issue_key = db.Column(db.String(120), primary_key=True)
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    source_adventurer_id = db.Column(db.Integer, nullable=False, default=0)
    audit_log_id = db.Column(db.Integer, nullable=True, index=True)
    illegal_quantity = db.Column(db.Integer, nullable=False, default=0, index=True)
    detection_mode = db.Column(db.String(80), nullable=False, index=True)
    payload_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.Index('idx_advs1_bug_reclaim_candidate_adv_audit', 'adventurer_id', 'audit_log_id'),
        db.Index(
            'idx_advs1_bug_reclaim_candidate_source_mode',
            'source_adventurer_id', 'detection_mode',
        ),
        {'extend_existing': True},
    )


class AdventurerS1CompanionEvolutionScanState(db.Model):
    """伙伴嬗变完整性扫描的持久水位与已完成预览产物。"""

    __tablename__ = 'adventurer_s1_companion_evolution_scan_states'

    id = db.Column(db.Integer, primary_key=True, default=1)
    detection_mode = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='scanning', index=True)
    source_cursors_json = db.Column(db.JSON, nullable=False, default=dict)
    source_targets_json = db.Column(db.JSON, nullable=False, default=dict)
    source_revisions_json = db.Column(db.JSON, nullable=False, default=dict)
    dirty_expedition_log_ids_json = db.Column(db.JSON, nullable=False, default=list)
    cached_result_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = ({'extend_existing': True},)


class AdventurerS1CompanionEvolutionScanEvent(db.Model):
    """已扫描的嬗变、交易与伙伴连续性事件；旧历史无需重复读取。"""

    __tablename__ = 'adventurer_s1_companion_evolution_scan_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    source_type = db.Column(db.String(40), nullable=False, index=True)
    source_id = db.Column(db.Integer, nullable=False)
    event_at = db.Column(db.DateTime, nullable=True, index=True)
    payload_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'source_type', 'source_id',
            name='uq_advs1_companion_evolution_scan_event_source',
        ),
        db.Index(
            'idx_advs1_companion_evolution_scan_event_page',
            'source_type', 'source_id',
        ),
        {'extend_existing': True},
    )


class AdventurerS1BugReclaimItem(db.Model):
    """异常收益回收明细：一行对应一个已判定的非法增量来源。"""

    __tablename__ = 'adventurer_s1_bug_reclaim_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_bug_reclaim_batches.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending/executed/skipped/dismissed/adjusted/failed
    issue_key = db.Column(db.String(120), nullable=False, index=True)
    issue_type = db.Column(db.String(40), nullable=False, index=True)
    appeal_id = db.Column(db.Integer, nullable=True, index=True)
    audit_log_id = db.Column(db.Integer, nullable=True, index=True)
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(50), nullable=False, default='')
    item_rarity = db.Column(db.String(20), nullable=False, default='common')
    illegal_quantity = db.Column(db.Integer, nullable=False, default=0)
    reclaimed_item_quantity = db.Column(db.Integer, nullable=False, default=0)
    gold_deducted = db.Column(db.Float, nullable=False, default=0.0)
    before_json = db.Column(db.JSON, nullable=True)
    after_json = db.Column(db.JSON, nullable=True)
    route_json = db.Column(db.JSON, nullable=True)
    result_json = db.Column(db.JSON, nullable=True)
    executed_by = db.Column(db.Integer, nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    reverted_by = db.Column(db.Integer, nullable=True)
    reverted_at = db.Column(db.DateTime, nullable=True, index=True)
    revert_result_json = db.Column(db.JSON, nullable=True)
    error_message = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('issue_key', 'status', name='uq_advs1_bug_reclaim_issue_status'),
        db.Index('idx_advs1_bug_reclaim_adv_status', 'adventurer_id', 'status'),
        {'extend_existing': True},
    )

    batch = db.relationship('AdventurerS1BugReclaimBatch', back_populates='items')

    def to_dict(self):
        return {
            'id': self.id,
            'batch_id': self.batch_id,
            'status': self.status,
            'issue_key': self.issue_key,
            'issue_type': self.issue_type,
            'appeal_id': self.appeal_id,
            'audit_log_id': self.audit_log_id,
            'adventurer_id': self.adventurer_id,
            'user_id': self.user_id,
            'item_name': self.item_name,
            'item_type': self.item_type,
            'item_rarity': self.item_rarity,
            'illegal_quantity': int(self.illegal_quantity or 0),
            'reclaimed_item_quantity': int(self.reclaimed_item_quantity or 0),
            'gold_deducted': _round_money(self.gold_deducted),
            'before': self.before_json or {},
            'after': self.after_json or {},
            'route': self.route_json or [],
            'result': self.result_json or {},
            'error_message': self.error_message,
            'executed_at': to_utc_iso(self.executed_at) if self.executed_at else None,
            'reverted_by': self.reverted_by,
            'reverted_at': to_utc_iso(self.reverted_at) if self.reverted_at else None,
            'revert_result': self.revert_result_json or {},
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1BugQtyShortfallBatch(db.Model):
    """管理员补发数据申诉历史漏写数量的批次台账。"""

    __tablename__ = 'adventurer_s1_bug_qty_shortfall_batches'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_user_id = db.Column(db.Integer, nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='preview')  # preview/executed/dismissed/adjusted/failed
    criteria_json = db.Column(db.JSON, nullable=True)
    summary_json = db.Column(db.JSON, nullable=True)
    note = db.Column(db.String(200), nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_bug_qty_shortfall_batch_status', 'status', 'created_at'),
        {'extend_existing': True},
    )

    items = db.relationship(
        'AdventurerS1BugQtyShortfallItem', back_populates='batch',
        lazy='select', cascade='all, delete-orphan',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'admin_user_id': self.admin_user_id,
            'status': self.status,
            'criteria': self.criteria_json or {},
            'summary': self.summary_json or {},
            'note': self.note,
            'executed_at': to_utc_iso(self.executed_at) if self.executed_at else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1BugQtyShortfallItem(db.Model):
    """异常数量漏给明细：一行对应一次 name/desc 已归位但 quantity 漏写的候选。"""

    __tablename__ = 'adventurer_s1_bug_qty_shortfall_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_bug_qty_shortfall_batches.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending/executed/skipped/dismissed/adjusted/failed
    issue_key = db.Column(db.String(120), nullable=False, index=True)
    issue_type = db.Column(db.String(40), nullable=False, index=True)
    appeal_id = db.Column(db.Integer, nullable=True, index=True)
    audit_log_id = db.Column(db.Integer, nullable=True, index=True)
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(50), nullable=False, default='')
    item_rarity = db.Column(db.String(20), nullable=False, default='common')
    intended_quantity = db.Column(db.Integer, nullable=False, default=0)
    actual_quantity = db.Column(db.Integer, nullable=False, default=0)
    shortfall_quantity = db.Column(db.Integer, nullable=False, default=0)
    granted_quantity = db.Column(db.Integer, nullable=False, default=0)
    before_json = db.Column(db.JSON, nullable=True)
    after_json = db.Column(db.JSON, nullable=True)
    route_json = db.Column(db.JSON, nullable=True)
    result_json = db.Column(db.JSON, nullable=True)
    executed_by = db.Column(db.Integer, nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('issue_key', 'status', name='uq_advs1_bug_qty_shortfall_issue_status'),
        db.Index('idx_advs1_bug_qty_shortfall_adv_status', 'adventurer_id', 'status'),
        {'extend_existing': True},
    )

    batch = db.relationship('AdventurerS1BugQtyShortfallBatch', back_populates='items')

    def to_dict(self):
        return {
            'id': self.id,
            'batch_id': self.batch_id,
            'status': self.status,
            'issue_key': self.issue_key,
            'issue_type': self.issue_type,
            'appeal_id': self.appeal_id,
            'audit_log_id': self.audit_log_id,
            'adventurer_id': self.adventurer_id,
            'user_id': self.user_id,
            'item_name': self.item_name,
            'item_type': self.item_type,
            'item_rarity': self.item_rarity,
            'intended_quantity': int(self.intended_quantity or 0),
            'actual_quantity': int(self.actual_quantity or 0),
            'shortfall_quantity': int(self.shortfall_quantity or 0),
            'granted_quantity': int(self.granted_quantity or 0),
            'before': self.before_json or {},
            'after': self.after_json or {},
            'route': self.route_json or [],
            'result': self.result_json or {},
            'error_message': self.error_message,
            'executed_at': to_utc_iso(self.executed_at) if self.executed_at else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1NoticeAck(db.Model):
    """玩家已确认的酒馆公告版本（以 SystemConfig.updated_at 的 UTC ISO 字符串追踪）。"""

    __tablename__ = 'adventurer_s1_notice_acks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True,
    )
    notice_version = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'notice_version': self.notice_version,
            'created_at': to_utc_iso(self.created_at),
            'updated_at': to_utc_iso(self.updated_at),
        }


class AdventurerS1PlaceholderSkillRecallOffer(db.Model):
    """占位「xxx入门」技能回收换卷轴：管理员推送，玩家同意后删除技能并补发职业通用技能卷轴。"""

    __tablename__ = 'adventurer_s1_placeholder_skill_recall_offers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending/accepted/declined/cancelled
    skill_snapshot = db.Column(db.JSON, nullable=False, default=list)  # [{id, name}]
    scroll_grant_count = db.Column(db.Integer, nullable=False, default=1)
    admin_note = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    responded_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    adventurer = db.relationship('AdventurerS1')
    user = db.relationship('User')

    __table_args__ = (
        db.Index('idx_advs1_psr_offer_adv_status', 'adventurer_id', 'status'),
        {'extend_existing': True},
    )

    def to_player_dict(self, *, adventurer_name: str | None = None) -> dict:
        skills = self.skill_snapshot if isinstance(self.skill_snapshot, list) else []
        return {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': adventurer_name,
            'status': self.status or 'pending',
            'placeholder_skills': [
                {'id': s.get('id'), 'name': s.get('name')}
                for s in skills if isinstance(s, dict) and s.get('name')
            ],
            'scroll_grant_count': int(self.scroll_grant_count or 0),
            'scroll_name': '职业通用技能卷轴',
            'admin_note': (self.admin_note or '').strip()[:500] or None,
            'sent_at': to_utc_iso(self.sent_at) if self.sent_at else None,
        }


class AdventurerS1SupportVote(db.Model):
    """深渊酒馆 S1 个人页支持投票：每个账号最多一票。"""

    __tablename__ = 'adventurer_s1_support_votes'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True,
    )
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    option_key = db.Column(db.String(50), nullable=False, default='support_s1')
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    user = db.relationship('User')
    adventurer = db.relationship('AdventurerS1')

    __table_args__ = (
        db.Index('idx_advs1_support_vote_created', 'created_at'),
        {'extend_existing': True},
    )

    def to_admin_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'nickname': (self.user.nickname or self.user.username) if self.user else None,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': self.adventurer.name if self.adventurer else None,
            'option_key': self.option_key,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


# ── 英灵碑林：阵亡冒险者墓碑 ────────────────────────────────────────────────────


class AdventurerS1Tombstone(db.Model):
    """阵亡冒险者墓碑：死亡时写入 epitaph（来自探险结算 death_reason）。"""

    __tablename__ = 'adventurer_s1_tombstones'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True,
    )
    owner_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    name = db.Column(db.String(100), nullable=False)
    character_class = db.Column(db.String(50), nullable=True)
    level = db.Column(db.Integer, nullable=False, default=1)
    epitaph = db.Column(db.Text, nullable=False, default='')
    expedition_log_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    died_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('idx_advs1_tombstone_died_at_id', 'died_at', 'id'),
        {'extend_existing': True},
    )

    messages = db.relationship(
        'AdventurerS1TombstoneMessage', back_populates='tombstone',
        lazy='select', cascade='all, delete-orphan',
    )
    tributes = db.relationship(
        'AdventurerS1TombstoneTribute', back_populates='tombstone',
        lazy='select', cascade='all, delete-orphan',
    )

    def to_dict(
        self,
        *,
        message_count: int | None = None,
        recent_messages: list | None = None,
        tribute_count: int | None = None,
        tribute_summary: list | None = None,
    ):
        res = {
            'id': self.id,
            'adventurer_id': self.adventurer_id,
            'name': self.name,
            'character_class': self.character_class,
            'level': self.level,
            'epitaph': self.epitaph,
            'died_at': to_utc_iso(self.died_at) if self.died_at else None,
        }
        if message_count is not None:
            res['message_count'] = message_count
        if recent_messages is not None:
            res['recent_messages'] = recent_messages
        if tribute_count is not None:
            res['tribute_count'] = tribute_count
        if tribute_summary is not None:
            res['tribute_summary'] = tribute_summary
        return res


class AdventurerS1TombstoneMessage(db.Model):
    """英灵碑林：玩家在墓碑下的留言。"""

    __tablename__ = 'adventurer_s1_tombstone_messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tombstone_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_tombstones.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    content = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    tombstone = db.relationship('AdventurerS1Tombstone', back_populates='messages')
    user = db.relationship('User')

    __table_args__ = (
        db.Index('idx_advs1_tomb_msg_stone_time', 'tombstone_id', 'created_at'),
        {'extend_existing': True},
    )

    def to_dict(self):
        return {
            'id': self.id,
            'nickname': (self.user.nickname or self.user.username) if self.user else '未知',
            'content': self.content,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1TombstoneTribute(db.Model):
    """英灵碑林：玩家以冒险者名义给墓碑献花的记录。"""

    __tablename__ = 'adventurer_s1_tombstone_tributes'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tombstone_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_tombstones.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    flower_code = db.Column(db.String(50), nullable=False)
    flower_name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    # nullable 仅用于兼容自动迁移前的历史献花记录；新写入始终保存价格快照。
    unit_price = db.Column(db.Float, nullable=True, default=0.0)
    total_price = db.Column(db.Float, nullable=True, default=0.0)
    message = db.Column(db.Text, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    tombstone = db.relationship('AdventurerS1Tombstone', back_populates='tributes')
    user = db.relationship('User')
    adventurer = db.relationship('AdventurerS1')

    __table_args__ = (
        db.Index('idx_advs1_tomb_tribute_stone_time', 'tombstone_id', 'created_at'),
        {'extend_existing': True},
    )

    @staticmethod
    def _resolve_flower_emoji(flower_code: str) -> str:
        catalog = current_app.config.get('ADVENTURER_S1_FLOWER_SHOP_CATALOG') or {}
        entry = catalog.get(flower_code) or {}
        return entry.get('emoji', '💐')

    def to_dict(self):
        return {
            'id': self.id,
            'adventurer_name': self.adventurer.name if self.adventurer else None,
            'flower_name': self.flower_name,
            'flower_emoji': self._resolve_flower_emoji(self.flower_code),
            'quantity': self.quantity,
            'message': self.message,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }


class AdventurerS1ExpeditionAudit(db.Model):
    """已下线探险结算审核的历史记录模型；仅用于保留既有数据与查账语义。"""

    __tablename__ = 'adventurer_s1_expedition_audits'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expedition_log_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    admin_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)  # 发起审核的玩家（玩家发起时）
    model_used = db.Column(db.String(50), nullable=False, default='deepseek-v4-flash')

    ai_status = db.Column(db.String(20), nullable=False, default='pending')  # pending/processing/done/failed
    ai_raw_response = db.Column(ColdCompressedText, nullable=True)
    ai_summary = db.Column(db.Text, nullable=True)
    proposed_patch = db.Column(ColdCompressedJSON, nullable=True)

    status = db.Column(db.String(30), nullable=False, default='processing')  # pending_votes/processing/awaiting_review/approved/rejected/cancelled
    vote_status = db.Column(db.JSON, nullable=True)  # {user_id: "yes"/"no", ...} 玩家投票状态
    applied_fields = db.Column(db.JSON, nullable=True)  # 管理员实际选择应用的字段子集
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    applied_at = db.Column(db.DateTime, nullable=True)
    reject_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)
    cold_compressed_at = db.Column(db.DateTime, nullable=True, index=True)

    __table_args__ = (
        db.Index('ix_advs1_audit_status_created', 'status', 'created_at'),
        {'extend_existing': True},
    )

    log = db.relationship('AdventurerS1ExpeditionLog', lazy='select')

    def to_dict(self, *, include_patch=False, include_ai_fields=False):
        from utils.time_utils import to_utc_iso
        data = {
            'id': self.id,
            'expedition_log_id': self.expedition_log_id,
            'admin_user_id': self.admin_user_id,
            'requested_by': self.requested_by,
            'model_used': self.model_used,
            'ai_status': self.ai_status,
            'status': self.status,
            'vote_status': self.vote_status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'reviewed_at': to_utc_iso(self.reviewed_at) if self.reviewed_at else None,
            'applied_at': to_utc_iso(self.applied_at) if self.applied_at else None,
            'reject_reason': self.reject_reason,
        }
        if self.log:
            data['target_dungeon'] = self.log.target_dungeon
            data['party_name'] = self.log.party_name
            data['log_status'] = self.log.status
        if include_ai_fields or include_patch:
            data['ai_summary'] = self.ai_summary
        if include_patch:
            data['proposed_patch'] = self.proposed_patch
            data['ai_raw_response'] = self.ai_raw_response
            data['applied_fields'] = self.applied_fields
        return data

    def to_player_dict(self, *, include_summary: bool = False) -> dict:
        """玩家端审核摘要，只返回页面实际展示所需字段。"""
        from utils.time_utils import to_utc_iso

        data = {
            'id': self.id,
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'reject_reason': self.reject_reason,
        }
        if self.log:
            data['target_dungeon'] = self.log.target_dungeon
            data['party_name'] = self.log.party_name
        if include_summary:
            data['ai_summary'] = self.ai_summary
        return data


class AdventurerS1ExpeditionObserverReview(db.Model):
    """每次主探险 AI 生成后的只读剧情审核，仅供管理员观察。"""

    __tablename__ = 'adventurer_s1_expedition_observer_reviews'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expedition_log_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    # model_used 保留既有“逻辑请求模型”语义；实际物理路由单独记录，
    # 避免历史行与新行在同一列中混用两种口径。
    model_used = db.Column(db.String(100), nullable=False)
    correlation_id = db.Column(db.String(64), nullable=True)
    effective_model_used = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    result = db.Column(ColdCompressedJSON, nullable=True)
    raw_response = db.Column(ColdCompressedText, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    # status 只描述模型审核合同/任务本身；最终候选是否被 runner/结算消费者
    # 接受另行记录，避免“模型输出完成”被误读为“剧情已成功落库”。
    consumption_status = db.Column(db.String(20), nullable=True)
    consumption_failure_stage = db.Column(db.String(80), nullable=True)
    consumption_error_code = db.Column(db.String(160), nullable=True)
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)
    cold_compressed_at = db.Column(db.DateTime, nullable=True, index=True)

    __table_args__ = (
        db.Index('ix_advs1_observer_review_log_created', 'expedition_log_id', 'created_at'),
        db.Index('ix_advs1_observer_review_correlation', 'correlation_id'),
        {'extend_existing': True},
    )

    def to_admin_dict(self) -> dict:
        return {
            'id': self.id,
            'expedition_log_id': self.expedition_log_id,
            'model_used': self.model_used,
            'correlation_id': self.correlation_id,
            'effective_model_used': self.effective_model_used,
            'status': self.status,
            'model_contract_status': self.status,
            'consumption_status': self.consumption_status,
            'consumption_failure_stage': self.consumption_failure_stage,
            'consumption_error_code': self.consumption_error_code,
            'result': self.result,
            'raw_response': self.raw_response,
            'error_message': self.error_message,
            'retry_count': self.retry_count or 0,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'updated_at': to_utc_iso(self.updated_at) if self.updated_at else None,
        }


class AdventurerS1ExpeditionAuditVoter(db.Model):
    """已下线探险结算审核的历史投票人快照。"""
    """审核创建时冻结的投票人；资格不随小队或冒险者后续状态变化。"""

    __tablename__ = 'adventurer_s1_expedition_audit_voters'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    audit_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_expedition_audits.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_ids = db.Column(db.JSON, nullable=False, default=list)
    vote = db.Column(db.String(10), nullable=True)  # yes/no；进入结果确认阶段时重置
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('audit_id', 'user_id', name='uq_advs1_audit_voter'),
        db.Index('ix_advs1_audit_voter_user_audit', 'user_id', 'audit_id'),
        {'extend_existing': True},
    )

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'adventurer_ids': list(self.adventurer_ids or []),
            'vote': self.vote,
        }


class AdventurerS1Commission(db.Model):
    """悬赏布告栏委托：DS 模型每小时批量生成的分级委托。

    流程：玩家用贡献度接取（消耗型）→ 把绑定冒险者派进 target_dungeons 之一时按
    encounter_prob 触发委托事件 → 探险 AI 在剧情里判定是否完成 → 完成转 fulfilled，
    玩家回布告栏领取金币/物品奖励（仅接取本人可领）。

    - 高级委托（advanced）固定为讨伐任务，boss_name/boss_background 复用隐藏 boss 注入格式，
      完成判定走 commission_results，不污染固定 6-boss 的状态机。
    - 一个冒险者同时最多 1 个 accepted/fulfilled 委托；accepted 状态可取消（全额退还贡献度）。
    - encounter_prob 等内部判定字段不外露给玩家（纯后端判断）。
    """

    __tablename__ = 'adventurer_s1_commissions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tier = db.Column(db.String(20), nullable=False, index=True)  # junior / intermediate / advanced
    task_type = db.Column(db.String(20), nullable=False, default='gather')  # gather/rescue/escort/combat/subjugation
    title = db.Column(db.String(60), nullable=False)
    background = db.Column(db.Text, nullable=False, default='')        # 剧情背景
    requirement_text = db.Column(db.Text, nullable=False, default='')  # 任务需求（触发时传给探险 AI）
    # 数量进度：数量类委托(如"消灭8只")目标数 required_count，已累计完成 progress_count（跨探险累加）。
    # 非数量类(护送/讨伐单一Boss/找回单件)= 1，一次达成即完成。
    required_count = db.Column(db.Integer, nullable=False, server_default='1', default=1)
    progress_count = db.Column(db.Integer, nullable=False, server_default='0', default=0)
    target_dungeons = db.Column(db.JSON, nullable=False, default=list)  # [dungeon_name, ...]
    accept_cost = db.Column(db.Integer, nullable=False, default=0)
    encounter_prob = db.Column(db.Float, nullable=False, default=0.0)  # 内部判定字段，不外露
    reward_gold = db.Column(db.Integer, nullable=False, default=0)
    reward_items = db.Column(db.JSON, nullable=True)  # [{name, quantity}]，均为已校验的图鉴物品
    boss_name = db.Column(db.String(100), nullable=True)        # 仅 advanced 讨伐
    boss_background = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='open', index=True)  # open/accepted/fulfilled/claimed/expired
    accepted_by_adventurer_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1.id', ondelete='SET NULL'), nullable=True, index=True,
    )
    accepted_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True,
    )
    accepted_at = db.Column(db.DateTime, nullable=True)
    fulfilled_at = db.Column(db.DateTime, nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    completed_expedition_log_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)

    __table_args__ = (
        db.Index('ix_advs1_commission_status_tier', 'status', 'tier'),
        {'extend_existing': True},
    )

    def to_dict(self, include_internal=False):
        """玩家可见卡片字段。include_internal=True 才带 encounter_prob 等内部字段（管理员/调试）。"""
        data = {
            'id': self.id,
            'tier': self.tier,
            'task_type': self.task_type,
            'title': self.title,
            'background': self.background,
            'requirement_text': self.requirement_text,
            'required_count': self.required_count or 1,
            'progress_count': self.progress_count or 0,
            'target_dungeons': list(self.target_dungeons or []),
            'accept_cost': self.accept_cost,
            'reward_gold': self.reward_gold,
            'reward_items': self.reward_items or [],
            'status': self.status,
            'accepted_by_adventurer_id': self.accepted_by_adventurer_id,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
        if self.boss_name:
            data['boss_name'] = self.boss_name
            data['boss_background'] = self.boss_background
        if include_internal:
            data['encounter_prob'] = self.encounter_prob
            data['accepted_by_user_id'] = self.accepted_by_user_id
            data['accepted_at'] = to_utc_iso(self.accepted_at) if self.accepted_at else None
            data['fulfilled_at'] = to_utc_iso(self.fulfilled_at) if self.fulfilled_at else None
            data['claimed_at'] = to_utc_iso(self.claimed_at) if self.claimed_at else None
            data['completed_expedition_log_id'] = self.completed_expedition_log_id
        return data
