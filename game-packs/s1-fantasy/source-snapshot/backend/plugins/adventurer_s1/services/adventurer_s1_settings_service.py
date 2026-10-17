"""S1 专属运行时配置读写服务。

存放在 `adventurer_s1_settings` 表，key-value 结构。读优先表，回退 app.config。
当前管理的 key:
- dormitory_max_concurrent: int —— 全服「locked + building」宿舍在建/待建并发上限
- dormitory_max_total: int —— 玩家共建宿舍楼上限（不含种子房间升级占位实例）
- auto_draw_on_dormitory_complete: bool —— 宿舍完工后按床位数自动抽取预约冒险者入住
- daily_reservation_draw_enabled: bool —— 每天北京时间 20:00 按空床数自动抽取预约玩家
- waiting_player_npc_enabled: bool —— 是否允许把待抽取玩家作为可选路人 NPC 注入探险
- party_* / expedition_*: int —— 探险组队与调度间隔（持久化，管理员可热更新）
- data_appeal_daily_limit: int —— 全服每名玩家每日其余申诉基础次数
- data_appeal_other_daily_limit: int —— 全服每名玩家每日全文申诉（其他问题/稀有度复核）基础次数
- data_appeal_other_balance_cap: int —— 全服每名玩家全文申诉余额池上限
"""
import json
from typing import Optional, Tuple
from flask import current_app
from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1Settings
SETTING_KEYS = {'dormitory_max_concurrent': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_DORMITORY_MAX_CONCURRENT_BUILDING', 'fallback_default': 2, 'description': '全服同时在建/待建的宿舍楼上限', 'min_value': 1, 'max_value': 100}, 'dormitory_max_total': {'type': 'int', 'fallback_config_key': None, 'fallback_default': 10, 'description': '玩家共建宿舍楼上限（不含种子房间升级占位实例）', 'min_value': 1, 'max_value': 200}, 'auto_draw_on_dormitory_complete': {'type': 'bool', 'fallback_config_key': None, 'fallback_default': False, 'description': '宿舍完工后按当前剩余空床数自动抽取预约冒险者入住（默认关闭，由管理员手动抽取）', 'category': 'reservation'}, 'daily_reservation_draw_enabled': {'type': 'bool', 'fallback_config_key': None, 'fallback_default': False, 'description': '每天北京时间 20:00 按当前剩余空床数自动抽取预约玩家（沿用手动抽取规则）', 'category': 'reservation'}, 'waiting_player_npc_enabled': {'type': 'bool', 'fallback_config_key': None, 'fallback_default': False, 'description': '允许把待抽取玩家作为可选剧情角色注入探险；实际出场者与其它参与主体具有同等叙事资格，候选不强制出场', 'category': 'reservation', 'admin_only': True}, 'reservation_alt_auto_low_priority': {'type': 'bool', 'fallback_config_key': None, 'fallback_default': False, 'description': '自动把同伙检测命中的临时提升预约降为低优先级（含注册/登录 IP 和 /24 网段关联）', 'category': 'reservation', 'admin_only': True}, 'party_min_size': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_PARTY_MIN_SIZE', 'fallback_default': 4, 'description': '普通匹配成团人数下限', 'min_value': 1, 'max_value': 20, 'category': 'expedition'}, 'party_max_size': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_PARTY_MAX_SIZE', 'fallback_default': 6, 'description': '普通匹配成团人数上限', 'min_value': 1, 'max_value': 20, 'category': 'expedition'}, 'departure_minutes': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_PARTY_DEPARTURE_MINUTES', 'fallback_default': 30, 'description': '特殊机缘等手动小队满员后出发倒计时（分钟）；普通匹配不倒计时', 'min_value': 1, 'max_value': 1440, 'category': 'expedition'}, 'rank_cooldown_minutes': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_PARTY_RANK_COOLDOWN_MINUTES', 'fallback_default': 15, 'description': '同 rank 有队在 exploring 且已有 forming 时，出发未满 N 分钟内不再 seed 新空壳队；disbanded 后立即解除', 'min_value': 0, 'max_value': 10080, 'category': 'expedition'}, 'max_forming_per_rank': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_MAX_FORMING_PER_RANK', 'fallback_default': 2, 'description': '同 rank 同时在组队（forming）的小队数上限；exploring 不计入', 'min_value': 1, 'max_value': 10, 'category': 'expedition'}, 'slow_period_start_hour': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_SLOW_PERIOD_START_HOUR', 'fallback_default': 2, 'description': '慢时段开始（北京时间，整点）', 'min_value': 0, 'max_value': 23, 'category': 'expedition'}, 'slow_period_enabled': {'type': 'bool', 'fallback_config_key': 'ADVENTURER_S1_SLOW_PERIOD_ENABLED', 'fallback_default': True, 'description': '是否启用慢时段组队降速', 'category': 'expedition'}, 'slow_period_end_hour': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_SLOW_PERIOD_END_HOUR', 'fallback_default': 9, 'description': '慢时段结束（北京时间，整点，不含该时刻）', 'min_value': 0, 'max_value': 23, 'category': 'expedition'}, 'slow_period_factor': {'type': 'float', 'fallback_config_key': 'ADVENTURER_S1_SLOW_PERIOD_FACTOR', 'fallback_default': 0.25, 'description': '慢时段 seed 概率倍率（0~1）', 'min_value': 0.0, 'max_value': 1.0, 'category': 'expedition'}, 'fast_period_enabled': {'type': 'bool', 'fallback_config_key': 'ADVENTURER_S1_FAST_PERIOD_ENABLED', 'fallback_default': False, 'description': '是否启用快时段组队/发车加速', 'category': 'expedition'}, 'fast_period_start_hour': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_FAST_PERIOD_START_HOUR', 'fallback_default': 18, 'description': '快时段开始（北京时间，整点）', 'min_value': 0, 'max_value': 23, 'category': 'expedition'}, 'fast_period_end_hour': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_FAST_PERIOD_END_HOUR', 'fallback_default': 23, 'description': '快时段结束（北京时间，整点，不含该时刻）', 'min_value': 0, 'max_value': 23, 'category': 'expedition'}, 'fast_period_factor': {'type': 'float', 'fallback_config_key': 'ADVENTURER_S1_FAST_PERIOD_FACTOR', 'fallback_default': 2.0, 'description': '快时段组队概率与单轮发车上限倍率', 'min_value': 1.0, 'max_value': 10.0, 'category': 'expedition'}, 'hidden_dungeon_enabled': {'type': 'bool', 'fallback_config_key': 'ADVENTURER_S1_HIDDEN_DUNGEON_ENABLED', 'fallback_default': True, 'description': '是否允许常规副本在发车时独立触发隐藏副本', 'category': 'expedition', 'admin_only': True}, 'ai_calls_enabled': {'type': 'bool', 'fallback_config_key': 'ADVENTURER_S1_AI_CALLS_ENABLED', 'fallback_default': True, 'description': 'S1 是否允许发起 AI 调用', 'category': 'expedition', 'admin_only': True}, 'ai_blackout_enabled': {'type': 'bool', 'fallback_config_key': 'ADVENTURER_S1_AI_BLACKOUT_ENABLED', 'fallback_default': False, 'description': '是否按北京时间停用窗口暂停 S1 AI 调用', 'category': 'expedition', 'admin_only': True}, 'ai_blackout_windows': {'type': 'json', 'fallback_config_key': None, 'fallback_default': [], 'description': 'S1 AI 停用窗口（北京时间 HH:MM）', 'category': 'expedition', 'admin_only': True}, 'party_form_interval_minutes': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_PARTY_FORM_INTERVAL_MINUTES', 'fallback_default': 5, 'description': '组队 tick 间隔（分钟）：按发车配额从普通匹配队列成团', 'min_value': 1, 'max_value': 1440, 'category': 'expedition', 'scheduler_job_id': 'adventurer_s1_form_parties_tick'}, 'expedition_dispatch_interval_minutes': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_EXPEDITION_DISPATCH_INTERVAL_MINUTES', 'fallback_default': 1, 'description': '自动发车 tick 间隔（分钟）', 'min_value': 1, 'max_value': 60, 'category': 'expedition', 'scheduler_job_id': 'adventurer_s1_expedition_dispatch_tick'}, 'expedition_progress_interval_minutes': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_EXPEDITION_PROGRESS_INTERVAL_MINUTES', 'fallback_default': 5, 'description': '分批推进 tick 间隔（分钟）', 'min_value': 1, 'max_value': 60, 'category': 'expedition', 'scheduler_job_id': 'adventurer_s1_expedition_progress_tick'}, 'daily_expedition_quota': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_DAILY_EXPEDITION_QUOTA', 'fallback_default': 100, 'description': '每日探险队伍配额（北京日界，AI 调用闸门），0 = 不限制', 'min_value': 0, 'max_value': 10000, 'category': 'expedition'}, 'adventurer_max_level': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_MAX_LEVEL', 'fallback_default': 10, 'description': 'S1 冒险者等级上限', 'min_value': 1, 'max_value': 15, 'category': 'progression'}, 'expedition_dispatch_paused': {'type': 'bool', 'fallback_default': False, 'description': '静默暂停自动/手动发车（玩家侧组队、投票、倒计时照常，仅调度层不发车）', 'category': 'expedition', 'admin_only': True}, 'generating_gm_retry_minutes': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_GENERATING_GM_RETRY_MINUTES', 'fallback_default': 75, 'description': '探险日志 generating 阶段心跳超过该分钟数后自动补投，并允许 GM 人工重试', 'min_value': 1, 'max_value': 180, 'category': 'expedition'}, 'expedition_story_hard_min_chars': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_EXPEDITION_STORY_HARD_MIN_CHARS', 'fallback_default': 1000, 'description': '常规探险玩家可见正文的异常短硬底线；当前值会告知主剧情 AI（0 = 关闭）', 'min_value': 0, 'max_value': 10000, 'category': 'expedition'}, 'expedition_story_soft_target_chars': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_EXPEDITION_STORY_SOFT_TARGET_CHARS', 'fallback_default': 4000, 'description': '主剧情玩家可见正文的低优先级展开建议；不参与后端拒绝、审核打回或重试（0 = 关闭）', 'min_value': 0, 'max_value': 20000, 'category': 'expedition'}, 'expedition_raw_paragraphs_min': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_EXPEDITION_RAW_PARAGRAPHS_MIN', 'fallback_default': 6, 'description': '固定 6 段分批推送硬下限；三段式按空行分段，纯 JSON 兼容输出按 events 计数', 'min_value': 6, 'max_value': 6, 'category': 'expedition'}, 'tavern_level': {'type': 'int', 'fallback_config_key': None, 'fallback_default': 1, 'description': '深渊酒馆当前等级（升级后解锁建筑 2 级改造）', 'min_value': 1, 'max_value': 10, 'category': 'tavern'}, 'tavern_upgrade_ui_enabled': {'type': 'bool', 'fallback_config_key': 'ADVENTURER_S1_TAVERN_UPGRADE_UI_ENABLED', 'fallback_default': False, 'description': '是否在酒馆/建筑 Tab 显示酒馆升级进度面板', 'category': 'tavern'}, 'data_appeal_auto_approve': {'type': 'bool', 'fallback_config_key': 'ADVENTURER_S1_DATA_APPEAL_AUTO_APPROVE', 'fallback_default': False, 'description': '数据申诉 AI 分析完成后自动通过/驳回，无需管理员手动确认', 'category': 'appeal'}, 'data_appeal_daily_limit': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_DATA_APPEAL_DAILY_LIMIT', 'fallback_default': 5, 'description': '全服每名玩家每日可发起的其余申诉次数（不含其他问题和稀有度复核，北京时间 0 点刷新）', 'min_value': 1, 'max_value': 100, 'category': 'appeal', 'admin_only': True}, 'data_appeal_other_daily_limit': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_DATA_APPEAL_OTHER_DAILY_LIMIT', 'fallback_default': 5, 'description': '全服每名玩家每日分配的全文申诉基础次数（其他问题和稀有度复核，当日优先使用）', 'min_value': 1, 'max_value': 100, 'category': 'appeal', 'admin_only': True}, 'data_appeal_other_balance_cap': {'type': 'int', 'fallback_config_key': 'ADVENTURER_S1_DATA_APPEAL_OTHER_BALANCE_CAP', 'fallback_default': 5, 'description': '每名玩家可长期保存的全文申诉余额池上限；每日未用基础次数在次日转入余额池', 'min_value': 0, 'max_value': 100, 'category': 'appeal', 'admin_only': True}, 'data_appeal_other_balance_started_on': {'type': 'str', 'fallback_config_key': None, 'fallback_default': None, 'description': '全文申诉余额池启用的北京时间日期', 'category': 'appeal', 'internal_only': True}}
EXPEDITION_API_FIELDS = tuple((key for key, meta in SETTING_KEYS.items() if meta.get('category') == 'expedition'))

def _parse_value(key: str, raw: Optional[str]):
    """按 key 元数据的 type 解析字符串值，None 直接透传。"""
    if raw is None:
        return None
    meta = SETTING_KEYS.get(key) or {}
    t = meta.get('type', 'str')
    try:
        if t == 'int':
            return int(raw)
        if t == 'float':
            return float(raw)
        if t == 'bool':
            return raw.lower() in ('1', 'true', 'yes', 'on')
        if t == 'json':
            return json.loads(raw)
        return raw
    except (ValueError, TypeError):
        return None

def _is_value_within_configured_range(meta: dict, value) -> bool:
    """旧持久化值也必须服从当前元数据边界；越界时由读取方回退当前默认值。"""
    if value is None:
        return False
    try:
        if 'min_value' in meta and value < meta['min_value']:
            return False
        if 'max_value' in meta and value > meta['max_value']:
            return False
    except TypeError:
        return False
    return True

class S1SettingsService:

    @staticmethod
    def get(key: str):
        """读取设置：优先 settings 表 → 回退 app.config → 回退元数据 default。"""
        if key not in SETTING_KEYS:
            return None
        rec = AdventurerS1Settings.query.filter_by(key=key).first()
        return S1SettingsService._resolve_value(key, rec.value if rec is not None else None)

    @staticmethod
    def _resolve_value(key: str, raw: Optional[str]):
        """单项与批量读取共用持久化值校验及默认值优先级。"""
        if raw is not None:
            parsed = _parse_value(key, raw)
            if _is_value_within_configured_range(SETTING_KEYS[key], parsed):
                return parsed
        meta = SETTING_KEYS[key]
        fallback_key = meta.get('fallback_config_key')
        cfg_val = current_app.config.get(fallback_key) if fallback_key else None
        if cfg_val is not None:
            try:
                if meta.get('type') == 'int':
                    cfg_val = int(cfg_val)
                if _is_value_within_configured_range(meta, cfg_val):
                    return cfg_val
            except (ValueError, TypeError):
                pass
        return meta.get('fallback_default')

    @staticmethod
    def set(key: str, value, updated_by_user_id: Optional[int]=None) -> Tuple[bool, Optional[str]]:
        """写入设置。校验 key 已注册、类型可解析、在 min/max 范围内。"""
        if key == 'region_2_enabled' and current_app.config.get('ADVENTURER_S1_FIXED_REALM_CODE'):
            return (False, '二区准入只能由中央账号服务管理')
        meta = SETTING_KEYS.get(key)
        if not meta:
            return (False, f'未知配置 key: {key}')
        if meta.get('internal_only'):
            return (False, f'未知配置 key: {key}')
        t = meta.get('type', 'str')
        try:
            if t == 'int':
                v = int(value)
            elif t == 'float':
                v = float(value)
            elif t == 'bool':
                if isinstance(value, str):
                    v = value.lower() in ('1', 'true', 'yes', 'on')
                else:
                    v = bool(value)
            elif t == 'json':
                v = value
                json.dumps(v, ensure_ascii=False)
            else:
                v = str(value)
        except (ValueError, TypeError):
            return (False, f'值类型不合法：期望 {t}')
        if 'min_value' in meta and v < meta['min_value']:
            return (False, f"值不能小于 {meta['min_value']}")
        if 'max_value' in meta and v > meta['max_value']:
            return (False, f"值不能大于 {meta['max_value']}")
        rec = AdventurerS1Settings.query.filter_by(key=key).first()
        if rec is None:
            rec = AdventurerS1Settings(key=key, value=json.dumps(v, ensure_ascii=False) if t == 'json' else str(v), description=meta.get('description'), updated_by_user_id=updated_by_user_id)
            ok, err = db_manager.add(rec)
            if not ok:
                return (False, err)
        else:
            rec.value = json.dumps(v, ensure_ascii=False) if t == 'json' else str(v)
            if updated_by_user_id is not None:
                rec.updated_by_user_id = updated_by_user_id
            rec.description = meta.get('description')
        if key == 'data_appeal_other_balance_cap':
            from plugins.adventurer_s1.models import AdventurerS1DataAppealQuota
            AdventurerS1DataAppealQuota.query.filter(AdventurerS1DataAppealQuota.other_balance > v).update({AdventurerS1DataAppealQuota.other_balance: v}, synchronize_session=False)
        ok, err = db_manager.commit()
        if not ok:
            return (False, err)
        return (True, None)

    @staticmethod
    def list_all(include_admin_only: bool=False) -> list:
        """列出所有已注册 key 的当前值（含未持久化的回退值），供管理面板渲染。"""
        out = []
        for key, meta in SETTING_KEYS.items():
            if meta.get('internal_only'):
                continue
            if key == 'region_2_enabled' and current_app.config.get('ADVENTURER_S1_FIXED_REALM_CODE'):
                continue
            if meta.get('admin_only') and (not include_admin_only):
                continue
            out.append({'key': key, 'value': S1SettingsService.get(key), 'description': meta.get('description'), 'type': meta.get('type', 'str'), 'min_value': meta.get('min_value'), 'max_value': meta.get('max_value')})
        return out

    @staticmethod
    def list_player_dormitory_settings() -> list:
        """宿舍玩家读接口只提供页面使用的两个上限，不复用运营参数全表。"""
        return [{'key': key, 'value': S1SettingsService.get(key), 'description': SETTING_KEYS[key].get('description'), 'type': SETTING_KEYS[key]['type'], 'min_value': SETTING_KEYS[key].get('min_value'), 'max_value': SETTING_KEYS[key].get('max_value')} for key in ('dormitory_max_concurrent', 'dormitory_max_total')]

    @staticmethod
    def get_expedition_config() -> dict:
        """探险/组队配置扁平 dict，供 admin GET 与业务层读取。"""
        values = dict(db_manager.query(AdventurerS1Settings.key, AdventurerS1Settings.value).filter(AdventurerS1Settings.key.in_(EXPEDITION_API_FIELDS)).all())
        return {field: S1SettingsService._resolve_value(field, values.get(field)) for field in EXPEDITION_API_FIELDS}

    @staticmethod
    def sync_expedition_settings_to_app_config() -> None:
        """把探险设置同步到 app.config，兼容仍直读 config key 的代码。"""
        for key, meta in SETTING_KEYS.items():
            if meta.get('category') != 'expedition':
                continue
            cfg_key = meta.get('fallback_config_key')
            if not cfg_key:
                continue
            val = S1SettingsService.get(key)
            if val is not None:
                current_app.config[cfg_key] = val

    @staticmethod
    def sync_runtime_settings_to_app_config() -> None:
        """把持久化运行时设置同步到 app.config，兼容仍直读 config key 的代码。"""
        for key, meta in SETTING_KEYS.items():
            cfg_key = meta.get('fallback_config_key')
            if not cfg_key:
                continue
            val = S1SettingsService.get(key)
            if val is not None:
                current_app.config[cfg_key] = val

    @staticmethod
    def reschedule_expedition_scheduler_jobs() -> list[str]:
        """按当前设置热更新探险相关调度任务间隔。返回已成功 reschedule 的 job id。"""
        from services.scheduler_service import SchedulerService as scheduler_service
        updated: list[str] = []
        for key, meta in SETTING_KEYS.items():
            job_id = meta.get('scheduler_job_id')
            if not job_id:
                continue
            minutes = S1SettingsService.get(key)
            if minutes is None:
                continue
            ok, _ = scheduler_service.reschedule_interval_job(job_id, minutes=int(minutes))
            if ok:
                updated.append(job_id)
        return updated
