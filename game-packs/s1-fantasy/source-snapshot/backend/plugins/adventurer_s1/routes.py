"""深渊酒馆 S1 路由。

已实现接口：
- 插件健康检查
- 玩家自建冒险者（创建选项 + 创建）
- 冒险者列表 / 详情
- 装备操作（从仓库装备 / 卸下到仓库 / 批量 / 锁定切换）
- 技能配置（查看 / 替换）
"""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, request
from werkzeug.exceptions import RequestEntityTooLarge

from routes.auth import login_required
from utils.response import success, created, error
from utils.decorators import (
    admin_primary_required,
    admin_read_required,
    admin_required,
    generic_anti_cheat_check,
)
from utils.pagination import get_pagination_params
from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
from plugins.adventurer_s1.services.adventurer_s1_tavern_service import AdventurerS1TavernService
from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import AdventurerS1TeaHouseSeatService
from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import AdventurerS1HotSpringSeatService
from plugins.adventurer_s1.services.s1_squad_service import (
    S1SquadService,
    SQUAD_DISABLED_ERROR,
)

adventurer_s1_bp = Blueprint('adventurer_s1', __name__, url_prefix='/api/adventurer-s1')

from plugins.adventurer_s1.services.s1_team_loot_routes import register_team_loot_routes
register_team_loot_routes(adventurer_s1_bp)




def _conditional_get_success(data):
    """为大型私有 GET 响应附加 ETag；内容未变化时由浏览器复用本地响应体。"""
    response, _status_code = success(data)
    response.cache_control.private = True
    response.cache_control.no_cache = True
    response.vary.add('Authorization')
    response.vary.add('Cookie')
    response.add_etag()
    response.make_conditional(request)
    if response.status_code == 304:
        response.set_data(b'')
    return response


class AdminItemUpdateError(Exception):
    """管理员物品编辑/新增在事务块内的校验失败。

    db_manager.transaction() 在正常退出时提交；事务块内必须抛异常而非直接
    return error(...)，否则会提前提交块内已发生的未提交改动。
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _parse_admin_uses_left(body: dict) -> tuple[int | None, bool]:
    """解析管理员表单 uses_left。provided=True 表示显式传入（含 null 清除覆盖）。"""
    if 'uses_left' not in body:
        return None, False
    raw = body.get('uses_left')
    if raw is None or raw == '':
        return None, True
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError('使用次数无效')
    if val < 0:
        raise ValueError('使用次数不能为负')
    return val, True


@adventurer_s1_bp.after_request
def _record_owner_action_after_player_mutation(response):
    """玩家端非 GET 成功请求视为手动操作，刷新不活跃判定；自动下本等 scheduler 不走此路径。"""
    direct_cookie_realm = getattr(
        request,
        's1_direct_assignment_cookie_realm',
        None,
    )
    if direct_cookie_realm is not None:
        from plugins.adventurer_s1.services.s1_realm_gateway_service import (
            S1RealmGatewayService,
        )
        S1RealmGatewayService.attach_assignment_cookie(
            response,
            user_id=int(request.authenticated_user_id),
            realm_code=direct_cookie_realm,
        )
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return response
    if getattr(request, 's1_gateway_routed', False):
        return response
    if getattr(request, 's1_gateway_admin_request', False):
        return response
    if '/admin/' in (request.path or ''):
        return response
    if response.status_code >= 400:
        return response
    user_id = getattr(request, 'authenticated_user_id', None)
    if not user_id:
        return response
    try:
        from db_repository import db_manager
        AdventurerS1Service.record_owner_action_for_user(user_id)
        db_manager.commit()
    except Exception:
        # 不活跃打点失败不得影响玩家正常操作
        from db_repository import db_manager
        db_manager.rollback()
    return response


# ── 健康检查 ──────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/info', methods=['GET'])
def info():
    """插件健康检查：确认 S1 蓝图已挂载。"""
    return success({'plugin': 'adventurer_s1', 'status': 'ok'})
























# ── 静态环境 NPC ────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/ambient-npcs/current', methods=['GET'])
@login_required
def get_current_ambient_npc():
    """按玩家、界面与时间窗读取当前偶遇；纯读，不创建关系或回执。"""
    from plugins.adventurer_s1.services.s1_ambient_npc_service import (
        S1AmbientNpcService,
    )

    data, err = S1AmbientNpcService.get_current(
        request.current_user.id,
        request.args.get('surface') or '',
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/ambient-npcs/<string:encounter_id>/actions/<string:action_key>/items',
    methods=['GET'],
)
@login_required
def list_ambient_npc_action_items(encounter_id: str, action_key: str):
    """只返回当前动作可接受的随身/仓库物品白名单投影。"""
    from plugins.adventurer_s1.services.s1_ambient_npc_service import (
        S1AmbientNpcService,
    )

    data, err = S1AmbientNpcService.list_action_items(
        request.current_user.id,
        request.args.get('surface') or '',
        encounter_id,
        action_key,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/ambient-npcs/<string:encounter_id>/actions/<string:action_key>',
    methods=['POST'],
)
@login_required
@generic_anti_cheat_check('s1_ambient_npc')
def interact_with_ambient_npc(encounter_id: str, action_key: str):
    """执行模板动作；同请求重放，另一请求的动作回执按结构化冲突返回。"""
    from plugins.adventurer_s1.services.s1_ambient_npc_service import (
        AMBIENT_NPC_TERMINAL,
        AmbientNpcInteractionError,
        S1AmbientNpcService,
    )

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求体必须是 JSON 对象')
    action_input = body.get('action_input')
    if action_input is None:
        # 兼容首版 item_ref 形状；后续 Handler 统一从 action_input 取参数。
        action_input = {'item_ref': body.get('item_ref')} if 'item_ref' in body else {}
    if not isinstance(action_input, dict):
        return error('互动参数必须是对象')
    surface_key = body.get('surface') or ''
    request_id = (
        request.headers.get('Idempotency-Key')
        or body.get('request_id')
        or ''
    )

    def _bounded_public_identity(value, maximum: int) -> str:
        return value if isinstance(value, str) and len(value) <= maximum else ''

    data, err = S1AmbientNpcService.interact(
        request.current_user.id,
        surface_key=surface_key,
        encounter_id=encounter_id,
        action_key=action_key,
        request_id=request_id,
        action_input=action_input,
    )
    if err:
        if isinstance(err, AmbientNpcInteractionError):
            return error(
                err.message,
                code=err.http_status,
                data=err.to_public_data(),
            )
        return error(
            err,
            data={
                'error_code': 'ambient_npc_interaction_rejected',
                'retry_disposition': AMBIENT_NPC_TERMINAL,
                'request_id': _bounded_public_identity(request_id, 64),
                'encounter_id': _bounded_public_identity(encounter_id, 128),
                'action_key': _bounded_public_identity(action_key, 64),
                'surface': _bounded_public_identity(surface_key, 128),
            },
        )
    return success(data)


# ── 批量初始化 ──────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/init', methods=['GET'])
@login_required
def init_data():
    """批量返回酒馆首页所需数据，将 8+ 个并发请求合并为 1 个。"""
    from plugins.adventurer_s1.services.adventurer_s1_home_service import AdventurerS1HomeService

    data, err = AdventurerS1HomeService.get_init_data(
        user_id=request.current_user.id,
        is_admin=bool(getattr(request.current_user, 'is_admin', False)),
    )
    if err:
        return error(err)
    return _conditional_get_success(data)






@adventurer_s1_bp.route('/home-badges', methods=['GET'])
@login_required
def home_badges():
    """建筑分区入口角标（任务可接数、拍卖进行中拍品数），替代频繁拉完整任务板。"""
    from plugins.adventurer_s1.services.adventurer_s1_home_service import AdventurerS1HomeService

    data, err = AdventurerS1HomeService.get_badge_data(user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/home-badges/tasks', methods=['GET'])
@login_required
def home_task_badges():
    """仅任务栏可接数角标（不含拍卖行）。"""
    from plugins.adventurer_s1.services.adventurer_s1_home_service import AdventurerS1HomeService

    data, err = AdventurerS1HomeService.get_task_badge_data(user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tasks/board', methods=['GET'])
@login_required
def task_board_data():
    """返回任务列表 + 活跃任务，供任务栏面板一次性使用。"""
    adventurer_id = request.args.get('adventurer_id', type=int)
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.get_player_task_board_data(
        request.current_user.id, adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/materials/my', methods=['GET'])
@login_required
def my_materials():
    """当前玩家名下所有冒险者的材料汇总，供宿舍/建筑捐献统一使用。"""
    from plugins.adventurer_s1.services.adventurer_s1_material_service import (
        S1MaterialService,
    )
    materials, err = S1MaterialService.get_owner_totals(request.current_user.id)
    if err:
        return error(err)
    return success({
        'materials': materials,
        'meta': S1MaterialService.get_material_meta(),
    })


# ── 个人页支持投票 ────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/support-vote/status', methods=['GET'])
@login_required
def support_vote_status():
    """个人页支持投票状态；vote_count 为 0 时返回 None，前端不展示数量。"""
    from plugins.adventurer_s1.services.adventurer_s1_support_vote_service import (
        AdventurerS1SupportVoteService,
    )

    data, err = AdventurerS1SupportVoteService.get_status(
        request.current_user.id,
        is_admin=bool(getattr(request.current_user, 'is_admin', False)),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/support-vote', methods=['POST'])
@login_required
def cast_support_vote():
    """玩家支持投票：要求名下有存活自建冒险者，且每个账号最多一票。"""
    from plugins.adventurer_s1.services.adventurer_s1_support_vote_service import (
        AdventurerS1SupportVoteService,
    )

    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1SupportVoteService.cast_vote(
        request.current_user.id,
        option_key=body.get('option_key') or '',
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/support-vote/stats', methods=['GET'])
@login_required
@admin_required
def admin_support_vote_stats():
    """管理员后台统计支持投票；无投票时 vote_count 为 None。"""
    from plugins.adventurer_s1.services.adventurer_s1_support_vote_service import (
        AdventurerS1SupportVoteService,
    )

    data, err = AdventurerS1SupportVoteService.get_admin_stats()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/export/all', methods=['GET'])
@login_required
@admin_primary_required
def admin_export_all_player_data():
    """生成 S1 玩家业务数据 zip 到服务器 backups 目录，不要求二级验证。"""
    from plugins.adventurer_s1.services.s1_export_service import S1ExportService

    try:
        archive = S1ExportService.write_zip_archive()
    except Exception:
        current_app.logger.exception('S1 player data export failed')
        return error('导出酒馆数据失败，请稍后重试', 500)
    return success(archive, '酒馆数据已生成，请从服务器 backups 目录下载')


@adventurer_s1_bp.route('/admin/export/files', methods=['GET'])
@login_required
@admin_primary_required
def admin_list_export_archives():
    """列出已生成的 S1 归档，不要求管理员二级验证。"""
    from plugins.adventurer_s1.services.s1_export_service import S1ExportService

    try:
        files = S1ExportService.list_archives()
    except Exception:
        current_app.logger.exception('S1 player export listing failed')
        return error('读取酒馆导出文件失败，请稍后重试', 500)
    return success({
        'directory': S1ExportService.EXPORT_DIR_NAME,
        'files': files,
    })


@adventurer_s1_bp.route('/admin/support-vote', methods=['POST'])
@login_required
@admin_required
def admin_set_support_vote():
    """管理员新增/更新个人页投票配置；发布新投票会清空旧投票记录。"""
    from plugins.adventurer_s1.services.adventurer_s1_support_vote_service import (
        AdventurerS1SupportVoteService,
    )

    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1SupportVoteService.admin_set_current_vote(
        body,
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


# ── 悬赏布告栏（委托任务） ────────────────────────────────────────────────

@adventurer_s1_bp.route('/commissions', methods=['GET'])
@login_required
def commission_board():
    """悬赏布告栏看板：各档 open 委托 + 我的进行中委托 + 我的贡献余额。"""
    from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
    adventurer_id = request.args.get('adventurer_id', type=int)
    if adventurer_id is None:
        adventurer_id, _ = AdventurerS1Service.resolve_active_adventurer_id(
            request.current_user.id,
        )
    data, err = S1CommissionService.get_board(
        adventurer_id, user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/commissions/<int:commission_id>/accept', methods=['POST'])
@login_required
def commission_accept(commission_id):
    """接取委托（扣对应贡献度）。body: {adventurer_id?: int}"""
    from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if adventurer_id is None:
        adventurer_id, err = AdventurerS1Service.resolve_active_adventurer_id(
            request.current_user.id,
        )
        if err:
            return error(err)
    else:
        try:
            adventurer_id = int(adventurer_id)
        except (TypeError, ValueError):
            return error('adventurer_id 不合法')
    data, err = S1CommissionService.accept(
        commission_id, adventurer_id, user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/commissions/<int:commission_id>/claim', methods=['POST'])
@login_required
def commission_claim(commission_id):
    """领取已完成委托的奖励（金币/物品，仅接取本人）。"""
    from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
    data, err = S1CommissionService.claim(
        commission_id, user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/commissions/<int:commission_id>/cancel', methods=['POST'])
@login_required
def commission_cancel(commission_id):
    """取消已接取的委托（全额退还贡献度，重置进度）。"""
    from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
    data, err = S1CommissionService.cancel(
        commission_id, user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/contribution/reconcile', methods=['POST'])
@login_required
@admin_required
def admin_contribution_reconcile():
    """一键校准：把每个冒险者贡献度精确对账到正确值（累计=建筑贡献榜、可消耗=应得−净消费，双向、幂等）。"""
    from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
    data, err = S1ContributionService.reconcile_all(
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/level-cap-exp-reset/preview', methods=['GET'])
@login_required
@admin_required
def admin_level_cap_exp_reset_preview():
    """预览当前等级上限冒险者的溢出经验清零影响。"""
    from plugins.adventurer_s1.services.s1_level_cap_service import S1LevelCapService

    data, err = S1LevelCapService.preview_cap_exp_reset()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/level-cap-exp-reset', methods=['POST'])
@login_required
@admin_required
def admin_level_cap_exp_reset():
    """把当前等级上限及以上冒险者的当前等级经验清零，防止开放上限时连跳。"""
    from plugins.adventurer_s1.services.s1_level_cap_service import S1LevelCapService

    data, err = S1LevelCapService.reset_cap_exp(admin_user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data, message=f'已清零 {data.get("affected_count", 0)} 名冒险者的封顶溢出经验')


@adventurer_s1_bp.route('/admin/level-cap/open-15', methods=['POST'])
@login_required
@admin_required
def admin_level_cap_open_15():
    """热更新 S1 冒险者等级上限到 15。"""
    from plugins.adventurer_s1.services.s1_level_cap_service import S1LevelCapService

    data, err = S1LevelCapService.open_level_cap_to_15(admin_user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data, message=f'已开放等级上限到 Lv.{data.get("max_level", 15)}')


@adventurer_s1_bp.route('/admin/dungeon-rename/preview', methods=['GET'])
@login_required
@admin_required
def admin_dungeon_rename_preview():
    """预览高阶常规副本改名迁移影响，不写库。"""
    from plugins.adventurer_s1.services.s1_dungeon_rename_service import S1DungeonRenameService

    data, err = S1DungeonRenameService.preview()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/dungeon-rename', methods=['POST'])
@login_required
@admin_required
def admin_dungeon_rename_execute():
    """执行高阶常规副本改名迁移；必须显式 confirm=true。"""
    body = request.get_json(silent=True) or {}
    if body.get('confirm') is not True:
        return error('请先预览并确认后再执行迁移')
    from plugins.adventurer_s1.services.s1_dungeon_rename_service import S1DungeonRenameService

    data, err = S1DungeonRenameService.execute(admin_user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data, message=f'副本名迁移完成，共替换 {data.get("total_replacements", 0)} 处')


@adventurer_s1_bp.route('/admin/commissions/refill', methods=['POST'])
@login_required
@admin_required
def admin_commission_refill():
    """手动触发悬赏布告栏补满（便于测试 / 首次铺满）。"""
    from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
    if not S1CommissionService.enqueue_refill():
        return error('委托补满任务入队失败，请稍后重试')
    return success({'queued': True}, message='委托补满任务已入队')


@adventurer_s1_bp.route('/admin/commissions/refresh', methods=['POST'])
@login_required
@admin_required
def admin_commission_refresh():
    """强制刷新：删除所有 open 委托并重新生成一批。"""
    from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
    if not S1CommissionService.enqueue_refill(force_refresh=True):
        return error('委托刷新任务入队失败，请稍后重试')
    return success({'queued': True}, message='委托刷新任务已入队')


@adventurer_s1_bp.route('/admin/commissions/<int:commission_id>/refresh', methods=['POST'])
@login_required
@admin_required
def admin_commission_refresh_one(commission_id):
    """精准刷新单个委托：删除该 open 委托并生成 1 个同档新委托替换。"""
    from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
    data, err = S1CommissionService.enqueue_refresh_one(commission_id)
    if err:
        return error(err)
    return success(data, message='单条委托刷新任务已入队')


# ── 星灯礼堂（纪念仪式 + 独立酒馆活动） ────────────────────────────────────

@adventurer_s1_bp.route('/ceremony-hall', methods=['GET'])
@login_required
def ceremony_hall_current():
    """当前玩家的礼堂资料、生日资格与年度纪念礼帖。"""
    from plugins.adventurer_s1.services.s1_ceremony_hall_service import (
        S1CeremonyHallService,
    )
    from plugins.adventurer_s1.services.s1_realm_runtime_proxy_service import (
        RUNTIME_ACCESS_MODE_SPECTATOR,
    )

    is_spectator = bool(
        getattr(request, 's1_gateway_is_visitor', False)
        or getattr(request, 's1_runtime_access_mode', None)
        == RUNTIME_ACCESS_MODE_SPECTATOR
    )
    if is_spectator:
        display_name = (
            getattr(request.current_user, 'nickname', None)
            or getattr(request.current_user, 'username', None)
        )
        data, err = S1CeremonyHallService.get_spectator_state(display_name)
    else:
        data, err = S1CeremonyHallService.get_state(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/ceremony-hall/birthday', methods=['PUT'])
@login_required
def ceremony_hall_set_birthday():
    """登记月/日；本年度礼帖落印后冻结到下一自然年。"""
    from plugins.adventurer_s1.services.s1_ceremony_hall_service import (
        S1CeremonyHallService,
    )
    body = request.get_json(silent=True) or {}
    data, err = S1CeremonyHallService.set_birthday(
        request.current_user.id,
        body.get('month'),
        body.get('day'),
    )
    if err:
        return error(err)
    return success(data, message='生日已经写入星灯礼簿')


@adventurer_s1_bp.route('/ceremony-hall/birthday/attend', methods=['POST'])
@login_required
def ceremony_hall_attend_birthday():
    """生日当天冻结同席名单、宴席与祝词，并幂等落印当年礼帖。"""
    from plugins.adventurer_s1.services.s1_ceremony_hall_service import (
        S1CeremonyHallService,
    )
    body = request.get_json(silent=True) or {}
    data, err = S1CeremonyHallService.attend_birthday(
        request.current_user.id,
        body.get('wish_key'),
        body.get('visitors_open', False),
        feast_key=body.get('feast_key'),
    )
    if err:
        return error(err)
    return success(data, message='同席者已经落座，今夜星灯为你点亮')


@adventurer_s1_bp.route('/ceremony-hall/birthday/gift/claim', methods=['POST'])
@login_required
def ceremony_hall_claim_birthday_gift():
    """领取本年度礼帖对应的酒馆生日赠礼；重复请求只回放同一份回执。"""
    from plugins.adventurer_s1.services.s1_ceremony_hall_service import (
        S1CeremonyHallService,
    )
    data, err = S1CeremonyHallService.claim_birthday_gift(
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='掌柜把生日蛋糕郑重交到了你的手中')


@adventurer_s1_bp.route('/ceremony-hall/birthday/open-table', methods=['PUT'])
@login_required
def ceremony_hall_set_open_table():
    """生日当夜由寿星主动翻动“欢迎串门”门牌。"""
    from plugins.adventurer_s1.services.s1_ceremony_hall_service import (
        S1CeremonyHallService,
    )
    body = request.get_json(silent=True) or {}
    visitors_open = body.get('visitors_open')
    data, err = S1CeremonyHallService.set_visitors_open(
        request.current_user.id,
        visitors_open,
    )
    if err:
        return error(err)
    message = '生日桌已经向今夜的旅人敞开' if visitors_open else '生日桌已经翻回请勿打扰'
    return success(data, message=message)


@adventurer_s1_bp.route('/ceremony-hall/birthday/open-tables', methods=['GET'])
@login_required
def ceremony_hall_open_tables():
    """分页列出今夜由寿星主动开放的生日桌。"""
    from plugins.adventurer_s1.services.s1_ceremony_hall_service import (
        S1CeremonyHallService,
    )
    data, err = S1CeremonyHallService.list_open_tables(
        request.current_user.id,
        page=request.args.get('page', type=int),
        per_page=request.args.get('per_page', type=int),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/ceremony-hall/birthday/open-tables/<string:visit_token>/visits',
    methods=['GET'],
)
@login_required
def ceremony_hall_table_visits(visit_token):
    """分页读取开放桌互动；寿星可在门牌收起后继续查看自己的留影。"""
    from plugins.adventurer_s1.services.s1_ceremony_hall_service import (
        S1CeremonyHallService,
    )
    data, err = S1CeremonyHallService.list_table_visits(
        request.current_user.id,
        visit_token,
        page=request.args.get('page', type=int),
        per_page=request.args.get('per_page', type=int),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/ceremony-hall/birthday/open-tables/<string:visit_token>/visits',
    methods=['POST'],
)
@login_required
def ceremony_hall_visit_table(visit_token):
    """留下纯文字串门动作与可选祝福，不触发任何数值结算。"""
    from plugins.adventurer_s1.services.s1_ceremony_hall_service import (
        S1CeremonyHallService,
    )
    body = request.get_json(silent=True) or {}
    data, err = S1CeremonyHallService.visit_table(
        request.current_user.id,
        visit_token,
        body.get('action_key'),
        body.get('message'),
        body.get('request_token'),
    )
    if err:
        return error(err)
    return success(data, message='这一小段串门时光已经留在长桌边')


# ── 命运牌桌活动（独立于赌坊） ─────────────────────────────────────────────

@adventurer_s1_bp.route('/activities/current', methods=['GET'])
@login_required
def tavern_activity_current():
    """当前/最近一期酒馆活动，以及按阶段分页的比赛日志。"""
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    return success(S1TavernActivityService.get_current(
        request.current_user.id,
        admin=bool(getattr(request.current_user, 'is_admin', False)),
        stage_number=request.args.get('stage', type=int),
        page=request.args.get('page', type=int),
        per_page=request.args.get('per_page', type=int),
    ))


@adventurer_s1_bp.route('/activities/<int:activity_id>/register', methods=['POST'])
@login_required
def tavern_activity_register(activity_id):
    """活动总报名；抽签前不占用冒险者。"""
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if adventurer_id is None:
        adventurer_id, resolve_err = AdventurerS1Service.resolve_active_adventurer_id(
            request.current_user.id,
        )
        if resolve_err:
            return error(resolve_err)
    try:
        adventurer_id = int(adventurer_id)
    except (TypeError, ValueError):
        return error('adventurer_id 必须为整数')
    data, err = S1TavernActivityService.register(
        activity_id, adventurer_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='活动报名成功')


@adventurer_s1_bp.route('/activities/<int:activity_id>/register', methods=['DELETE'])
@login_required
def tavern_activity_withdraw(activity_id):
    """抽签前取消活动报名。"""
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if adventurer_id is None:
        adventurer_id, resolve_err = AdventurerS1Service.resolve_active_adventurer_id(
            request.current_user.id,
        )
        if resolve_err:
            return error(resolve_err)
    try:
        adventurer_id = int(adventurer_id)
    except (TypeError, ValueError):
        return error('adventurer_id 必须为整数')
    data, err = S1TavernActivityService.withdraw(
        activity_id, adventurer_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已取消活动报名')


@adventurer_s1_bp.route('/activity-matches/<int:match_id>/check-in', methods=['POST'])
@login_required
def tavern_activity_check_in(match_id):
    """进入抽签确定的编号组，即完成本场签到。"""
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if adventurer_id is None:
        adventurer_id, resolve_err = AdventurerS1Service.resolve_active_adventurer_id(
            request.current_user.id,
        )
        if resolve_err:
            return error(resolve_err)
    try:
        adventurer_id = int(adventurer_id)
    except (TypeError, ValueError):
        return error('adventurer_id 必须为整数')
    data, err = S1TavernActivityService.check_in(
        match_id, adventurer_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已进入编号组，等待开赛')


@adventurer_s1_bp.route('/admin/activities', methods=['POST'])
@login_required
@admin_required
def admin_tavern_activity_create():
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    data, err = S1TavernActivityService.create_activity(
        request.get_json(silent=True) or {},
        request.current_user.id,
    )
    if err:
        return error(err)
    return created(data, message='酒馆活动草稿已创建')


@adventurer_s1_bp.route('/admin/activities/<int:activity_id>/open-registration', methods=['POST'])
@login_required
@admin_required
def admin_tavern_activity_open_registration(activity_id):
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    body = request.get_json(silent=True) or {}
    data, err = S1TavernActivityService.open_registration(
        activity_id,
        registration_close_at=body.get('registration_close_at'),
    )
    if err:
        return error(err)
    return success(data, message='活动报名已开放')


@adventurer_s1_bp.route('/admin/activities/<int:activity_id>/draw', methods=['POST'])
@login_required
@admin_required
def admin_tavern_activity_draw(activity_id):
    """关闭总报名并一次性固定首轮所有编号组与错峰时间。"""
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    body = request.get_json(silent=True) or {}
    data, err = S1TavernActivityService.draw_first_stage(
        activity_id,
        first_stage_start_at=body.get('first_stage_start_at'),
    )
    if err:
        return error(err)
    return success(data, message='首轮抽签完成，分组与对手已经固定')


@adventurer_s1_bp.route('/admin/activities/<int:activity_id>/cancel', methods=['POST'])
@login_required
@admin_required
def admin_tavern_activity_cancel(activity_id):
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    data, err = S1TavernActivityService.cancel_activity(activity_id)
    if err:
        return error(err)
    return success(data, message='活动已取消，已签到冒险者已释放')


@adventurer_s1_bp.route('/admin/activity-matches/<int:match_id>/retry', methods=['POST'])
@login_required
@admin_required
def admin_tavern_activity_retry(match_id):
    from plugins.adventurer_s1.services.s1_tavern_activity_service import (
        S1TavernActivityService,
    )
    data, err = S1TavernActivityService.retry_match(match_id)
    if err:
        return error(err)
    return success(data, message='比赛剧情重试任务已入队')


@adventurer_s1_bp.route('/admin/world-outline/candidates', methods=['GET'])
@login_required
@admin_required
def admin_world_outline_candidates():
    """预览探险 AI 输出的世界事实 / DM 便签候选。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    page, per_page = get_pagination_params(default_per_page=20, max_per_page=100)
    data = S1WorldOutlineService.list_candidates(
        page=page,
        per_page=per_page,
        status=(request.args.get('status') or '').strip() or None,
        target_dungeon=(request.args.get('target_dungeon') or '').strip() or None,
        issue_code=(request.args.get('issue_code') or '').strip() or None,
    )
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/dm-handoff', methods=['GET'])
@login_required
@admin_required
def admin_world_outline_dm_handoff():
    """查看当前按地牢维护的 DM 接力便签。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    page, per_page = get_pagination_params(default_per_page=20)
    return success(S1WorldOutlineService.list_handoff_states(page=page, per_page=per_page))


@adventurer_s1_bp.route('/admin/world-outline/intel', methods=['GET'])
@login_required
@admin_read_required
def admin_world_outline_intel_list():
    """管理员查看公共世界情报库。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    page, per_page = get_pagination_params(default_per_page=20, max_per_page=100)
    data = S1WorldOutlineService.list_world_intel(
        page=page,
        per_page=per_page,
        status=(request.args.get('status') or '').strip() or None,
        thread_type=(request.args.get('thread_type') or '').strip() or None,
        keyword=(request.args.get('keyword') or '').strip() or None,
    )
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/intel', methods=['POST'])
@login_required
@admin_required
def admin_world_outline_intel_create():
    """管理员新建公共世界情报。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    body = request.get_json(silent=True) or {}
    data, err = S1WorldOutlineService.upsert_world_intel(body)
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/admin/world-outline/intel/generate', methods=['POST'])
@login_required
@admin_required
def admin_world_outline_intel_generate():
    """根据管理员概述生成一条未持久化的公共世界情报草稿。"""
    from plugins.adventurer_s1.services.s1_world_intel_gen_ai_service import (
        generate_world_intel_draft,
    )

    data, err = generate_world_intel_draft(request.get_json(silent=True) or {})
    if err:
        status_code = 502 if err in {
            '世界情报生成失败，请稍后重试',
            'AI 未返回有效的世界情报 JSON',
        } else 400
        return error(err, status_code)
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/intel/<int:thread_id>', methods=['PUT'])
@login_required
@admin_required
def admin_world_outline_intel_update(thread_id):
    """管理员编辑公共世界情报。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    body = request.get_json(silent=True) or {}
    data, err = S1WorldOutlineService.upsert_world_intel(body, thread_id=thread_id)
    if err:
        return error(err, 404 if err == '情报不存在' else 400)
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/intel/<int:thread_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_world_outline_intel_delete(thread_id):
    """管理员删除公共世界情报。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    data, err = S1WorldOutlineService.delete_world_intel(thread_id)
    if err:
        return error(err, 404)
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/intel/preview', methods=['GET'])
@login_required
@admin_required
def admin_world_outline_intel_preview():
    """管理员预览某副本会注入的公共世界情报。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    data = S1WorldOutlineService.preview_world_intel_injection(
        request.args.get('target_dungeon') or request.args.get('dungeon') or '',
        limit=request.args.get('limit', type=int),
        context_text=request.args.get('context') or request.args.get('keyword') or '',
    )
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/meta', methods=['GET'])
@login_required
@admin_read_required
def admin_world_campaign_meta():
    """共同战役表单枚举与分页势力选项。"""
    from plugins.adventurer_s1.services.s1_world_campaign_service import (
        S1WorldCampaignService,
    )

    page, per_page = get_pagination_params(default_per_page=20, max_per_page=100)
    return success(S1WorldCampaignService.get_meta(
        page=page,
        per_page=per_page,
        faction_status=(request.args.get('faction_status') or 'active').strip(),
        keyword=(request.args.get('keyword') or '').strip() or None,
    ))


@adventurer_s1_bp.route('/admin/world-outline/campaigns/presets/<preset_key>', methods=['GET'])
@login_required
@admin_read_required
def admin_world_campaign_preset(preset_key):
    from plugins.adventurer_s1.services.s1_world_campaign_service import S1WorldCampaignService
    preset = S1WorldCampaignService.get_preset(preset_key)
    if preset is None:
        return error('战役方案不存在', 404)
    return success(preset)


@adventurer_s1_bp.route('/admin/world-outline/campaigns', methods=['GET'])
@login_required
@admin_read_required
def admin_world_campaign_list():
    """分页查看共同战役 spine 与所有并行前线。"""
    from plugins.adventurer_s1.services.s1_world_campaign_service import (
        S1WorldCampaignService,
    )

    page, per_page = get_pagination_params(default_per_page=20, max_per_page=100)
    return success(S1WorldCampaignService.list_campaigns(
        page=page,
        per_page=per_page,
        status=(request.args.get('status') or '').strip() or None,
        keyword=(request.args.get('keyword') or '').strip() or None,
    ))


@adventurer_s1_bp.route('/campaign-hall', methods=['GET'])
@login_required
def campaign_hall_board():
    from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
    page, per_page = get_pagination_params(default_per_page=12, max_per_page=30)
    return success(S1CampaignDispatchService.board(page, per_page))


@adventurer_s1_bp.route('/campaign-hall/<int:dispatch_id>/reports', methods=['GET'])
@login_required
def campaign_hall_reports(dispatch_id):
    from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
    page, per_page = get_pagination_params(default_per_page=12, max_per_page=30)
    data, err = S1CampaignDispatchService.history(dispatch_id, page, per_page)
    return error(err, 404) if err else success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/dispatch', methods=['POST'])
@login_required
@admin_required
def admin_enable_campaign_dispatch(root_id):
    from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
    data, err = S1CampaignDispatchService.enable(root_id)
    return error(err) if err else success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/dispatch/retry', methods=['POST'])
@login_required
@admin_required
def admin_retry_campaign_publication(root_id):
    from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
    data, err = S1CampaignDispatchService.retry_publication(root_id)
    return error(err) if err else success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/progress', methods=['GET'])
@login_required
@admin_read_required
def admin_campaign_progress(root_id):
    from services.admin_database_profile_service import AdminDatabaseProfileService
    data, err = AdminDatabaseProfileService.get_s1_campaign(root_id)
    return error(err, 400) if err else success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/transitions/preview', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/progress-review/preview', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/advance-mode/preview', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/challenges/preview', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/revelations/preview', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/front-recovery/preview', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/ending/preview', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/objectives/preview', methods=['POST'])
@login_required
@admin_required
def admin_campaign_stage_preview(root_id):
    from plugins.adventurer_s1.services.s1_campaign_stage_service import S1CampaignStageService
    kind = {'transitions': 'transition', 'progress-review': 'progress_review',
            'advance-mode': 'advance_mode', 'challenges': 'challenge_update', 'revelations': 'revelation_update',
            'front-recovery': 'front_recovery', 'ending': 'ending_summary',
            'objectives': 'objectives_update'}[request.path.split('/')[-2]]
    data, err = S1CampaignStageService.preview(root_id, kind, request.get_json(silent=True),
                                               request.current_user.id)
    return error(err, 400) if err else success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/transitions/execute', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/progress-review/execute', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/advance-mode/execute', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/challenges/execute', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/revelations/execute', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/front-recovery/execute', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/ending/execute', methods=['POST'])
@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/objectives/execute', methods=['POST'])
@login_required
@admin_required
def admin_campaign_stage_execute(root_id):
    from plugins.adventurer_s1.services.s1_campaign_stage_service import S1CampaignStageService
    kind = {'transitions': 'transition', 'progress-review': 'progress_review',
            'advance-mode': 'advance_mode', 'challenges': 'challenge_update', 'revelations': 'revelation_update',
            'front-recovery': 'front_recovery', 'ending': 'ending_summary',
            'objectives': 'objectives_update'}[request.path.split('/')[-2]]
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求必须为对象', 400)
    data, err = S1CampaignStageService.execute(root_id, kind, body.get('preview_token'),
                                               request.current_user.id)
    return error(err, 409) if err else success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/continuations', methods=['GET'])
@login_required
@admin_read_required
def admin_campaign_continuation_status(root_id):
    from plugins.adventurer_s1.services.s1_campaign_continuation_service import S1CampaignContinuationService
    data, err = S1CampaignContinuationService.status(root_id)
    return error(err, 400) if err else success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/<int:root_id>/continuations/<action>', methods=['POST'])
@login_required
@admin_required
def admin_campaign_continuation_action(root_id, action):
    from plugins.adventurer_s1.services.s1_campaign_continuation_service import S1CampaignContinuationService as Service
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求必须为对象', 400)
    actor_id = request.current_user.id
    if action == 'prepare':
        data, err = Service.prepare(root_id, body, actor_id)
    elif action == 'preview':
        data, err = Service.preview(root_id, actor_id)
    elif action == 'execute':
        data, err = Service.execute(root_id, body.get('preview_token'), actor_id)
    else:
        return error('不支持的衔接操作', 404)
    return error(err, 409) if err else success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns/preview', methods=['POST'])
@login_required
@admin_required
def admin_world_campaign_preview():
    """冻结共同战役启动合同与精确势力档案版本。"""
    from plugins.adventurer_s1.services.s1_world_campaign_service import (
        S1WorldCampaignService,
    )

    data, err, status_code = S1WorldCampaignService.preview_create(
        request.get_json(silent=True) or {},
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err, status_code)
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/campaigns', methods=['POST'])
@login_required
@admin_required
def admin_world_campaign_create():
    """按冻结预览原子创建 spine、fronts 与精确势力关联。"""
    from plugins.adventurer_s1.services.s1_world_campaign_service import (
        S1WorldCampaignService,
    )

    body = request.get_json(silent=True) or {}
    data, err, status_code = S1WorldCampaignService.create_from_preview(
        body.get('preview_token'),
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err, status_code)
    return created(data) if status_code == 201 else success(data)


@adventurer_s1_bp.route(
    '/admin/world-outline/campaigns/threads/<int:thread_id>/agenda',
    methods=['PATCH'],
)
@adventurer_s1_bp.route(
    '/admin/world-outline/campaigns/<int:thread_id>/agenda',
    methods=['PATCH'],
)
@login_required
@admin_required
def admin_world_campaign_patch_agenda(thread_id):
    """仅按 revision 修改共同战役节点的运营 agenda。"""
    from plugins.adventurer_s1.services.s1_world_campaign_service import (
        S1WorldCampaignService,
    )

    data, err, status_code = S1WorldCampaignService.patch_agenda(
        thread_id,
        request.get_json(silent=True) or {},
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err, status_code)
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/candidates/<int:candidate_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_world_outline_delete_candidate(candidate_id):
    """删除单条世界事实 / DM 便签候选。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    data, err = S1WorldOutlineService.delete_candidate(candidate_id)
    if err:
        return error(err, 404)
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/dm-handoff/<int:state_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_world_outline_delete_handoff(state_id):
    """清空某个副本当前 DM 接力便签。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    data, err = S1WorldOutlineService.delete_handoff_state(state_id)
    if err:
        return error(err, 404)
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/maintain', methods=['POST'])
@login_required
@admin_required
def admin_world_outline_maintain():
    """手动提交一次异步大纲维护任务。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService

    ok = S1WorldOutlineService.submit_maintenance_task()
    return success({'submitted': bool(ok)})


@adventurer_s1_bp.route('/admin/history-story/repair-codenames/preview', methods=['GET'])
@login_required
@admin_required
def admin_history_story_repair_codenames_preview():
    """预览：传记残留代号可精确还原 / 仍残留的数量（dry-run，不写库）。"""
    from plugins.adventurer_s1.services.adventurer_s1_codename_repair_service import (
        AdventurerS1CodenameRepairService,
    )
    data, err = AdventurerS1CodenameRepairService.preview()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/history-story/repair-codenames', methods=['POST'])
@login_required
@admin_required
def admin_history_story_repair_codenames():
    """精确还原传记残留代号（先自动备份 history_story 原文，出问题可一键还原）。"""
    from plugins.adventurer_s1.services.adventurer_s1_codename_repair_service import (
        AdventurerS1CodenameRepairService,
    )
    data, err = AdventurerS1CodenameRepairService.repair(
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/history-story/repair-codenames/restore', methods=['POST'])
@login_required
@admin_required
def admin_history_story_restore_codenames():
    """从最近一次修复备份一键还原 history_story。"""
    from plugins.adventurer_s1.services.adventurer_s1_codename_repair_service import (
        AdventurerS1CodenameRepairService,
    )
    data, err = AdventurerS1CodenameRepairService.restore()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-tool-types/preview', methods=['GET'])
@login_required
@admin_required
def admin_repair_tool_types_preview():
    """预览：被误标 consumable 的可复用工具（地图/钥匙/典籍类）及受影响实例数（dry-run）。"""
    from plugins.adventurer_s1.services.s1_item_type_repair_service import (
        S1ItemTypeRepairService,
    )
    data, err = S1ItemTypeRepairService.preview()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-tool-types', methods=['POST'])
@login_required
@admin_required
def admin_repair_tool_types():
    """把误标 consumable 的可复用工具图鉴 + 实例 type 改为 document（先备份可还原）。"""
    from plugins.adventurer_s1.services.s1_item_type_repair_service import (
        S1ItemTypeRepairService,
    )
    data, err = S1ItemTypeRepairService.repair(
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-tool-types/restore', methods=['POST'])
@login_required
@admin_required
def admin_restore_tool_types():
    """从最近一次物品类型修复备份一键还原 type。"""
    from plugins.adventurer_s1.services.s1_item_type_repair_service import (
        S1ItemTypeRepairService,
    )
    data, err = S1ItemTypeRepairService.restore()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-uncommon-rarity/preview', methods=['GET'])
@login_required
@admin_required
def admin_repair_uncommon_rarity_preview():
    """预览：已入库 uncommon 稀有度条目与各表受影响行数（dry-run）。"""
    from plugins.adventurer_s1.services.s1_item_rarity_repair_service import (
        S1ItemRarityRepairService,
    )
    data, err = S1ItemRarityRepairService.preview()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-uncommon-rarity', methods=['POST'])
@login_required
@admin_required
def admin_repair_uncommon_rarity():
    """把已入库 uncommon 稀有度批量改为 rare（图鉴+实例+流水等，先备份可还原）。"""
    from plugins.adventurer_s1.services.s1_item_rarity_repair_service import (
        S1ItemRarityRepairService,
    )
    data, err = S1ItemRarityRepairService.repair(
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-uncommon-rarity/restore', methods=['POST'])
@login_required
@admin_required
def admin_restore_uncommon_rarity():
    """从最近一次 uncommon 稀有度修复备份一键还原。"""
    from plugins.adventurer_s1.services.s1_item_rarity_repair_service import (
        S1ItemRarityRepairService,
    )
    data, err = S1ItemRarityRepairService.restore()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/visible-names/repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_visible_name_repair_preview():
    """预览玩家可见内部标识污染、协议名特质和已确认漏发奖励。"""
    from plugins.adventurer_s1.services.s1_visible_name_repair_service import (
        S1VisibleNameRepairService,
    )
    data, err = S1VisibleNameRepairService.preview()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/visible-names/repair', methods=['POST'])
@login_required
@admin_required
def admin_visible_name_repair():
    """修复当前资产和协议名特质，并按结算收据补回漏发奖励。"""
    from plugins.adventurer_s1.services.s1_visible_name_repair_service import (
        S1VisibleNameRepairService,
    )
    data, err = S1VisibleNameRepairService.repair(
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/visible-names/repair/restore', methods=['POST'])
@login_required
@admin_required
def admin_visible_name_repair_restore():
    """从最近备份恢复名称与特质，并撤销仍可精确定位的补发。"""
    from plugins.adventurer_s1.services.s1_visible_name_repair_service import (
        S1VisibleNameRepairService,
    )
    data, err = S1VisibleNameRepairService.restore(
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-companion-evolution-stages/preview', methods=['GET'])
@login_required
@admin_required
def admin_repair_companion_evolution_stages_preview():
    """预览成功嬗变日志高于当前伙伴阶位的唯一匹配实例（dry-run）。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
        S1CompanionEvolutionStageRepairService,
    )

    data, err = S1CompanionEvolutionStageRepairService.preview()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/companion-evolution-integrity/preview', methods=['POST'])
@login_required
@admin_required
def admin_companion_evolution_integrity_preview():
    """分批续扫并统一预览嬗变费用、当前阶级与伙伴谱系。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_integrity_scan_service import (
        S1CompanionEvolutionIntegrityScanService,
    )

    data, err = S1CompanionEvolutionIntegrityScanService.preview_integrity(
        int(request.current_user.id),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-companion-evolution-stages', methods=['POST'])
@login_required
@admin_required
def admin_repair_companion_evolution_stages():
    """按成功嬗变日志下限恢复被历史穿脱链路归零的伙伴阶位。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
        S1CompanionEvolutionStageRepairService,
    )

    data, err = S1CompanionEvolutionStageRepairService.repair(
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/repair-companion-evolution-stages/restore', methods=['POST'])
@login_required
@admin_required
def admin_restore_companion_evolution_stages():
    """从最近一次伙伴嬗变阶位修复备份还原。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
        S1CompanionEvolutionStageRepairService,
    )

    data, err = S1CompanionEvolutionStageRepairService.restore()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/class-repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_class_repair_preview():
    """预览：指定冒险者当前职业 + 传记尾段，供管理员核对该补什么职业（dry-run）。"""
    adventurer_id = request.args.get('adventurer_id', type=int)
    if not adventurer_id:
        return error('缺少 adventurer_id')
    from plugins.adventurer_s1.services.s1_class_repair_service import (
        S1ClassRepairService,
    )
    data, err = S1ClassRepairService.preview(adventurer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/class-repair', methods=['POST'])
@login_required
@admin_required
def admin_class_repair():
    """给指定冒险者补 / 改职业（character_class），先备份原值可一键还原。"""
    payload = request.get_json(silent=True) or {}
    try:
        adventurer_id = int(payload.get('adventurer_id'))
    except (TypeError, ValueError):
        return error('缺少或非法 adventurer_id')
    new_class = payload.get('new_class')
    if not isinstance(new_class, str) or not new_class.strip():
        return error('缺少 new_class')
    from plugins.adventurer_s1.services.s1_class_repair_service import (
        S1ClassRepairService,
    )
    data, err = S1ClassRepairService.repair(
        adventurer_id, new_class,
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/class-repair/restore', methods=['POST'])
@login_required
@admin_required
def admin_class_repair_restore():
    """把所有被职业修复改过的冒险者职业回退到修复前原值，并清空备份。"""
    from plugins.adventurer_s1.services.s1_class_repair_service import (
        S1ClassRepairService,
    )
    data, err = S1ClassRepairService.restore()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/class-skill-scroll/preview', methods=['GET'])
@login_required
@admin_required
def admin_class_skill_scroll_preview():
    """预览：转职/进阶后漏发职业技能、且未申诉补发过的冒险者（dry-run）。"""
    adventurer_id = request.args.get('adventurer_id', type=int)
    from plugins.adventurer_s1.services.s1_class_skill_scroll_repair_service import (
        S1ClassSkillScrollRepairService,
    )
    data, err = S1ClassSkillScrollRepairService.preview(adventurer_id=adventurer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/class-skill-scroll/repair', methods=['POST'])
@login_required
@admin_required
def admin_class_skill_scroll_repair():
    """批量补发「职业通用技能卷轴」到仓库（先备份可一键撤销）。"""
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('adventurer_ids')
    adventurer_ids = None
    if isinstance(raw_ids, list) and raw_ids:
        try:
            adventurer_ids = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            return error('adventurer_ids 须为整数数组')
    from plugins.adventurer_s1.services.s1_class_skill_scroll_repair_service import (
        S1ClassSkillScrollRepairService,
    )
    data, err = S1ClassSkillScrollRepairService.repair(
        adventurer_ids=adventurer_ids,
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/class-skill-scroll/restore', methods=['POST'])
@login_required
@admin_required
def admin_class_skill_scroll_restore():
    """撤销本服务补发的职业通用技能卷轴，并清空备份。"""
    from plugins.adventurer_s1.services.s1_class_skill_scroll_repair_service import (
        S1ClassSkillScrollRepairService,
    )
    data, err = S1ClassSkillScrollRepairService.restore()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/placeholder-skill-recall/preview', methods=['GET'])
@login_required
@admin_required
def admin_placeholder_skill_recall_preview():
    """预览：持有占位「xxx入门」技能的冒险者（dry-run）。"""
    adventurer_id = request.args.get('adventurer_id', type=int)
    from plugins.adventurer_s1.services.s1_placeholder_skill_recall_service import (
        S1PlaceholderSkillRecallService,
    )
    data, err = S1PlaceholderSkillRecallService.preview(adventurer_id=adventurer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/world-outline/actors', methods=['GET'])
@login_required
@admin_required
def admin_world_actor_list():
    """查看自动演化的长期世界角色当前态。"""
    from plugins.adventurer_s1.services.s1_world_actor_service import list_world_actors

    page, per_page = get_pagination_params(default_per_page=20, max_per_page=100)
    return success(list_world_actors(
        page=page,
        per_page=per_page,
        status=(request.args.get('status') or '').strip() or None,
        keyword=(request.args.get('keyword') or '').strip() or None,
    ))


@adventurer_s1_bp.route('/admin/world-outline/actors/<int:actor_id>', methods=['GET'])
@login_required
@admin_required
def admin_world_actor_detail(actor_id):
    """查看角色关系、世界线与最近交集审计。"""
    from plugins.adventurer_s1.services.s1_world_actor_service import get_world_actor_detail

    data, err = get_world_actor_detail(actor_id)
    if err:
        return error(err, 404)
    return success(data)


@adventurer_s1_bp.route('/admin/special-dungeon-ticket-penalties/preview', methods=['GET'])
@login_required
@admin_required
def admin_special_dungeon_ticket_penalties_preview():
    """管理员：预览特殊副本未扣门票人次及精确追缴金额。"""
    from plugins.adventurer_s1.services.s1_special_dungeon_ticket_penalty_service import (
        S1SpecialDungeonTicketPenaltyService,
    )
    data, err = S1SpecialDungeonTicketPenaltyService.preview(
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/gambling-gold-reconciliation/preview', methods=['GET'])
@login_required
@admin_required
def admin_gambling_gold_reconciliation_preview():
    """管理员：按最终轮次与金币流水预览赌坊超额派彩。"""
    from plugins.adventurer_s1.services.s1_gambling_gold_reconciliation_service import (
        S1GamblingGoldReconciliationService,
    )

    data, err = S1GamblingGoldReconciliationService.preview(
        admin_user_id=request.current_user.id,
        adventurer_id=request.args.get('adventurer_id'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/gambling-gold-reconciliation/execute', methods=['POST'])
@login_required
@admin_required
def admin_gambling_gold_reconciliation_execute():
    """管理员：按不可变预览快照幂等追缴赌坊超额派彩。"""
    from plugins.adventurer_s1.services.s1_gambling_gold_reconciliation_service import (
        S1GamblingGoldReconciliationService,
    )

    body = request.get_json(silent=True) or {}
    data, err = S1GamblingGoldReconciliationService.execute(
        request.current_user.id,
        preview_token=str(body.get('preview_token') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message='赌坊金币对账校正完成')


@adventurer_s1_bp.route('/admin/special-dungeon-ticket-penalties/execute', methods=['POST'])
@login_required
@admin_required
def admin_special_dungeon_ticket_penalties_execute():
    """管理员：一键追缴未扣特殊副本门票对应金币，允许扣成负数。"""
    from plugins.adventurer_s1.services.s1_special_dungeon_ticket_penalty_service import (
        S1SpecialDungeonTicketPenaltyService,
    )
    body = request.get_json(silent=True) or {}
    data, err = S1SpecialDungeonTicketPenaltyService.execute(
        request.current_user.id,
        preview_token=str(body.get('preview_token') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message='特殊副本未扣门票金币追缴完成')


@adventurer_s1_bp.route('/admin/expedition-skill-repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_expedition_skill_repair_preview():
    """预览：结算 new_skills 已承诺、但技能子表中不存在的技能。"""
    expedition_log_id = request.args.get('expedition_log_id', type=int)
    adventurer_id = request.args.get('adventurer_id', type=int)
    from plugins.adventurer_s1.services.s1_expedition_skill_repair_service import (
        S1ExpeditionSkillRepairService,
    )
    data, err = S1ExpeditionSkillRepairService.preview(
        expedition_log_id=expedition_log_id,
        adventurer_id=adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/companion-evolution-fee-reclaims/preview', methods=['GET'])
@login_required
@admin_required
def admin_companion_evolution_fee_reclaims_preview():
    """预览唯一连续嬗变链中因阶位回退少付的费用。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
        S1CompanionEvolutionStageRepairService,
    )

    data, err = S1CompanionEvolutionStageRepairService.preview_fee_reclaims(
        int(request.current_user.id),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/companion-evolution-fee-reclaims', methods=['POST'])
@login_required
@admin_required
def admin_companion_evolution_fee_reclaims_execute():
    """按预览快照幂等扣回漏缴的伙伴嬗变费用。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
        S1CompanionEvolutionStageRepairService,
    )

    payload = request.get_json(silent=True) or {}
    data, err = S1CompanionEvolutionStageRepairService.execute_fee_reclaims(
        int(request.current_user.id),
        str(payload.get('preview_token') or ''),
        str(payload.get('note') or ''),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/companion-evolution-fee-reconciliation/preview', methods=['GET'])
@login_required
@admin_required
def admin_companion_evolution_fee_reconciliation_preview():
    """预览历史伙伴嬗变费回收的双向差额校准。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
        S1CompanionEvolutionStageRepairService,
    )

    data, err = S1CompanionEvolutionStageRepairService.preview_fee_reconciliation(
        int(request.current_user.id),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/items/companion-evolution-fee-reconciliation', methods=['POST'])
@login_required
@admin_required
def admin_companion_evolution_fee_reconciliation_execute():
    """按预览快照退回多扣金额并补扣仍欠的嬗变费差价。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
        S1CompanionEvolutionStageRepairService,
    )

    payload = request.get_json(silent=True) or {}
    data, err = S1CompanionEvolutionStageRepairService.execute_fee_reconciliation(
        int(request.current_user.id),
        str(payload.get('preview_token') or ''),
        str(payload.get('note') or ''),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/fate-settlement-repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_fate_settlement_repair_preview():
    """统一预览旧能力损失、最终试炼资格与伙伴替换连续性风险。"""
    from plugins.adventurer_s1.services.s1_fate_settlement_repair_service import (
        S1FateSettlementRepairService,
    )
    raw_expedition_log_id = request.args.get('expedition_log_id')
    expedition_log_id = None
    if raw_expedition_log_id not in (None, ''):
        try:
            expedition_log_id = int(raw_expedition_log_id)
        except (TypeError, ValueError):
            return error('expedition_log_id 必须是正整数')
        if expedition_log_id <= 0:
            return error('expedition_log_id 必须是正整数')
    raw_include_unreviewed = request.args.get('include_unreviewed')
    if raw_include_unreviewed not in (None, '', '0', '1'):
        return error('include_unreviewed 只允许 0 或 1')
    data, err = S1FateSettlementRepairService.preview(
        request.current_user.id,
        expedition_log_id=expedition_log_id,
        include_unreviewed=raw_include_unreviewed == '1',
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/items/companion-continuity-repair/review-evidence',
    methods=['POST'],
)
@login_required
@admin_required
def admin_companion_continuity_repair_review_evidence():
    """读取并冻结历史伙伴替换、后续时间线与当前实例证据。"""
    from plugins.adventurer_s1.services.s1_companion_historical_repair_service import (
        S1CompanionHistoricalRepairService,
    )
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('expedition_log_ids')
    if not isinstance(raw_ids, list):
        return error('expedition_log_ids 必须是正整数数组')
    try:
        expedition_log_ids = [int(item) for item in raw_ids]
    except (TypeError, ValueError):
        return error('expedition_log_ids 必须是正整数数组')
    if any(item <= 0 for item in expedition_log_ids):
        return error('expedition_log_ids 必须是正整数数组')
    data, err = S1CompanionHistoricalRepairService.load_review_evidence(
        request.current_user.id,
        expedition_log_ids,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/items/companion-continuity-repair/review-ai-preview',
    methods=['POST'],
)
@login_required
@admin_required
def admin_companion_continuity_repair_review_ai_preview():
    """调用配置的 S1 AI，生成不写库的伙伴谱系绑定预览。"""
    from plugins.adventurer_s1.services.s1_companion_historical_repair_service import (
        S1CompanionHistoricalRepairService,
    )
    payload = request.get_json(silent=True) or {}
    evidence_token = str(payload.get('evidence_token') or '').strip()
    if not evidence_token:
        return error('必须提供冻结证据令牌')
    data, err = S1CompanionHistoricalRepairService.ai_review_preview(
        request.current_user.id,
        evidence_token,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/items/companion-continuity-repair/review-preview',
    methods=['POST'],
)
@login_required
@admin_required
def admin_companion_continuity_repair_review_preview():
    """接收外部逐项复核，并通过同一合同生成谱系绑定预览。"""
    from plugins.adventurer_s1.services.s1_companion_historical_repair_service import (
        S1CompanionHistoricalRepairService,
    )
    payload = request.get_json(silent=True) or {}
    evidence_token = str(payload.get('evidence_token') or '').strip()
    if not evidence_token:
        return error('必须提供冻结证据令牌')
    data, err = S1CompanionHistoricalRepairService.submit_review(
        request.current_user.id,
        evidence_token,
        payload.get('review'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/items/companion-continuity-repair/execute',
    methods=['POST'],
)
@login_required
@admin_required
def admin_companion_continuity_repair_execute():
    """按冻结预览绑定唯一当前后继，并持久化可回滚复核回执。"""
    from plugins.adventurer_s1.services.s1_companion_historical_repair_service import (
        S1CompanionHistoricalRepairService,
    )
    payload = request.get_json(silent=True) or {}
    preview_token = str(payload.get('preview_token') or '').strip()
    if not preview_token:
        return error('必须提供谱系预览令牌')
    data, err = S1CompanionHistoricalRepairService.execute(
        request.current_user.id,
        preview_token,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/items/companion-continuity-repair/restore',
    methods=['POST'],
)
@login_required
@admin_required
def admin_companion_continuity_repair_restore():
    """移除指定批次谱系；仅在状态精确一致时回退同期阶级补正。"""
    from plugins.adventurer_s1.services.s1_companion_historical_repair_service import (
        S1CompanionHistoricalRepairService,
    )
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get('run_id') or '').strip()
    if not run_id:
        return error('必须提供 run_id')
    data, err = S1CompanionHistoricalRepairService.restore(
        request.current_user.id,
        run_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/fate-settlement-repair/review-evidence',
    methods=['GET'],
)
@login_required
@admin_required
def admin_fate_settlement_repair_review_evidence():
    """读取并冻结一条旧最终命途删除的完整复核证据。"""
    from plugins.adventurer_s1.services.s1_fate_settlement_repair_service import (
        S1FateSettlementRepairService,
    )
    try:
        expedition_log_id = int(request.args.get('expedition_log_id') or 0)
    except (TypeError, ValueError):
        return error('expedition_log_id 必须是正整数')
    if expedition_log_id <= 0:
        return error('expedition_log_id 必须是正整数')
    data, err = S1FateSettlementRepairService.load_historical_review_evidence(
        request.current_user.id,
        expedition_log_id=expedition_log_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/fate-settlement-repair/review-ai-preview',
    methods=['POST'],
)
@login_required
@admin_required
def admin_fate_settlement_repair_review_ai_preview():
    """调用配置的 S1 AI 逐项判断，并生成不写库的修复预览。"""
    from plugins.adventurer_s1.services.s1_fate_settlement_repair_service import (
        S1FateSettlementRepairService,
    )
    payload = request.get_json(silent=True) or {}
    evidence_token = str(payload.get('evidence_token') or '').strip()
    if not evidence_token:
        return error('必须提供冻结证据令牌')
    data, err = S1FateSettlementRepairService.ai_review_historical_evidence(
        request.current_user.id,
        evidence_token=evidence_token,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/fate-settlement-repair/review-preview',
    methods=['POST'],
)
@login_required
@admin_required
def admin_fate_settlement_repair_review_preview():
    """接收 Codex/外部复核提案，用同一合同生成安全修复预览。"""
    from plugins.adventurer_s1.services.s1_fate_settlement_repair_service import (
        S1FateSettlementRepairService,
    )
    payload = request.get_json(silent=True) or {}
    evidence_token = str(payload.get('evidence_token') or '').strip()
    if not evidence_token:
        return error('必须提供冻结证据令牌')
    data, err = S1FateSettlementRepairService.submit_historical_review(
        request.current_user.id,
        evidence_token=evidence_token,
        raw_review=payload.get('review'),
        review_source='external_api',
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/fate-settlement-repair/execute', methods=['POST'])
@login_required
@admin_required
def admin_fate_settlement_repair_execute():
    """按冻结预览恢复旧能力，并重开被错误关闭的最终试炼。"""
    from plugins.adventurer_s1.services.s1_fate_settlement_repair_service import (
        S1FateSettlementRepairService,
    )
    payload = request.get_json(silent=True) or {}
    data, err = S1FateSettlementRepairService.execute(
        request.current_user.id,
        str(payload.get('preview_token') or '').strip(),
    )
    if err:
        return error(err)
    return success(
        data,
        message=(
            f"已恢复 {data.get('created_count', 0)} 项旧技能/特质，"
            f"重开 {data.get('reopened_final_count', 0)} 人最终试炼"
        ),
    )


@adventurer_s1_bp.route('/admin/fate-settlement-repair/restore', methods=['POST'])
@login_required
@admin_required
def admin_fate_settlement_repair_restore():
    """回滚指定批次中仍保持修复原值的旧能力与命途状态。"""
    from plugins.adventurer_s1.services.s1_fate_settlement_repair_service import (
        S1FateSettlementRepairService,
    )
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get('run_id') or '').strip()
    if not run_id:
        return error('必须提供要回滚的命途修复批次号')
    data, err = S1FateSettlementRepairService.restore(
        run_id=run_id,
    )
    if err:
        return error(err)
    return success(data, message=f"已回滚 {data.get('removed_count', 0)} 项修复")


@adventurer_s1_bp.route('/admin/expedition-skill-repair/repair', methods=['POST'])
@login_required
@admin_required
def admin_expedition_skill_repair():
    """幂等补发结构化探险结算已承诺但未落库的技能。"""
    payload = request.get_json(silent=True) or {}
    expedition_log_id = payload.get('expedition_log_id')
    adventurer_id = payload.get('adventurer_id')
    try:
        expedition_log_id = int(expedition_log_id) if expedition_log_id else None
        adventurer_id = int(adventurer_id) if adventurer_id else None
    except (TypeError, ValueError):
        return error('expedition_log_id / adventurer_id 须为整数')
    from plugins.adventurer_s1.services.s1_expedition_skill_repair_service import (
        S1ExpeditionSkillRepairService,
    )
    data, err = S1ExpeditionSkillRepairService.repair(
        expedition_log_id=expedition_log_id,
        adventurer_id=adventurer_id,
        updated_by_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message=f"已补发 {data.get('created', 0)} 个漏发技能")


@adventurer_s1_bp.route('/admin/expedition-skill-repair/restore', methods=['POST'])
@login_required
@admin_required
def admin_expedition_skill_repair_restore():
    """撤销本修复服务创建且仍保持原身份的技能记录。"""
    from plugins.adventurer_s1.services.s1_expedition_skill_repair_service import (
        S1ExpeditionSkillRepairService,
    )
    data, err = S1ExpeditionSkillRepairService.restore()
    if err:
        return error(err)
    return success(data, message=f"已还原 {data.get('removed', 0)} 个补发技能")


@adventurer_s1_bp.route('/admin/expedition-consumption-repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_expedition_consumption_repair_preview():
    """预览旧版 legacy consumed 堆叠只扣 1 的精确可追回记录。"""
    expedition_log_id = request.args.get('expedition_log_id', type=int)
    adventurer_id = request.args.get('adventurer_id', type=int)
    from plugins.adventurer_s1.services.s1_expedition_consumption_repair_service import (
        S1ExpeditionConsumptionRepairService,
    )
    data, err = S1ExpeditionConsumptionRepairService.preview(
        request.current_user.id,
        expedition_log_id=expedition_log_id,
        adventurer_id=adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/same-name-replacement-desc-repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_same_name_replacement_desc_repair_preview():
    """预览同名装备替换或同名技能重复落账时丢失的描述。"""
    expedition_log_id = request.args.get('expedition_log_id', type=int)
    if not expedition_log_id:
        return error('expedition_log_id 必填')
    from plugins.adventurer_s1.services.s1_same_name_replacement_desc_repair_service import (
        S1SameNameReplacementDescRepairService,
    )
    data, err = S1SameNameReplacementDescRepairService.preview(
        expedition_log_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/same-name-replacement-desc-repair/execute', methods=['POST'])
@login_required
@admin_required
def admin_same_name_replacement_desc_repair_execute():
    """按管理员预览快照精确恢复同名替换前的冻结描述。"""
    payload = request.get_json(silent=True) or {}
    preview_token = str(payload.get('preview_token') or '').strip()
    if not preview_token:
        return error('请先预览后再执行')
    from plugins.adventurer_s1.services.s1_same_name_replacement_desc_repair_service import (
        S1SameNameReplacementDescRepairService,
    )
    data, err = S1SameNameReplacementDescRepairService.execute(
        request.current_user.id, preview_token,
    )
    if err:
        return error(err)
    return success(data, message=f"已修复 {data.get('repaired_count', 0)} 条资源描述")


@adventurer_s1_bp.route('/admin/same-name-replacement-desc-repair/restore', methods=['POST'])
@login_required
@admin_required
def admin_same_name_replacement_desc_repair_restore():
    """回滚最近一次同名替换资源描述修复。"""
    from plugins.adventurer_s1.services.s1_same_name_replacement_desc_repair_service import (
        S1SameNameReplacementDescRepairService,
    )
    data, err = S1SameNameReplacementDescRepairService.restore()
    if err:
        return error(err)
    return success(data, message=f"已回滚 {data.get('restored_count', 0)} 条资源描述")


@adventurer_s1_bp.route('/admin/asset-mechanism-revisions', methods=['POST'])
@login_required
@admin_required
def admin_asset_mechanism_revisions():
    """按显式清单预览、执行或条件恢复资产机制修订。"""
    from db_repository.exceptions import DatabaseError
    from plugins.adventurer_s1.services.s1_asset_mechanism_revision_service import (
        apply_asset_mechanism_revisions,
        preview_asset_mechanism_revisions,
    )

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) - {'action', 'manifest'}:
        return error('请求只接受 action 与 manifest')
    action = body.get('action', 'preview')
    if not isinstance(action, str) or action not in {'preview', 'apply', 'restore'}:
        return error('未知修订操作')
    admin_user_id = int(request.current_user.id)
    try:
        if action == 'preview':
            data = preview_asset_mechanism_revisions(body.get('manifest'))
        else:
            data = apply_asset_mechanism_revisions(
                body.get('manifest'), admin_user_id=admin_user_id, restore=action == 'restore',
            )
    except (ValueError, DatabaseError) as exc:
        cause = getattr(exc, 'original_error', None) or exc
        if isinstance(cause, ValueError):
            return error(str(cause))
        current_app.logger.exception('管理员资产机制修订失败')
        return error('资产修订失败，请核对修订回执后重试', 500)
    return success(data, message='请按每条资产的状态核对修订结果')


@adventurer_s1_bp.route('/admin/expedition-equipment-update-repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_expedition_equipment_update_repair_preview():
    """按单次探险冻结快照预览装备类型、阶级和长描述恢复。"""
    expedition_log_id = request.args.get('expedition_log_id', type=int)
    if not expedition_log_id:
        return error('expedition_log_id 必填')
    from plugins.adventurer_s1.services.s1_expedition_equipment_update_repair_service import (
        S1ExpeditionEquipmentUpdateRepairService,
    )
    data, err = S1ExpeditionEquipmentUpdateRepairService.preview(
        expedition_log_id,
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition-equipment-update-repair/execute', methods=['POST'])
@login_required
@admin_required
def admin_expedition_equipment_update_repair_execute():
    """执行刚才预览且当前状态未漂移的装备恢复。"""
    payload = request.get_json(silent=True) or {}
    preview_token = str(payload.get('preview_token') or '').strip()
    if not preview_token:
        return error('请先预览后再执行')
    from plugins.adventurer_s1.services.s1_expedition_equipment_update_repair_service import (
        S1ExpeditionEquipmentUpdateRepairService,
    )
    data, err = S1ExpeditionEquipmentUpdateRepairService.execute(
        request.current_user.id,
        preview_token,
    )
    if err:
        return error(err)
    return success(data, message=f"已修复 {data.get('repaired_count', 0)} 件装备")


@adventurer_s1_bp.route('/admin/expedition-equipment-update-repair/restore', methods=['POST'])
@login_required
@admin_required
def admin_expedition_equipment_update_repair_restore():
    """条件回滚最近一次探险装备更新恢复。"""
    from plugins.adventurer_s1.services.s1_expedition_equipment_update_repair_service import (
        S1ExpeditionEquipmentUpdateRepairService,
    )
    data, err = S1ExpeditionEquipmentUpdateRepairService.restore()
    if err:
        return error(err)
    return success(data, message=f"已回滚 {data.get('restored_count', 0)} 件装备")


@adventurer_s1_bp.route('/admin/expedition-consumption-repair/execute', methods=['POST'])
@login_required
@admin_required
def admin_expedition_consumption_repair_execute():
    """按管理员预览快照幂等追回历史物品漏扣。"""
    payload = request.get_json(silent=True) or {}
    preview_token = str(payload.get('preview_token') or '').strip()
    if not preview_token:
        return error('请先预览后再执行')
    from plugins.adventurer_s1.services.s1_expedition_consumption_repair_service import (
        S1ExpeditionConsumptionRepairService,
    )
    data, err = S1ExpeditionConsumptionRepairService.execute(
        request.current_user.id, preview_token,
    )
    if err:
        return error(err)
    return success(data, message=f"已追回 {data.get('repaired_count', 0)} 条历史漏扣")


@adventurer_s1_bp.route('/admin/expedition-consumption-repair/restore', methods=['POST'])
@login_required
@admin_required
def admin_expedition_consumption_repair_restore():
    """撤销所有尚未撤销的历史物品漏扣追回。"""
    from plugins.adventurer_s1.services.s1_expedition_consumption_repair_service import (
        S1ExpeditionConsumptionRepairService,
    )
    data, err = S1ExpeditionConsumptionRepairService.restore(request.current_user.id)
    if err:
        return error(err)
    return success(data, message=f"已撤销 {data.get('restored_count', 0)} 条历史漏扣追回")


@adventurer_s1_bp.route('/admin/low-consumption-overcredit-reclaim/preview', methods=['GET'])
@login_required
@admin_required
def admin_low_consumption_overcredit_reclaim_preview():
    """逐笔预览旧版低消费临时金重复增发及其原始收益谱系。"""
    from plugins.adventurer_s1.services.s1_low_consumption_overcredit_reclaim_service import (
        S1LowConsumptionOvercreditReclaimService,
    )

    data, err = S1LowConsumptionOvercreditReclaimService.preview(
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/low-consumption-overcredit-reclaim/execute', methods=['POST'])
@login_required
@admin_required
def admin_low_consumption_overcredit_reclaim_execute():
    """按冻结谱系整批回收重复价值，并写入逐旧流水唯一凭证。"""
    payload = request.get_json(silent=True) or {}
    preview_token = str(payload.get('preview_token') or '').strip()
    if not preview_token:
        return error('请先预览后再执行')
    from plugins.adventurer_s1.services.s1_low_consumption_overcredit_reclaim_service import (
        S1LowConsumptionOvercreditReclaimService,
    )

    data, err = S1LowConsumptionOvercreditReclaimService.execute(
        request.current_user.id,
        preview_token,
    )
    if err:
        return error(err)
    return success(
        data,
        message=f"已精确回收 {data.get('reclaimed_total', 0):g} 金币价值",
    )


@adventurer_s1_bp.route('/admin/low-consumption-overcredit-reclaim/restore', methods=['POST'])
@login_required
@admin_required
def admin_low_consumption_overcredit_reclaim_restore():
    """仅在回收后余额与流水均未变化时整批恢复。"""
    payload = request.get_json(silent=True) or {}
    batch_id = str(payload.get('batch_id') or '').strip()
    if not batch_id:
        return error('batch_id 必填')
    from plugins.adventurer_s1.services.s1_low_consumption_overcredit_reclaim_service import (
        S1LowConsumptionOvercreditReclaimService,
    )

    data, err = S1LowConsumptionOvercreditReclaimService.restore(
        request.current_user.id,
        batch_id,
    )
    if err:
        return error(err)
    return success(
        data,
        message=f"已恢复 {data.get('restored_count', 0)} 名冒险者的回收余额",
    )


@adventurer_s1_bp.route('/admin/commission-settlement-repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_commission_settlement_repair_preview():
    expedition_log_id = request.args.get('expedition_log_id', type=int)
    if not expedition_log_id:
        return error('expedition_log_id 须为正整数')
    data, err = S1CommissionService.preview_expedition_reconciliation(expedition_log_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/commission-settlement-repair/repair', methods=['POST'])
@login_required
@admin_required
def admin_commission_settlement_repair():
    expedition_log_id = (request.get_json(silent=True) or {}).get('expedition_log_id')
    try:
        expedition_log_id = int(expedition_log_id)
    except (TypeError, ValueError):
        return error('expedition_log_id 须为正整数')
    data, err = S1CommissionService.reconcile_expedition_log(expedition_log_id)
    if err:
        return error(err)
    return success(data, message=f"已补结算 {data.get('fulfilled', 0)} 个委托")


@adventurer_s1_bp.route('/admin/placeholder-skill-recall/send-offers', methods=['POST'])
@login_required
@admin_required
def admin_placeholder_skill_recall_send_offers():
    """批量向玩家推送「同意则移除占位入门技能并补发卷轴」确认。"""
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('adventurer_ids')
    adventurer_ids = None
    if isinstance(raw_ids, list) and raw_ids:
        try:
            adventurer_ids = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            return error('adventurer_ids 须为整数数组')
    admin_note = (payload.get('admin_note') or payload.get('note') or '').strip() or None
    from plugins.adventurer_s1.services.s1_placeholder_skill_recall_service import (
        S1PlaceholderSkillRecallService,
    )
    data, err = S1PlaceholderSkillRecallService.send_offers(
        admin_id=request.current_user.id,
        adventurer_ids=adventurer_ids,
        admin_note=admin_note,
    )
    if err:
        return error(err)
    sent = len(data.get('sent') or [])
    return success(data, message=f'已发送 {sent} 条回收确认邀请')


@adventurer_s1_bp.route('/placeholder-skill-recall/pending', methods=['GET'])
@login_required
def list_pending_placeholder_skill_recall_offers():
    """玩家：待确认的占位入门技能回收邀请。"""
    from plugins.adventurer_s1.services.s1_placeholder_skill_recall_service import (
        S1PlaceholderSkillRecallService,
    )
    try:
        limit = int(request.args.get('limit') or 10)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = S1PlaceholderSkillRecallService.list_pending_for_user(
        request.current_user.id, limit=limit,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/placeholder-skill-recall/<int:offer_id>/accept', methods=['POST'])
@login_required
def accept_placeholder_skill_recall_offer(offer_id):
    from plugins.adventurer_s1.services.s1_placeholder_skill_recall_service import (
        S1PlaceholderSkillRecallService,
    )
    data, err = S1PlaceholderSkillRecallService.accept_offer(
        offer_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已移除占位技能并补发职业通用技能卷轴')


@adventurer_s1_bp.route('/placeholder-skill-recall/<int:offer_id>/decline', methods=['POST'])
@login_required
def decline_placeholder_skill_recall_offer(offer_id):
    from plugins.adventurer_s1.services.s1_placeholder_skill_recall_service import (
        S1PlaceholderSkillRecallService,
    )
    data, err = S1PlaceholderSkillRecallService.decline_offer(
        offer_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已拒绝本次回收邀请')


# ── 创建冒险者（预约排队） ────────────────────────────────────────────────

@adventurer_s1_bp.route('/creation-options', methods=['GET'])
@login_required
def creation_options():
    """获取创建冒险者的可选项：初始技能、初始装备、性别池、性格池。"""
    data, err = AdventurerS1Service.get_creation_options(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/reserve', methods=['POST'])
@login_required
def submit_reservation():
    """玩家提交冒险者预约：选 1 技能 + 2 装备，进入排队。"""
    body = request.get_json(silent=True) or {}
    result, err = AdventurerS1Service.submit_reservation(
        user_id=request.current_user.id,
        name=body.get('name'),
        gender=body.get('gender'),
        personality=body.get('personality'),
        skill_template_id=body.get('skill_template_id'),
        item_ids=body.get('item_ids'),
    )
    if err:
        return error(err)
    return created(result)


@adventurer_s1_bp.route('/reservation-status', methods=['GET'])
@login_required
def reservation_status():
    """查询当前用户的 pending 预约状态。"""
    data, err = AdventurerS1Service.get_user_reservation_status(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/reservation/appearances', methods=['GET'])
@login_required
def reservation_appearances():
    """当前账号在当前区服的待抽取预约出场历程。"""
    from plugins.adventurer_s1.services.s1_reservation_history_service import (
        get_reservation_appearance_history,
    )
    page, per_page = get_pagination_params()
    return success(get_reservation_appearance_history(
        request.current_user.id, page=page, per_page=per_page,
    ))


@adventurer_s1_bp.route('/reservation/cancel', methods=['POST'])
@login_required
def cancel_reservation():
    """玩家取消自己的 pending 预约。"""
    data, err = AdventurerS1Service.cancel_reservation(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/reservation/update', methods=['PUT'])
@login_required
def update_reservation():
    """玩家修改自己的 pending 预约（只更新传入的字段）。"""
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1Service.update_reservation(
        user_id=request.current_user.id,
        name=body.get('name'),
        gender=body.get('gender'),
        personality=body.get('personality'),
        skill_template_id=body.get('skill_template_id'),
        item_ids=body.get('item_ids'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/reservation/boost-priority', methods=['POST'])
@login_required
def boost_reservation_priority():
    """玩家临时提高 pending 预约优先级（默认 6 小时内有效，到期后可再次使用）。"""
    data, err = AdventurerS1Service.boost_reservation_priority(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/archive-appeals', methods=['POST'])
@login_required
def submit_archive_appeal():
    """封存清退玩家提交解封申请。body: {adventurer_id: int, reason: str}"""
    from plugins.adventurer_s1.services.adventurer_s1_archive_appeal_service import (
        AdventurerS1ArchiveAppealService,
    )
    body = request.get_json(silent=True) or {}
    try:
        adventurer_id = int(body.get('adventurer_id'))
    except (TypeError, ValueError):
        return error('冒险者参数无效')
    data, err = AdventurerS1ArchiveAppealService.submit_appeal(
        user_id=request.current_user.id,
        adventurer_id=adventurer_id,
        reason=body.get('reason') or '',
    )
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/archive-appeals/mine', methods=['GET'])
@login_required
def my_archive_appeal():
    """查询当前用户的解封申请状态。"""
    from plugins.adventurer_s1.services.adventurer_s1_archive_appeal_service import (
        AdventurerS1ArchiveAppealService,
    )
    data, err = AdventurerS1ArchiveAppealService.get_my_appeal(request.current_user.id)
    if err:
        return error(err)
    return success(data)


# ── 冒险者列表 / 详情 ─────────────────────────────────────────────────────

@adventurer_s1_bp.route('/list', methods=['GET'])
@login_required
def adventurer_list():
    """获取当前玩家拥有的冒险者列表。"""
    data, err = AdventurerS1Service.get_adventurer_list(user_id=request.current_user.id)
    if err:
        return error(err)
    return _conditional_get_success(data)


@adventurer_s1_bp.route('/my/activity', methods=['GET'])
@login_required
def my_activity():
    """当前玩家冒险者的最近动态：货币/材料流水 + 建筑使用结果（含治疗/锻造成败）合并时间线。

    query: limit（默认 20，上限 50）、before（ISO 时间游标，加载更多）、
    filter（动态分类白名单；默认 all）。
    """
    data, err = AdventurerS1Service.get_my_activity(
        user_id=request.current_user.id,
        limit=request.args.get('limit', 20, type=int),
        before=request.args.get('before'),
        activity_filter=request.args.get('filter', 'all'),
        # 到期恢复由每分钟 scheduler 批量结算；GET 时间线只读，避免轮询触发写库。
        settle_due_trait_recovery=False,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/my/companion-evolution-ledger', methods=['GET'])
@login_required
def my_companion_evolution_ledger():
    """读取当前账号所有绑定冒险者各自独立的伙伴嬗变与费用账本。"""
    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
        S1CompanionEvolutionStageRepairService,
    )

    data, err = S1CompanionEvolutionStageRepairService.get_player_ledger(
        int(request.current_user.id),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/my/activity/<int:log_id>/undo', methods=['POST'])
@login_required
def undo_my_building_activity(log_id: int):
    """撤回百艺阁最近一次技能进阶或铁匠铺最近一次成功锻造。"""
    from plugins.adventurer_s1.services.s1_building_undo_service import S1BuildingUndoService

    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return error('请求参数无效')
    item_index = body.get('item_index')
    data, err = S1BuildingUndoService.undo(
        request.current_user.id,
        log_id,
        item_index=item_index,
    )
    if err:
        return error(err)
    return success(data)


# ── 纹章小队（纯名片装饰）─────────────────────────────────────────────────

def _squad_error(err):
    return error(err, code=403 if err == SQUAD_DISABLED_ERROR else 400)


def _squad_request_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


@adventurer_s1_bp.route('/squads', methods=['GET'])
@login_required
def squad_list():
    """分页读取纹章小队，以及当前玩家自己的小队详情。"""
    page, per_page = get_pagination_params(default_per_page=12, max_per_page=50)
    data, err = S1SquadService.list_squads(
        user_id=request.current_user.id,
        page=page,
        per_page=per_page,
    )
    if err:
        return _squad_error(err)
    return success(data)


@adventurer_s1_bp.route('/squads', methods=['POST'])
@login_required
def squad_create():
    """花费配置金币创建纹章小队；创建者固定为队长。"""
    body = _squad_request_body()
    if body is None:
        return error('请求参数无效')
    data, err = S1SquadService.create_squad(
        user_id=request.current_user.id,
        adventurer_id=body.get('adventurer_id'),
        name=body.get('name'),
        emblem=body.get('emblem'),
    )
    if err:
        return _squad_error(err)
    return created(data, message='纹章小队已创建')


@adventurer_s1_bp.route('/squads/<int:squad_id>/join', methods=['POST'])
@login_required
def squad_join(squad_id: int):
    """以自己的冒险者加入纹章小队。"""
    body = _squad_request_body()
    if body is None:
        return error('请求参数无效')
    data, err = S1SquadService.join_squad(
        user_id=request.current_user.id,
        adventurer_id=body.get('adventurer_id'),
        squad_id=squad_id,
    )
    if err:
        return _squad_error(err)
    return success(data, message='已加入纹章小队')


@adventurer_s1_bp.route('/squads/<int:squad_id>/leave', methods=['POST'])
@login_required
def squad_leave(squad_id: int):
    """普通成员退出纹章小队；队长须走解散操作。"""
    body = _squad_request_body()
    if body is None:
        return error('请求参数无效')
    data, err = S1SquadService.leave_squad(
        user_id=request.current_user.id,
        adventurer_id=body.get('adventurer_id'),
        squad_id=squad_id,
    )
    if err:
        return _squad_error(err)
    return success(data, message='已退出纹章小队')


@adventurer_s1_bp.route('/squads/<int:squad_id>/emblem', methods=['PUT'])
@login_required
def squad_update_emblem(squad_id: int):
    """队长用固定素材重新组合纹章。"""
    body = _squad_request_body()
    if body is None:
        return error('请求参数无效')
    data, err = S1SquadService.update_emblem(
        user_id=request.current_user.id,
        adventurer_id=body.get('adventurer_id'),
        squad_id=squad_id,
        emblem=body.get('emblem'),
    )
    if err:
        return _squad_error(err)
    return success(data, message='纹章已保存')


@adventurer_s1_bp.route('/squads/<int:squad_id>/disband', methods=['POST'])
@login_required
def squad_disband(squad_id: int):
    """队长解散纹章小队并一次性移除全体成员的名片纹章。"""
    body = _squad_request_body()
    if body is None:
        return error('请求参数无效')
    data, err = S1SquadService.disband_squad(
        user_id=request.current_user.id,
        adventurer_id=body.get('adventurer_id'),
        squad_id=squad_id,
    )
    if err:
        return _squad_error(err)
    return success(data, message='纹章小队已解散')


@adventurer_s1_bp.route('/<int:adv_id>', methods=['GET'])
@login_required
def adventurer_detail(adv_id):
    """玩家详情：私人库存仅本人可见；管理字段由独立管理接口提供。"""
    force_refresh = str(request.args.get('fresh') or '').strip() == '1'
    data, err = AdventurerS1Service.get_adventurer_detail(
        adventurer_id=adv_id,
        user_id=request.current_user.id,
        is_admin=False,
        force_refresh=force_refresh,
    )
    if err:
        return error(err)
    return _conditional_get_success(data)


@adventurer_s1_bp.route('/<int:adv_id>/initial-name', methods=['PUT'])
@login_required
def rename_initial_adventurer(adv_id):
    """为自动分配占位名的自有冒险者使用一次初始命名机会。"""
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1Service.rename_initial_adventurer(
        adventurer_id=adv_id,
        user_id=request.current_user.id,
        name=body.get('name'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/waiting-npc-origin', methods=['GET'])
@login_required
def waiting_npc_origin_state(adv_id):
    """读取自己的抽取前 NPC 来源账本；不向其他玩家公开。"""
    from plugins.adventurer_s1.services.s1_waiting_player_npc_service import (
        get_waiting_npc_origin_state,
    )
    data, err = get_waiting_npc_origin_state(
        adv_id,
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/waiting-npc-origin/reset', methods=['POST'])
@login_required
def reset_waiting_npc_origin_route(adv_id):
    """玩家一次性另启新生，只撤销抽取前 NPC 来源资产。"""
    from plugins.adventurer_s1.services.s1_waiting_player_npc_service import (
        reset_waiting_npc_origin,
    )
    data, err = reset_waiting_npc_origin(
        adv_id,
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已另启新生，抽取前 NPC 经历与继承项已清除')


@adventurer_s1_bp.route('/<int:adv_id>/active-title', methods=['PUT'])
@login_required
def set_adventurer_active_title(adv_id):
    """装备或卸下本人冒险者已获得的称号。title_id=null 表示卸下。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or 'title_id' not in body:
        return error('请求体必须包含 title_id（可为 null）')
    from plugins.adventurer_s1.services.s1_title_service import S1TitleService
    data, err = S1TitleService.equip(
        adv_id, request.current_user.id, body.get('title_id'),
    )
    if err:
        return error(err)
    return success(data, message='称号已更新')


@adventurer_s1_bp.route('/fate/<int:adv_id>', methods=['GET'])
@login_required
def fate_journey_state(adv_id):
    """读取绑定冒险者的 15 级命途旅程。"""
    from plugins.adventurer_s1.services.s1_fate_journey_service import S1FateJourneyService
    data, err = S1FateJourneyService.get_player_state(adv_id, request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/fate/<int:adv_id>/choices', methods=['POST'])
@login_required
def generate_fate_choices(adv_id):
    """由命途 AI生成该冒险者愿意追寻的个性化候选道路。"""
    from plugins.adventurer_s1.services.s1_fate_journey_service import S1FateJourneyService
    data, err = S1FateJourneyService.enqueue_choice_generation(adv_id, request.current_user.id)
    if err:
        return error(err)
    return success(data, message='命途的回响正在浮现')


@adventurer_s1_bp.route('/fate/<int:adv_id>/choose', methods=['POST'])
@login_required
def choose_fate_route(adv_id):
    """玩家确认愿意陪伴冒险者追寻的道路。"""
    payload = request.get_json(silent=True) or {}
    option_id = str(payload.get('option_id') or '').strip()
    if not option_id:
        return error('请选择要支持的命途方向')
    from plugins.adventurer_s1.services.s1_fate_journey_service import S1FateJourneyService
    data, err = S1FateJourneyService.choose_route(adv_id, request.current_user.id, option_id)
    if err:
        return error(err)
    return success(data, message='命途旅程已经开始')


@adventurer_s1_bp.route('/fate/<int:adv_id>/reset-request/respond', methods=['POST'])
@login_required
def respond_fate_reset_request(adv_id):
    """绑定玩家接受或拒绝管理员发起的命途重置确认。"""
    payload = request.get_json(silent=True) or {}
    request_id = payload.get('request_id')
    accept = payload.get('accept')
    if not isinstance(request_id, int) or not isinstance(accept, bool):
        return error('request_id 必须为整数，accept 必须为布尔值')
    from plugins.adventurer_s1.services.s1_fate_journey_service import S1FateJourneyService
    data, err = S1FateJourneyService.respond_reset(
        adv_id,
        request.current_user.id,
        request_id,
        accept,
    )
    if err:
        return error(err)
    message = '命途已经重置，可以重新生成候选' if accept else '已保留当前命途旅程'
    return success(data, message=message)


@adventurer_s1_bp.route('/<int:adv_id>/history-story', methods=['GET'])
@login_required
def adventurer_history_story(adv_id):
    """分页获取冒险史段落（最新在前）。query: page, per_page（默认 5，上限 20）。"""
    page, per_page = get_pagination_params(default_per_page=5, max_per_page=20)
    data, err = AdventurerS1Service.get_adventurer_history_story(
        adventurer_id=adv_id, page=page, per_page=per_page,
    )
    if err:
        return error(err)
    return success(data)


# ── 装备操作 ──────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/<int:adv_id>/items/protection', methods=['POST'])
@login_required
def set_item_protection(adv_id):
    """切换玩家物品保护。body: {source: equipment|warehouse, item_id: int, locked: bool}。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求体必须是 JSON 对象')
    source = body.get('source')
    item_id = body.get('item_id')
    locked = body.get('locked')
    if source not in {'equipment', 'warehouse'}:
        return error('source 必须是 equipment 或 warehouse')
    if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
        return error('item_id 必须是正整数')
    if not isinstance(locked, bool):
        return error('locked 必须是布尔值')

    from plugins.adventurer_s1.services.s1_item_protection_service import (
        S1ItemProtectionService,
    )

    data, err = S1ItemProtectionService.set_player_lock(
        adventurer_id=adv_id,
        user_id=request.current_user.id,
        source=source,
        item_id=item_id,
        locked=locked,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/equip', methods=['POST'])
@login_required
def equip_item(adv_id):
    """从仓库取出物品装备到随身。body: {item_id, item_name?, quantity?}。quantity 省略=整堆。"""
    body = request.get_json(silent=True) or {}
    item_id = body.get('item_id')
    item_name = body.get('item_name')
    quantity = body.get('quantity')
    if item_id is None and not item_name:
        return error('缺少 item_id 或 item_name 参数')
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.equip_from_warehouse(
        adventurer_id=adv_id, user_id=request.current_user.id,
        item_id=item_id, item_name=item_name, quantity=quantity, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/unequip', methods=['POST'])
@login_required
def unequip_item(adv_id):
    """把随身装备卸下存入仓库。body: {item_id, item_name?, quantity?}。quantity 省略=整堆。"""
    body = request.get_json(silent=True) or {}
    item_id = body.get('item_id')
    item_name = body.get('item_name')
    quantity = body.get('quantity')
    if item_id is None and not item_name:
        return error('缺少 item_id 或 item_name 参数')
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.unequip_to_warehouse(
        adventurer_id=adv_id, user_id=request.current_user.id,
        item_id=item_id, item_name=item_name, quantity=quantity, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/party-preference', methods=['POST'])
@login_required
def set_party_preference(adv_id):
    """设置组队偏好：auto 自动随机地图单人匹配，其他值不自动报名。body: {preference: str}"""
    body = request.get_json(silent=True) or {}
    preference = body.get('preference')
    if not preference:
        return error('缺少 preference 参数')
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.set_party_preference(
        adventurer_id=adv_id, user_id=request.current_user.id,
        preference=preference, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/join-party/<int:party_id>', methods=['POST'])
@login_required
def join_party(adv_id, party_id):
    """玩家手动让自己的冒险者加入指定 forming 小队。

    仅限 forming 阶段、未满员；冒险者须 idle/resting、精力 > 全局门槛，且 rank 跨度合法。
    """
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.join_party(
        adventurer_id=adv_id, party_id=party_id,
        user_id=request.current_user.id, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/special-dungeons', methods=['GET'])
@login_required
def list_special_dungeons():
    """列出门票制特殊副本（名称/门票/不限等级文案/简介）。

    可选 ?adventurer_id=：附带该冒险者是否持有对应门票（用于前端置灰组建按钮）。
    后端掷骰概率属于隐藏机制，不下发前端。
    """
    is_admin = getattr(request.current_user, 'is_admin', False)
    adv_id = request.args.get('adventurer_id', None, type=int)
    data, err = AdventurerS1Service.list_special_dungeons(
        adventurer_id=adv_id, user_id=request.current_user.id, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/create-special-party', methods=['POST'])
@login_required
def create_special_party(adv_id):
    """玩家用持票冒险者手动组建前往门票制特殊副本的小队（该冒险者为队长）。

    body: {dungeon: 副本名}。固定 4 人，创建后为集结中，满员后启动出发倒计时；门票在发车时才扣除。
    """
    body = request.get_json(silent=True) or {}
    dungeon = str(body.get('dungeon') or '').strip()
    if not dungeon:
        return error('请选择要前往的特殊副本')
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.create_special_party(
        adventurer_id=adv_id, dungeon_name=dungeon,
        user_id=request.current_user.id, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/enqueue', methods=['POST'])
@login_required
def enqueue_matchmaking():
    """普通副本单人匹配报名。body: {preferred_dungeons?: list[str], preferred_dungeon?: str|null}。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    body = request.get_json(silent=True) or {}
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.enqueue(
        user_id=request.current_user.id,
        preferred_dungeon=body.get('preferred_dungeon') or body.get('dungeon'),
        preferred_dungeons=body.get('preferred_dungeons'),
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/matchmaking/solo', methods=['POST'])
@login_required
def enqueue_matchmaking_solo(adv_id):
    """兼容旧前端的单人匹配入口；adv_id 会被忽略，后端使用当前玩家唯一可操作冒险者。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    body = request.get_json(silent=True) or {}
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.enqueue(
        user_id=request.current_user.id,
        preferred_dungeon=body.get('preferred_dungeon') or body.get('dungeon'),
        preferred_dungeons=body.get('preferred_dungeons'),
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/description', methods=['PUT'])
@login_required
def update_matchmaking_description(ticket_id):
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import AdventurerS1MatchmakingService
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get('description'), str):
        return error('请填写小队描述')
    data, err = AdventurerS1MatchmakingService.update_description(
        ticket_id, request.current_user.id, body['description'],
    )
    return error(err) if err else success(data)


@adventurer_s1_bp.route('/matchmaking/create-party', methods=['POST'])
@login_required
def create_matchmaking_party():
    """创建普通匹配小队。body: {preferred_dungeon?, description?, access_mode?, allow_solo_fill?}。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    body = request.get_json(silent=True) or {}
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.create_party(
        user_id=request.current_user.id,
        preferred_dungeon=body.get('preferred_dungeon') or body.get('dungeon'),
        description=body.get('description'),
        access_mode=body.get('access_mode'),
        allow_solo_fill=body.get('allow_solo_fill'),
        is_admin=is_admin,
        campaign_dispatch_id=body.get('campaign_dispatch_id'),
        campaign_dispatch_revision=body.get('campaign_dispatch_revision'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/available-dungeons', methods=['GET'])
@login_required
def matchmaking_available_dungeons():
    """当前玩家唯一可操作冒险者可选择的普通匹配地图。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    data, err = AdventurerS1MatchmakingService.available_dungeons(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/board', methods=['GET'])
@login_required
def matchmaking_board():
    """普通匹配面板聚合数据：我的报名、公开小队、可选地图。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    data, err = AdventurerS1MatchmakingService.board(
        request.current_user.id,
        client_version=request.args.get('version'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/public-parties', methods=['GET'])
@login_required
def matchmaking_public_parties():
    """公开普通匹配小队列表。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    data, err = AdventurerS1MatchmakingService.public_tickets(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/join', methods=['POST'])
@login_required
def join_matchmaking_public_party(ticket_id):
    """加入普通匹配小队。公开队来自列表；邀请制队通过小队编号加入。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    body = request.get_json(silent=True) or {}
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.join_public_party(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
        confirm_fate_risk=body.get('confirm_fate_risk') is True,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/code/<string:invite_code>/join', methods=['POST'])
@login_required
def join_matchmaking_party_by_code(invite_code):
    """通过 6 位随机邀请码加入普通匹配小队。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.join_by_invite_code(
        invite_code=invite_code,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/invite-candidates', methods=['GET'])
@login_required
def matchmaking_invite_candidates(ticket_id):
    """队长邀请候选：已关注排序靠前，可 search 搜索冒险者名。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    data, err = AdventurerS1MatchmakingService.invite_candidates(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        search=request.args.get('search'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/invite', methods=['POST'])
@login_required
def matchmaking_invite(ticket_id):
    """队长邀请目标冒险者加入小队。body: {adventurer_id: int}"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    body = request.get_json(silent=True) or {}
    adv_id = body.get('adventurer_id')
    if not adv_id:
        return error('缺少 adventurer_id')
    data, err = AdventurerS1MatchmakingService.invite_adventurer(
        ticket_id=ticket_id,
        target_adventurer_id=int(adv_id),
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/start', methods=['POST'])
@login_required
def start_matchmaking_party(ticket_id):
    """队长点击开始匹配：forming 小队转入 queued，之后才会被 scheduler 成团。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.start_matching(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/fate-risk/confirm', methods=['POST'])
@login_required
def confirm_matchmaking_fate_risk(ticket_id):
    """低等级成员确认当前目标地牢的命途事件风险。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.confirm_fate_risk(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/stop', methods=['POST'])
@login_required
def stop_matchmaking_party(ticket_id):
    """队长取消匹配但保留小队，queued -> forming。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.stop_matching(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/make-public', methods=['POST'])
@login_required
def make_matchmaking_party_public(ticket_id):
    """队长将邀请制小队转为公开（仅控制招募板可见性）。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.make_public(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/solo-fill', methods=['POST'])
@login_required
def set_matchmaking_solo_fill(ticket_id):
    """队长切换普通匹配小队是否允许单排路人补位。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    body = request.get_json(silent=True) or {}
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.set_solo_fill(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        allow_solo_fill=bool(body.get('allow_solo_fill')),
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/my', methods=['GET'])
@login_required
def my_matchmaking_tickets():
    """我的普通匹配小队/报名（forming/queued/matched）。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    data, err = AdventurerS1MatchmakingService.my_tickets(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/invites/my', methods=['GET'])
@login_required
def my_matchmaking_invites():
    """我收到的待处理小队邀请。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    data, err = AdventurerS1MatchmakingService.my_invites(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/invites/<int:invite_id>/accept', methods=['POST'])
@login_required
def accept_matchmaking_invite(invite_id):
    """接受小队邀请。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.accept_invite(invite_id, request.current_user.id, is_admin=is_admin)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/invites/<int:invite_id>/decline', methods=['POST'])
@login_required
def decline_matchmaking_invite(invite_id):
    """拒绝小队邀请。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.decline_invite(invite_id, request.current_user.id, is_admin=is_admin)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/leave', methods=['POST'])
@login_required
def leave_matchmaking_ticket(ticket_id):
    """非队长成员退出 forming 匹配小队。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.leave_ticket(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/matchmaking/<int:ticket_id>/cancel', methods=['POST'])
@login_required
def cancel_matchmaking(ticket_id):
    """取消 forming/queued 状态的普通匹配小队/报名。"""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1MatchmakingService.cancel(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/matchmaking/<int:ticket_id>/cancel',
    methods=['POST'],
)
@login_required
@admin_required
def admin_cancel_matchmaking(ticket_id):
    """Cancel the selected realm's ticket without trusting a colliding ID."""
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService,
    )

    data, err = AdventurerS1MatchmakingService.cancel(
        ticket_id=ticket_id,
        user_id=request.current_user.id,
        is_admin=True,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/leave-party', methods=['POST'])
@login_required
def leave_party(adv_id):
    """玩家手动让自己的冒险者离开当前所在 forming 小队（仅 forming 阶段可离）。"""
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.leave_party(
        adventurer_id=adv_id, user_id=request.current_user.id, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/vote-party-departure/<int:party_id>', methods=['POST'])
@login_required
def vote_party_departure(adv_id, party_id):
    """成员投票提前出发；全体成员投赞成票后下一调度 tick 立即发车。"""
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.vote_party_departure(
        adventurer_id=adv_id, party_id=party_id,
        user_id=request.current_user.id, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/equip-batch', methods=['POST'])
@login_required
def equip_batch(adv_id):
    """批量从仓库取出装备到随身。body: {item_ids: [int]}（或旧版 item_names）"""
    body = request.get_json(silent=True) or {}
    item_ids = body.get('item_ids')
    item_names = body.get('item_names')
    if not item_ids and not item_names:
        return error('缺少 item_ids 列表')
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.batch_equip_from_warehouse(
        adventurer_id=adv_id, user_id=request.current_user.id,
        item_ids=item_ids, item_names=item_names, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/unequip-batch', methods=['POST'])
@login_required
def unequip_batch(adv_id):
    """批量把随身装备卸下存入仓库。body: {item_ids: [int]}（或旧版 item_names）"""
    body = request.get_json(silent=True) or {}
    item_ids = body.get('item_ids')
    item_names = body.get('item_names')
    if not item_ids and not item_names:
        return error('缺少 item_ids 列表')
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.batch_unequip_to_warehouse(
        adventurer_id=adv_id, user_id=request.current_user.id,
        item_ids=item_ids, item_names=item_names, is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


# ── 特质 / 技能配置 ─────────────────────────────────────────────────────

@adventurer_s1_bp.route('/<int:adv_id>/traits/<int:trait_id>/equip', methods=['PUT'])
@login_required
def equip_trait(adv_id, trait_id):
    """携带一项经确认的纯正面特质。"""
    data, err = AdventurerS1Service.set_trait_equipped(
        adventurer_id=adv_id,
        user_id=request.current_user.id,
        trait_id=trait_id,
        equipped=True,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/traits/<int:trait_id>/unequip', methods=['PUT'])
@login_required
def unequip_trait(adv_id, trait_id):
    """不携带一项经确认的纯正面特质。"""
    data, err = AdventurerS1Service.set_trait_equipped(
        adventurer_id=adv_id,
        user_id=request.current_user.id,
        trait_id=trait_id,
        equipped=False,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/traits/unequip-all', methods=['PUT'])
@login_required
def unequip_all_traits(adv_id):
    """批量卸下本人当前允许卸下的特质。"""
    data, err = AdventurerS1Service.unequip_all_traits(
        adventurer_id=adv_id, user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/skills', methods=['GET'])
@login_required
def get_skills(adv_id):
    """获取冒险者当前技能列表。"""
    data, err = AdventurerS1Service.get_adventurer_skills(
        adventurer_id=adv_id, user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/skills/<int:skill_id>/equip', methods=['PUT'])
@login_required
def equip_skill(adv_id, skill_id):
    """将冒险者已有技能标记为携带。"""
    data, err = AdventurerS1Service.equip_skill(
        adventurer_id=adv_id, user_id=request.current_user.id, skill_id=skill_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/skills/<int:skill_id>/unequip', methods=['PUT'])
@login_required
def unequip_skill(adv_id, skill_id):
    """将冒险者已携带技能放回未携带池。"""
    data, err = AdventurerS1Service.unequip_skill(
        adventurer_id=adv_id, user_id=request.current_user.id, skill_id=skill_id,
    )
    if err:
        return error(err)
    return success(data)


# ── 酒馆动态 / 社交（最小只读层） ───────────────────────────────────────────

@adventurer_s1_bp.route('/notice', methods=['GET'])
@login_required
def tavern_notice():
    """获取深渊酒馆 S1 公告（含当前用户是否需展示）。"""
    data, err = AdventurerS1TavernService.get_notice(user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/notice/ack', methods=['POST'])
@login_required
def tavern_notice_ack():
    """玩家确认已读当前公告。"""
    body = request.get_json(silent=True) or {}
    version = (body.get('version') or '').strip()
    if not version:
        return error('缺少公告版本号', 400)
    data, err = AdventurerS1TavernService.ack_notice(
        user_id=request.current_user.id,
        version=version,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/notice', methods=['PUT'])
@login_required
@admin_required
def admin_set_tavern_notice():
    """管理员设置或更新酒馆顶部公告。"""
    body = request.get_json(silent=True) or {}
    notice = body.get('notice', '')
    data, err = AdventurerS1TavernService.admin_set_notice(notice=notice)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/notice', methods=['GET'])
@login_required
@admin_read_required
def admin_get_tavern_notice():
    """Read the selected realm's notice without player acknowledgement state."""
    data, err = AdventurerS1TavernService.get_notice(user_id=None)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/adventurers', methods=['GET'])
@login_required
def browse_adventurers():
    """分页浏览全体冒险者（酒馆「冒险者」标签）。支持 status/search/sort_field/sort_order。"""
    page, per_page = get_pagination_params(default_per_page=24)
    data, err = AdventurerS1TavernService.list_adventurers(
        page=page, per_page=per_page,
        status=request.args.get('status'),
        search=request.args.get('search'),
        sort_field=request.args.get('sort_field'),
        sort_order=request.args.get('sort_order'),
        current_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return _conditional_get_success(data)


@adventurer_s1_bp.route('/parties', methods=['GET'])
@login_required
def list_parties():
    """分页浏览小队（酒馆「小队」标签）。status 默认 active（集结中+探险中）。

    查询参数 mine=1 仅返回当前用户冒险者所在小队；exclude_mine=1 排除我的小队。
    """
    page, per_page = get_pagination_params(default_per_page=10)
    mine = request.args.get('mine') in ('1', 'true', 'yes')
    exclude_mine = request.args.get('exclude_mine') in ('1', 'true', 'yes')
    data, err = AdventurerS1TavernService.list_parties(
        page=page, per_page=per_page, status=request.args.get('status', 'active'),
        current_user_id=request.current_user.id,
        mine=mine,
        exclude_mine=exclude_mine,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/parties/board', methods=['GET'])
@login_required
def parties_board():
    """合并返回「我的小队」+「浏览小队」，减少小队 tab 的并发请求。"""
    data, err = AdventurerS1TavernService.list_parties_board(
        current_user_id=request.current_user.id,
        status=request.args.get('status', 'active'),
        client_version=request.args.get('version'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition-log-members/backfill/preview', methods=['GET'])
@login_required
@admin_required
def admin_expedition_log_members_backfill_preview():
    """预览探险日志参与者索引覆盖情况（只读）。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_log_member_service import (
        preview_expedition_log_member_backfill,
    )
    return success(preview_expedition_log_member_backfill())


@adventurer_s1_bp.route('/admin/expedition-log-members/backfill', methods=['POST'])
@login_required
@admin_required
def admin_expedition_log_members_backfill():
    """回填探险日志参与者索引（幂等，可重复触发）。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_log_member_service import (
        backfill_expedition_log_members,
        preview_expedition_log_member_backfill,
    )
    inserted = backfill_expedition_log_members()
    preview = preview_expedition_log_member_backfill()
    preview['inserted_rows'] = inserted
    return success(preview)


@adventurer_s1_bp.route('/logs', methods=['GET'])
@login_required
def list_logs():
    """探险日志动态流（轻量列表，不含 full_log）。
    支持 search / adventurer_id / status / exclude_failed 过滤。
    管理员额外返回 expedition_config（避免前端多一次请求）。
    """
    page, per_page = get_pagination_params(default_per_page=10)
    data, err = AdventurerS1TavernService.list_logs(
        page=page, per_page=per_page,
        search=request.args.get('search'),
        adventurer_id=request.args.get('adventurer_id', type=int),
        status=request.args.get('status'),
        category=request.args.get('category'),
        user_id=request.current_user.id,
        exclude_failed=request.args.get('exclude_failed') in ('1', 'true', 'yes'),
    )
    if err:
        return error(err)
    # 管理员附加探险配置，省去单独请求 /admin/expedition/config
    if getattr(request.current_user, 'is_admin', False):
        from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
            get_daily_expedition_quota_state,
        )
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        quota_used, quota = get_daily_expedition_quota_state()
        config = S1SettingsService.get_expedition_config()
        config['daily_expedition_quota'] = quota
        config['daily_expedition_used'] = quota_used
        data['expedition_config'] = config
    return success(data)


@adventurer_s1_bp.route('/logs/<int:log_id>', methods=['GET'])
@login_required
def log_detail(log_id):
    """单条探险日志详情（含 full_log）。管理员可见额外字段。"""
    is_admin = getattr(request.current_user, 'is_admin', False)
    reveal_full = is_admin and request.args.get('reveal_full') in ('1', 'true', 'yes')
    data, err = AdventurerS1TavernService.get_log_detail(
        log_id=log_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
        reveal_full=reveal_full,
    )
    if err:
        return error(err, 404 if err == '日志不存在' else 400)
    return success(data)


@adventurer_s1_bp.route('/logs/<int:log_id>/like', methods=['POST'])
@login_required
def like_expedition_log(log_id):
    """在最新一次已完成探险日志中给一名队友点赞。"""
    body = request.get_json(silent=True) or {}
    try:
        to_adventurer_id = int(body.get('to_adventurer_id') or 0)
    except (TypeError, ValueError):
        return error('点赞目标无效')
    if to_adventurer_id <= 0:
        return error('点赞目标无效')

    data, err = AdventurerS1TavernService.like_expedition_log(
        log_id=log_id,
        user_id=request.current_user.id,
        to_adventurer_id=to_adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/ai-feedback', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def submit_ai_feedback():
    """提交或修改当前玩家对一项 AI 成品的两维轻量评分。"""
    from plugins.adventurer_s1.services.s1_ai_feedback_service import (
        S1AiFeedbackService,
    )

    data, err = S1AiFeedbackService.submit(
        request.current_user.id,
        request.get_json(silent=True),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/logs/<int:log_id>/favorite', methods=['PUT', 'DELETE'])
@login_required
def set_expedition_log_favorite(log_id):
    """收藏或取消收藏指定探险日志。"""
    data, err = AdventurerS1TavernService.set_log_favorite(
        log_id=log_id,
        user_id=request.current_user.id,
        favorited=request.method == 'PUT',
    )
    if err:
        return error(err, 404 if err == '日志不存在' else 400)
    return success(data)


@adventurer_s1_bp.route('/logs/<int:log_id>/comments', methods=['GET'])
@login_required
def get_expedition_comments(log_id):
    """分页读取一个探险段落或成员状态更新的评论。"""
    target_type = str(request.args.get('target_type') or '').strip()
    try:
        target_index = int(request.args.get('target_index'))
    except (TypeError, ValueError):
        return error('评论位置无效')
    page, per_page = get_pagination_params(default_per_page=20, max_per_page=50)
    data, err = AdventurerS1TavernService.list_expedition_comments(
        log_id=log_id,
        target_type=target_type,
        target_index=target_index,
        page=page,
        per_page=per_page,
    )
    if err:
        return error(err, 404 if err == '日志不存在' else 400)
    return success(data)


@adventurer_s1_bp.route('/logs/<int:log_id>/comments', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def post_expedition_comment(log_id):
    """评论一个探险段落或成员状态更新。"""
    body = request.get_json(silent=True) or {}
    target_type = str(body.get('target_type') or '').strip()
    try:
        target_index = int(body.get('target_index'))
    except (TypeError, ValueError):
        return error('评论位置无效')
    data, err = AdventurerS1TavernService.post_expedition_comment(
        log_id=log_id,
        user_id=request.current_user.id,
        target_type=target_type,
        target_index=target_index,
        content=body.get('content'),
    )
    if err:
        return error(err, 404 if err == '日志不存在' else 400)
    return created(data)


@adventurer_s1_bp.route('/chat', methods=['GET'])
@login_required
def get_chat():
    """分页读取酒馆聊天（倒序）。"""
    page, per_page = get_pagination_params(default_per_page=30)
    data, err = AdventurerS1TavernService.list_chat(page=page, per_page=per_page)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/chat', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def post_chat():
    """发表一条酒馆聊天。body: {content: str}"""
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1TavernService.post_chat(
        user_id=request.current_user.id, content=body.get('content'),
    )
    if err:
        return error(err)
    return created(data)


# ── 酒馆升级进度（框架占位） ─────────────────────────────────────────────

@adventurer_s1_bp.route('/tavern/upgrade-progress', methods=['GET'])
@login_required
def tavern_upgrade_progress():
    """酒馆升级进度：存活冒险者数 / 下一档门槛 / 建筑 2 级是否已解锁。"""
    data, err = AdventurerS1TavernService.get_tavern_upgrade_progress()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/tavern/unlock-upgrade', methods=['POST'])
@login_required
@admin_required
def admin_unlock_tavern_upgrade():
    """管理员在升级条件满足后解锁酒馆升级。"""
    data, err = AdventurerS1TavernService.admin_unlock_tavern_upgrade(
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/tavern/upgrade-progress', methods=['GET'])
@login_required
@admin_read_required
def admin_tavern_upgrade_progress():
    """Read upgrade progress from the selected realm's isolated database."""
    data, err = AdventurerS1TavernService.get_tavern_upgrade_progress()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/tavern/force-set-level', methods=['POST'])
@login_required
@admin_required
def admin_force_set_tavern_level():
    """GM 测试：无视门槛直接设置酒馆等级（默认升到最高）并补建 Lv2 建筑。body: {target_level?: int}"""
    body = request.get_json(silent=True) or {}
    raw_level = body.get('target_level')
    target_level = None
    if raw_level is not None:
        try:
            target_level = int(raw_level)
        except (TypeError, ValueError):
            return error('target_level 必须是整数')
    data, err = AdventurerS1TavernService.admin_force_set_tavern_level(
        admin_user_id=request.current_user.id, target_level=target_level,
    )
    if err:
        return error(err)
    return success(data)


# ── 酒馆大堂座位 / 点酒 ──────────────────────────────────────────────────

@adventurer_s1_bp.route('/tavern/seats', methods=['GET'])
@login_required
def tavern_seats():
    """酒馆大堂座位全景：所有座位 + 在座冒险者 + 眼前的酒。"""
    data, err = AdventurerS1TavernSeatService.list_seats(user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tavern/drinks', methods=['GET'])
@login_required
def tavern_drinks():
    """酒单（静态数据，前端拉一次即可缓存）。"""
    data, err = AdventurerS1TavernSeatService.get_drinks_menu()
    if err:
        return error(err)
    return success({'drinks': data})


@adventurer_s1_bp.route('/tavern/foods', methods=['GET'])
@login_required
def tavern_foods():
    """Lv1 食物菜单（静态数据，前端拉一次即可缓存）。"""
    data, err = AdventurerS1TavernSeatService.get_foods_menu()
    if err:
        return error(err)
    return success({'foods': data})


@adventurer_s1_bp.route('/tavern/seats/<int:seat_id>/sit', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def tavern_seat_sit(seat_id):
    """让自己的空闲冒险者入座。body: {adventurer_id: int}"""
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not isinstance(adventurer_id, int):
        return error('缺少 adventurer_id 参数')
    data, err = AdventurerS1TavernSeatService.sit(
        user_id=request.current_user.id, seat_id=seat_id, adventurer_id=adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tavern/seats/<int:seat_id>/leave', methods=['POST'])
@login_required
def tavern_seat_leave(seat_id):
    """让自己的冒险者离座。"""
    data, err = AdventurerS1TavernSeatService.leave(
        user_id=request.current_user.id, seat_id=seat_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tavern/seats/<int:seat_id>/order', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def tavern_seat_order(seat_id):
    """给座位上自己的冒险者点酒（扣冒险者金币）。body: {drink_key: str}"""
    body = request.get_json(silent=True) or {}
    drink_key = body.get('drink_key')
    if not drink_key or not isinstance(drink_key, str):
        return error('缺少 drink_key 参数')
    data, err = AdventurerS1TavernSeatService.order_drink(
        user_id=request.current_user.id, seat_id=seat_id, drink_key=drink_key,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tavern/seats/<int:seat_id>/order-food', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def tavern_seat_order_food(seat_id):
    """给座位上自己的冒险者点餐。body: {food_key: str}"""
    body = request.get_json(silent=True) or {}
    food_key = body.get('food_key')
    if not food_key or not isinstance(food_key, str):
        return error('缺少 food_key 参数')
    data, err = AdventurerS1TavernSeatService.order_food(
        user_id=request.current_user.id, seat_id=seat_id, food_key=food_key,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tavern/seats/<int:seat_id>/treat', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def tavern_seat_treat(seat_id):
    """Request to buy a drink for another seated adventurer. body: {drink_key: str}"""
    body = request.get_json(silent=True) or {}
    drink_key = body.get('drink_key')
    if not drink_key or not isinstance(drink_key, str):
        return error('缺少 drink_key 参数')
    data, err = AdventurerS1TavernSeatService.request_treat(
        user_id=request.current_user.id,
        target_seat_id=seat_id,
        drink_key=drink_key,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tavern/treat-requests/<int:request_id>/respond', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def tavern_treat_respond(request_id):
    """Accept or decline an incoming drink invitation. body: {accept: bool}"""
    body = request.get_json(silent=True) or {}
    accept = body.get('accept')
    if not isinstance(accept, bool):
        return error('缺少 accept 参数')
    data, err = AdventurerS1TavernSeatService.respond_treat(
        user_id=request.current_user.id,
        request_id=request_id,
        accept=accept,
    )
    if err:
        return error(err)
    return success(data)


# ── 深渊茶舍座位 ─────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/tea-house/seats', methods=['GET'])
@login_required
def tea_house_seats():
    """茶舍座位全景：所有座位 + 在座冒险者 + 闲聊。"""
    data, err = AdventurerS1TeaHouseSeatService.list_seats(user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tea-house/teas', methods=['GET'])
@login_required
def tea_house_teas():
    """茶单（静态数据，前端拉一次即可缓存）。"""
    data, err = AdventurerS1TeaHouseSeatService.get_teas_menu()
    if err:
        return error(err)
    return success({'teas': data})


@adventurer_s1_bp.route('/tea-house/seats/<int:seat_id>/sit', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def tea_house_seat_sit(seat_id):
    """让自己的空闲冒险者入座（扣 30 金币入场费）。body: {adventurer_id: int}"""
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not isinstance(adventurer_id, int):
        return error('缺少 adventurer_id 参数')
    data, err = AdventurerS1TeaHouseSeatService.sit(
        user_id=request.current_user.id, seat_id=seat_id, adventurer_id=adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tea-house/seats/<int:seat_id>/order', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def tea_house_seat_order(seat_id):
    """给座位上自己的冒险者点茶（免费）。body: {tea_key: str}"""
    body = request.get_json(silent=True) or {}
    tea_key = body.get('tea_key')
    if not tea_key or not isinstance(tea_key, str):
        return error('缺少 tea_key 参数')
    data, err = AdventurerS1TeaHouseSeatService.order_tea(
        user_id=request.current_user.id, seat_id=seat_id, tea_key=tea_key,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tea-house/seats/<int:seat_id>/leave', methods=['POST'])
@login_required
def tea_house_seat_leave(seat_id):
    """让自己的冒险者离座（精力落账）。"""
    data, err = AdventurerS1TeaHouseSeatService.leave(
        user_id=request.current_user.id, seat_id=seat_id,
    )
    if err:
        return error(err)
    return success(data)


# ── 深渊温泉座位 ─────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/hot-spring/seats', methods=['GET'])
@login_required
def hot_spring_seats():
    """温泉座位全景：所有座位 + 在座冒险者 + 闲聊。"""
    data, err = AdventurerS1HotSpringSeatService.list_seats(user_id=request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/hot-spring/drinks', methods=['GET'])
@login_required
def hot_spring_drinks():
    """饮品单（静态数据，前端拉一次即可缓存）。"""
    data, err = AdventurerS1HotSpringSeatService.get_drinks_menu()
    if err:
        return error(err)
    return success({'drinks': data})


@adventurer_s1_bp.route('/hot-spring/seats/<int:seat_id>/sit', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def hot_spring_seat_sit(seat_id):
    """让自己的空闲冒险者入座（扣 100 金币入场费）。body: {adventurer_id: int}"""
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not isinstance(adventurer_id, int):
        return error('缺少 adventurer_id 参数')
    data, err = AdventurerS1HotSpringSeatService.sit(
        user_id=request.current_user.id, seat_id=seat_id, adventurer_id=adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/hot-spring/seats/<int:seat_id>/order', methods=['POST'])
@generic_anti_cheat_check('social')
@login_required
def hot_spring_seat_order(seat_id):
    """给座位上自己的冒险者点饮品（免费）。body: {drink_key: str}"""
    body = request.get_json(silent=True) or {}
    drink_key = body.get('drink_key')
    if not drink_key or not isinstance(drink_key, str):
        return error('缺少 drink_key 参数')
    data, err = AdventurerS1HotSpringSeatService.order_drink(
        user_id=request.current_user.id, seat_id=seat_id, drink_key=drink_key,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/hot-spring/seats/<int:seat_id>/leave', methods=['POST'])
@login_required
def hot_spring_seat_leave(seat_id):
    """让自己的冒险者离座（回血落账）。"""
    data, err = AdventurerS1HotSpringSeatService.leave(
        user_id=request.current_user.id, seat_id=seat_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/follow', methods=['POST'])
@login_required
@generic_anti_cheat_check('social')
def follow_adventurer(adv_id):
    """关注冒险者。"""
    data, err = AdventurerS1TavernService.follow(
        user_id=request.current_user.id, adventurer_id=adv_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/unfollow', methods=['POST'])
@login_required
@generic_anti_cheat_check('social')
def unfollow_adventurer(adv_id):
    """取消关注冒险者。"""
    data, err = AdventurerS1TavernService.unfollow(
        user_id=request.current_user.id, adventurer_id=adv_id,
    )
    if err:
        return error(err)
    return success(data)


# ── 管理员：预约管理 ──────────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/reservations', methods=['GET'])
@login_required
@admin_required
def admin_list_reservations():
    """管理员分页浏览预约列表。可按 status / priority 筛选、按冒险者名字/玩家名字/玩家ID 搜索。"""
    page, per_page = get_pagination_params(default_per_page=20)
    data, err = AdventurerS1Service.list_reservations(
        page=page, per_page=per_page, status=request.args.get('status'),
        search=request.args.get('search'), priority=request.args.get('priority'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/reservations/repair-duplicate-beds', methods=['POST'])
@login_required
@admin_required
def admin_repair_duplicate_beds():
    """管理员修复重复占床：同床位多人时保留最早入驻者，其余挪到空床。"""
    data, err = AdventurerS1Service.repair_duplicate_bed_assignments()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/reservations/detect-alts', methods=['GET'])
@login_required
@admin_required
def admin_detect_reservation_alts():
    """检测 pending / drawn 预约中的同伙多号（预约队列内 IP 关联分组，仅标记不做处理）。"""
    segment_bits = request.args.get('segment_bits', 24, type=int)
    data, err = AdventurerS1Service.detect_reservation_alts(segment_bits=segment_bits)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/reservations/auto-lower-alts', methods=['POST'])
@login_required
@admin_required
def admin_auto_lower_reservation_alts():
    data, err = AdventurerS1Service.auto_lower_alt_reservation_priorities()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/reservations/<int:res_id>/priority', methods=['POST'])
@login_required
@admin_required
def admin_set_priority(res_id):
    """管理员设置预约优先级。body: {priority: high/normal/low}"""
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1Service.set_priority(res_id, body.get('priority', 'normal'))
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/reservations/<int:res_id>/reject', methods=['POST'])
@login_required
@admin_required
def admin_reject_reservation(res_id):
    """管理员拒绝预约。body: {reason: str}"""
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1Service.reject_reservation(res_id, body.get('reason', ''))
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/reservations/<int:res_id>/restore', methods=['POST'])
@login_required
@admin_required
def admin_restore_reservation(res_id):
    """管理员恢复误拒绝的预约，重新放入抽取队列。"""
    data, err = AdventurerS1Service.restore_reservation(res_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/archive-appeals', methods=['GET'])
@login_required
@admin_required
def admin_list_archive_appeals():
    """管理员分页浏览解封申请。"""
    from plugins.adventurer_s1.services.adventurer_s1_archive_appeal_service import (
        AdventurerS1ArchiveAppealService,
    )
    page, per_page = get_pagination_params(default_per_page=20)
    data, err = AdventurerS1ArchiveAppealService.list_admin(
        page=page,
        per_page=per_page,
        status=request.args.get('status'),
        search=request.args.get('search'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/archive-appeals/<int:appeal_id>/approve', methods=['POST'])
@login_required
@admin_required
def admin_approve_archive_appeal(appeal_id):
    """管理员通过解封申请，将玩家放入预约抽取队列。"""
    from plugins.adventurer_s1.services.adventurer_s1_archive_appeal_service import (
        AdventurerS1ArchiveAppealService,
    )
    data, err = AdventurerS1ArchiveAppealService.approve(
        appeal_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/archive-appeals/<int:appeal_id>/reject', methods=['POST'])
@login_required
@admin_required
def admin_reject_archive_appeal(appeal_id):
    """管理员拒绝解封申请。body: {reason: str}"""
    from plugins.adventurer_s1.services.adventurer_s1_archive_appeal_service import (
        AdventurerS1ArchiveAppealService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1ArchiveAppealService.reject(
        appeal_id, request.current_user.id, body.get('reason') or '',
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/draw', methods=['POST'])
@login_required
@admin_required
def admin_draw():
    """管理员抽取 N 名冒险者。body: {count: int}"""
    body = request.get_json(silent=True) or {}
    count = body.get('count', 1)
    try:
        count = int(count)
    except (TypeError, ValueError):
        return error('抽取数量无效')
    data, err = AdventurerS1Service.draw_adventurers(count)
    if err:
        return error(err)
    return success(data)


# ── 管理员：冒险者数据管理 ──────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/titles', methods=['GET'])
@login_required
@admin_required
def admin_list_s1_titles():
    """管理员查看 S1 称号目录与聚合持有数。"""
    from plugins.adventurer_s1.services.s1_title_service import S1TitleService
    return success(S1TitleService.admin_list())


@adventurer_s1_bp.route('/admin/titles', methods=['POST'])
@login_required
@admin_required
def admin_create_s1_title():
    """创建可由管理员授予的 S1 称号。"""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error('请求参数无效')
    from plugins.adventurer_s1.services.s1_title_service import S1TitleService
    data, err = S1TitleService.admin_create(body)
    if err:
        return error(err)
    return success(data, message='称号已创建')


@adventurer_s1_bp.route('/admin/titles/<int:title_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_s1_title(title_id):
    """修改称号文案、启用状态与安全边框样式。"""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error('请求参数无效')
    from plugins.adventurer_s1.services.s1_title_service import S1TitleService
    data, err = S1TitleService.admin_update(title_id, body)
    if err:
        return error(err)
    return success(data, message='称号已保存')


@adventurer_s1_bp.route('/admin/titles/<int:title_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_s1_title(title_id):
    """删除自定义称号；系统称号仅允许停用。"""
    from plugins.adventurer_s1.services.s1_title_service import S1TitleService
    err = S1TitleService.admin_delete(title_id)
    if err:
        return error(err)
    return success(None, message='称号已删除')


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/titles', methods=['GET'])
@login_required
@admin_required
def admin_get_adventurer_titles(adv_id):
    """读取指定冒险者的自动资格与管理员授予记录。"""
    from plugins.adventurer_s1.services.s1_title_service import S1TitleService
    data, err = S1TitleService.admin_adventurer_state(adv_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/adventurers/<int:adv_id>/titles/<int:title_id>', methods=['POST'],
)
@login_required
@admin_required
def admin_grant_adventurer_title(adv_id, title_id):
    """管理员手工授予一个 S1 称号。"""
    body = request.get_json(silent=True) or {}
    from plugins.adventurer_s1.services.s1_title_service import S1TitleService
    data, err = S1TitleService.admin_grant(
        adv_id, title_id, request.current_user.id, body.get('note', ''),
    )
    if err:
        return error(err)
    return success(data, message='称号已授予')


@adventurer_s1_bp.route(
    '/admin/adventurers/<int:adv_id>/titles/<int:title_id>', methods=['DELETE'],
)
@login_required
@admin_required
def admin_revoke_adventurer_title(adv_id, title_id):
    """撤销管理员手工授予；结构化自动资格不会被误删。"""
    from plugins.adventurer_s1.services.s1_title_service import S1TitleService
    data, err = S1TitleService.admin_revoke(adv_id, title_id)
    if err:
        return error(err)
    return success(data, message='手工授予已撤销')


@adventurer_s1_bp.route('/admin/adventurers/detect-alts', methods=['GET'])
@login_required
@admin_required
def admin_detect_adventurer_alts():
    """检测在册冒险者中疑似同一人多号（IP 关联分组，仅标记不做处理）。"""
    segment_bits = request.args.get('segment_bits', 24, type=int)
    data, err = AdventurerS1Service.detect_adventurer_alts(segment_bits=segment_bits)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/adventurers', methods=['GET'])
@login_required
@admin_required
def admin_list_adventurers():
    """管理员分页浏览所有 S1 冒险者（含 admin 字段）。支持 status / search 过滤。"""
    from plugins.adventurer_s1.models import AdventurerS1

    page, per_page = get_pagination_params(default_per_page=20)
    status = request.args.get('status')
    search = request.args.get('search', '').strip()

    query = AdventurerS1.query
    if status:
        query = query.filter(AdventurerS1.status == status)
    if search:
        query = query.filter(AdventurerS1.name.ilike(f'%{search}%'))
    query = query.order_by(AdventurerS1.id.desc())

    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    building_info_map = AdventurerS1Service.build_adventurer_building_info_map(items)
    from plugins.adventurer_s1.services.s1_trait_loadout_service import (
        build_trait_loadout_states,
    )
    trait_loadout_states = build_trait_loadout_states(
        trait for adv in items for trait in adv.traits
    )

    item_dicts = [
        adv.to_dict(
            is_admin=True,
            mode='list',
            trait_loadout_states=trait_loadout_states,
            building_info=building_info_map.get(adv.id),
        )
        for adv in items
    ]
    # 批量注入贡献度（当前值 + 总值），一条 IN 查询取两种 code，避免逐个查 N+1
    if items:
        from plugins.adventurer_s1.models import AdventurerS1Currency
        cur_map: dict = {}
        total_map: dict = {}
        for r in AdventurerS1Currency.query.filter(
            AdventurerS1Currency.currency_code.in_(('contribution', 'contribution_total')),
            AdventurerS1Currency.adventurer_id.in_([adv.id for adv in items]),
        ).all():
            (cur_map if r.currency_code == 'contribution' else total_map)[r.adventurer_id] = int(r.amount)
        for d in item_dicts:
            d['contribution'] = cur_map.get(d.get('id'), 0)
            d['contribution_total'] = total_map.get(d.get('id'), 0)

    # 批量注入茶舍/温泉/酒馆座位状态（前端管理员强制踢人按钮条件）
    if items:
        from plugins.adventurer_s1.models import (
            AdventurerS1TeaHouseSeat, AdventurerS1TavernSeat, AdventurerS1HotSpringSeat,
        )
        adv_ids = [adv.id for adv in items]
        tea_advids = {
            row[0] for row in
            AdventurerS1TeaHouseSeat.query.with_entities(AdventurerS1TeaHouseSeat.adventurer_id)
            .filter(AdventurerS1TeaHouseSeat.adventurer_id.in_(adv_ids)).all()
        }
        hot_spring_advids = {
            row[0] for row in
            AdventurerS1HotSpringSeat.query.with_entities(AdventurerS1HotSpringSeat.adventurer_id)
            .filter(AdventurerS1HotSpringSeat.adventurer_id.in_(adv_ids)).all()
        }
        tavern_advids = {
            row[0] for row in
            AdventurerS1TavernSeat.query.with_entities(AdventurerS1TavernSeat.adventurer_id)
            .filter(AdventurerS1TavernSeat.adventurer_id.in_(adv_ids)).all()
        }
        for d in item_dicts:
            did = d.get('id')
            if did in tea_advids:
                d['tea_house_seated'] = True
            if did in hot_spring_advids:
                d['hot_spring_seated'] = True
            if did in tavern_advids:
                d['tavern_seated'] = True

    return success({
        'items': item_dicts,
        'total': total,
        'page': page,
        'pages': max(1, (total + per_page - 1) // per_page),
    })


@adventurer_s1_bp.route('/admin/adventurers/party-preference/manual', methods=['POST'])
@login_required
@admin_required
def admin_switch_all_players_to_manual():
    """管理员确认后，将当前选定区服所有在册玩家从自动匹配切换为手动。"""
    from plugins.adventurer_s1.services.adventurer_s1_admin_service import AdventurerS1AdminService

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求体必须是 JSON 对象')
    if set(body) - {'confirmed'}:
        return error('仅支持 confirmed 参数')
    if body.get('confirmed') is not True:
        return error('请确认将本区服全部玩家切换为手动匹配')

    data, err = AdventurerS1AdminService.admin_switch_all_players_to_manual(
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err, 500)
    return success(data, message='已将本区服全部自动匹配玩家切换为手动')


@adventurer_s1_bp.route('/admin/adventurers/inactive', methods=['GET'])
@login_required
@admin_required
def admin_list_inactive_adventurers():
    """管理员：列出超过不活跃窗口的酒馆冒险者。"""
    from plugins.adventurer_s1.services.adventurer_s1_admin_service import AdventurerS1AdminService

    page, per_page = get_pagination_params(default_per_page=50)
    data, err = AdventurerS1AdminService.list_inactive_adventurers(page=page, per_page=per_page)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/archive', methods=['POST'])
@login_required
@admin_required
def admin_archive_adventurer(adv_id):
    """管理员：封存清退冒险者（保留档案盒，释放床位）。"""
    from plugins.adventurer_s1.services.adventurer_s1_admin_service import AdventurerS1AdminService

    body = request.get_json(silent=True) or {}
    reason = body.get('reason')
    data, err = AdventurerS1AdminService.admin_archive_adventurer(
        adventurer_id=adv_id,
        admin_user_id=request.current_user.id,
        reason=reason,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/unarchive', methods=['POST'])
@login_required
@admin_required
def admin_unarchive_adventurer(adv_id):
    """管理员：解封封存清退冒险者，并送回封存前宿舍床位。"""
    from plugins.adventurer_s1.services.adventurer_s1_admin_service import AdventurerS1AdminService

    data, err = AdventurerS1AdminService.admin_unarchive_adventurer(
        adventurer_id=adv_id,
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/force-evict-building', methods=['POST'])
@login_required
@admin_required
def admin_force_evict_building(adv_id):
    """管理员：强制中断建筑占用，将冒险者送回宿舍休息态。"""
    from plugins.adventurer_s1.services.adventurer_s1_admin_service import AdventurerS1AdminService

    data, err = AdventurerS1AdminService.admin_force_evict_from_building(
        adventurer_id=adv_id,
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/gold/add', methods=['POST'])
@login_required
@admin_required
def admin_add_adventurer_gold(adv_id):
    """管理员：按正向增量增加金币，允许负余额逐步回补。"""
    from plugins.adventurer_s1.services.adventurer_s1_admin_service import (
        AdventurerS1AdminService,
    )
    from services.admin_operation_audit_service import AdminOperationAuditService

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求体必须是 JSON 对象')

    reason = body.get('reason')
    idempotency_key = (
        request.headers.get('Idempotency-Key')
        or request.headers.get('X-Idempotency-Key')
        or body.get('idempotency_key')
    )
    AdminOperationAuditService.annotate_current_operation(
        action_code='s1_admin_gold_add',
        target_type='adventurer_s1',
        target_id=adv_id,
        reason=reason if isinstance(reason, str) else None,
    )

    data, err = AdventurerS1AdminService.admin_add_gold(
        adventurer_id=adv_id,
        amount=body.get('amount'),
        reason=reason,
        idempotency_key=idempotency_key,
    )
    if err:
        if err == '冒险者不存在':
            return error(err, 404)
        if err.startswith('该 Idempotency-Key 已用于'):
            return error(err, 409)
        return error(err)

    message = '重复请求已回放，未再次增加金币' if data['idempotent_replay'] else '金币增加成功'
    return success(data, message=message)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>', methods=['GET'])
@login_required
@admin_required
def admin_get_adventurer(adv_id):
    """管理员查看单个冒险者完整数据。"""
    from plugins.adventurer_s1.models import AdventurerS1

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)
    from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
    detail = adv.to_dict(is_admin=True)
    detail['contribution'] = S1ContributionService.get_balance(adv_id)
    detail['contribution_total'] = S1ContributionService.get_total_balance(adv_id)
    return success(detail)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_adventurer(adv_id):
    '管理员修改冒险者数据。\n\n    可修改字段：name, gender, level, hp, max_hp, strength, agility, intelligence,\n    luck, personality, gold, contribution, exp, status, character_class,\n    party_preference, stamina, history_story。\n    '
    from db_repository import db_manager
    from db_repository.s1_realm_repository import S1RealmRepository
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services.s1_realm_service import (
        S1RealmAssignmentError,
        S1RealmService,
    )

    body = request.get_json(silent=True) or {}
    allowed_fields = {'name', 'gender', 'level', 'hp', 'max_hp', 'strength', 'agility', 'intelligence', 'luck', 'personality', 'gold', 'exp', 'status', 'character_class', 'party_preference', 'stamina', 'history_story'}
    int_fields = {'level', 'hp', 'max_hp', 'strength', 'agility', 'intelligence', 'luck', 'exp', 'stamina'}
    float_fields = {'gold'}
    valid_statuses = {
        'resting', 'in_party', 'exploring', 'activity',
        'building', 'dead', 'archived', 'idle',
    }

    requested_status = body.get('status')
    if 'status' in body and requested_status not in valid_statuses:
        return error(
            f"非法状态值「{requested_status}」，合法值：{', '.join(sorted(valid_statuses))}"
        )

    try:
        normalized = {}
        for field in allowed_fields:
            if field not in body:
                continue
            value = body[field]
            if field in int_fields:
                value = int(value)
            elif field in float_fields:
                value = float(value)
            elif field == 'personality' and isinstance(value, list):
                value = ','.join(value)
            normalized[field] = value
        new_contribution = (
            int(body['contribution']) if body.get('contribution') is not None else None
        )
        new_contribution_total = (
            int(body['contribution_total'])
            if body.get('contribution_total') is not None
            else None
        )
    except (TypeError, ValueError):
        return error('数值参数无效')

    new_gold = normalized.pop('gold', None)
    if normalized.get('hp', 0) < 0:
        return error('HP 不能为负数')
    if 'max_hp' in normalized and normalized['max_hp'] < 1:
        return error('最大 HP 必须至少为 1')
    if normalized.get('stamina', 0) < 0:
        return error('精力不能为负数')
    if 'level' in normalized and normalized['level'] < 1:
        return error('等级必须至少为 1')
    if normalized.get('exp', 0) < 0:
        return error('经验不能为负数')
    if new_gold is not None and new_gold < 0:
        return error('金币不能为负数')
    if new_contribution is not None and new_contribution < 0:
        return error('贡献度不能为负数')
    if new_contribution_total is not None and new_contribution_total < 0:
        return error('总贡献值不能为负数')

    if 'name' in normalized:
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        validated_name, name_err = AdventurerS1Service.validate_adventurer_name(
            normalized['name'],
        )
        if name_err:
            return error(name_err)
        normalized['name'] = validated_name

    updated = {}
    pre_adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not pre_adv:
        return error('冒险者不存在', 404)
    requested_player_activation = bool(
        pre_adv.is_player_created
        and pre_adv.owner_id is not None
        and 'status' in body
        and requested_status not in AdventurerS1.TAVERN_EXCLUDED_STATUSES
    )
    if (
        requested_player_activation
        and pre_adv.status in AdventurerS1.TAVERN_EXCLUDED_STATUSES
    ):
        return error(
            '不能通过通用编辑直接复活或解封玩家角色；请使用带全局槽位校验的解封流程',
            409,
        )
    expected_owner_id = pre_adv.owner_id
    pre_new_hp = normalized.get('hp', pre_adv.hp)
    pre_new_max_hp = normalized.get('max_hp', pre_adv.max_hp)
    if pre_new_hp > pre_new_max_hp:
        return error('HP 不能超过最大 HP')
    if 'name' in normalized:
        dup_err = AdventurerS1Service.assert_adventurer_name_available(
            normalized['name'], exclude_adventurer_id=adv_id,
        )
        if dup_err:
            return error(dup_err)

    with db_manager.transaction():
        adv = S1RealmRepository.lock_adventurer(adv_id)
        if not adv:
            return error('冒险者不存在', 404)
        reactivating_player = bool(
            requested_player_activation
            and adv.status in AdventurerS1.TAVERN_EXCLUDED_STATUSES
        )
        if requested_player_activation and adv.owner_id != expected_owner_id:
            return error('冒险者归属已变化，请重试')
        if reactivating_player:
            return error('玩家角色状态已变化，请刷新后使用解封流程', 409)

        new_hp = normalized.get('hp', adv.hp)
        new_max_hp = normalized.get('max_hp', adv.max_hp)
        new_stamina = normalized.get('stamina', adv.stamina)
        new_level = normalized.get('level', adv.level)
        new_exp = normalized.get('exp', adv.exp)
        if new_hp < 0:
            return error('HP 不能为负数')
        if new_max_hp < 1:
            return error('最大 HP 必须至少为 1')
        if new_hp > new_max_hp:
            return error('HP 不能超过最大 HP')
        if new_stamina < 0:
            return error('精力不能为负数')
        if new_level < 1:
            return error('等级必须至少为 1')
        if new_exp < 0:
            return error('经验不能为负数')

        if 'name' in normalized:
            dup_err = AdventurerS1Service.assert_adventurer_name_available(
                normalized['name'], exclude_adventurer_id=adv_id,
            )
            if dup_err:
                return error(dup_err)

        silent = bool(body.get('silent'))
        if new_gold is not None:
            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            current_balance, bal_err = S1CurrencyService.get_balance(adv_id, 'gold')
            if bal_err:
                return error(bal_err)
            delta = round(float(new_gold) - float(current_balance or 0), 2)
            if delta > 0.001:
                _, gold_err = S1CurrencyService.add(
                    adv_id, 'gold', delta,
                    source_type='admin_adjust', description='管理员修改冒险者金币',
                    auto_commit=False, silent=silent,
                )
                if gold_err:
                    return error(gold_err)
            elif delta < -0.001:
                _, gold_err = S1CurrencyService.deduct(
                    adv_id, 'gold', abs(delta),
                    source_type='admin_adjust', description='管理员修改冒险者金币',
                    auto_commit=False, silent=silent,
                )
                if gold_err:
                    return error(gold_err)
            updated['gold'] = round(float(new_gold), 2)

        # 贡献度：复用 currency 体系按差值调整（与金币同模式）
        if new_contribution is not None:
            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            cur_c, c_bal_err = S1CurrencyService.get_balance(adv_id, 'contribution')
            if c_bal_err:
                return error(c_bal_err)
            c_delta = round(float(new_contribution) - float(cur_c or 0), 2)
            if c_delta > 0.001:
                _, c_err = S1CurrencyService.add(
                    adv_id, 'contribution', c_delta,
                    source_type='admin_adjust', description='管理员修改贡献度',
                    auto_commit=False, silent=silent,
                )
                if c_err:
                    return error(c_err)
            elif c_delta < -0.001:
                _, c_err = S1CurrencyService.deduct(
                    adv_id, 'contribution', abs(c_delta),
                    source_type='admin_adjust', description='管理员修改贡献度',
                    auto_commit=False, allow_negative=True, silent=silent,
                )
                if c_err:
                    return error(c_err)
            updated['contribution'] = new_contribution

        # 总贡献值（累计获得）：管理员可单独校正
        if new_contribution_total is not None:
            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            cur_ct, ct_bal_err = S1CurrencyService.get_balance(adv_id, 'contribution_total')
            if ct_bal_err:
                return error(ct_bal_err)
            ct_delta = round(float(new_contribution_total) - float(cur_ct or 0), 2)
            if ct_delta > 0.001:
                _, ct_err = S1CurrencyService.add(
                    adv_id, 'contribution_total', ct_delta,
                    source_type='admin_adjust', description='管理员修改总贡献值',
                    auto_commit=False, silent=silent,
                )
                if ct_err:
                    return error(ct_err)
            elif ct_delta < -0.001:
                _, ct_err = S1CurrencyService.deduct(
                    adv_id, 'contribution_total', abs(ct_delta),
                    source_type='admin_adjust', description='管理员修改总贡献值',
                    auto_commit=False, allow_negative=True, silent=silent,
                )
                if ct_err:
                    return error(ct_err)
            updated['contribution_total'] = new_contribution_total

        for field, value in normalized.items():
            setattr(adv, field, value)
            updated[field] = value

        from plugins.adventurer_s1.services.s1_adventurer_stat_service import clamp_s1_adventurer_stats
        clamp_s1_adventurer_stats(adv)

        # 精力变更时同步时间戳，避免下次 get_current_stamina 计算偏移
        if 'stamina' in normalized:
            from datetime import datetime as _dt
            adv.stamina_updated_at = _portable_utc_now()

    if not updated:
        return error('没有可更新的字段')

    if 'name' in updated:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import (
                s1_socketio as socketio,
            )
            socketio.emit('adventurer_changed', {
                'action': 'admin_update',
                'reason': 'admin_rename',
                'ids': [adv_id],
            }, namespace='/')
        except Exception:
            pass

    return success({'updated': updated, 'message': f'已更新冒险者【{adv.name}】的信息'})


# ── 管理员：特质管理 ──────────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/traits', methods=['GET'])
@login_required
@admin_required
def admin_list_traits(adv_id):
    """管理员查看冒险者全部特质（含归档）。"""
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Trait
    from utils.time_utils import to_utc_iso

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)
    traits = AdventurerS1Trait.query.filter_by(adventurer_id=adv_id).order_by(
        AdventurerS1Trait.is_archived, AdventurerS1Trait.id,
    ).all()
    # 管理端 CRUD 依赖 trait id；to_dict() 面向玩家展示不含 id
    return success([{
        **t.to_dict(),
        'id': t.id,
        'description_ai': t.description_ai,
        'description_ai_at': to_utc_iso(t.description_ai_at) if t.description_ai_at else None,
        'description_ai_len': len((t.description_ai or '').strip()),
        'lore_entries': t.lore_entries or [],
    } for t in traits])


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/traits', methods=['POST'])
@login_required
@admin_required
def admin_add_trait(adv_id):
    """管理员添加特质。body: {name: str, description: str}"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Trait

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)

    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    desc = (body.get('description') or '').strip()
    if not name:
        return error('特质名称不能为空')

    with db_manager.transaction():
        trait = AdventurerS1Trait(adventurer_id=adv_id, name=name, description=desc)
        db_manager.add(trait)

    return success({'message': f'已添加特质【{name}】'})


@adventurer_s1_bp.route('/admin/traits/compress/preview', methods=['GET'])
@login_required
@admin_required
def admin_trait_compress_preview():
    """分页预览特质全文、当前摘要缓存与路由审核状态。"""
    from plugins.adventurer_s1.services.s1_trait_description_ai_service import S1TraitDescriptionAiService

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', type=int)
    if per_page is None:
        per_page = request.args.get('limit', 50, type=int)
    only_missing = request.args.get('only_missing', '1') not in ('0', 'false', 'False')
    data, err = S1TraitDescriptionAiService.preview(
        page=page,
        per_page=per_page,
        only_missing=only_missing,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/descriptions/compress/preview', methods=['GET'])
@login_required
@admin_required
def admin_description_compress_preview():
    """分页查看特质、装备和已携带技能的摘要与审核状态。"""
    from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
        S1DescriptionAiAdminPreviewService,
    )

    data, err = S1DescriptionAiAdminPreviewService.preview(
        asset_kind=request.args.get('kind', 'trait'),
        status_filter=request.args.get('status', 'all'),
        page=request.args.get('page', 1, type=int),
        per_page=request.args.get('per_page', 50, type=int),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/traits/compress/compare/<int:trait_id>', methods=['GET'])
@login_required
@admin_required
def admin_trait_compress_compare(trait_id):
    """单条特质压缩前后全文对比。"""
    from plugins.adventurer_s1.services.s1_trait_description_ai_service import S1TraitDescriptionAiService

    data, err = S1TraitDescriptionAiService.compare(trait_id)
    if err:
        return error(err, 404 if err == '特质不存在' else 400)
    return success(data)


@adventurer_s1_bp.route('/admin/traits/compress', methods=['POST'])
@login_required
@admin_required
def admin_trait_compress():
    """批量调用压缩 AI 生成 description_ai（不改玩家可见 description）。"""
    from services.async_task_service import AsyncTaskService

    body = request.get_json(silent=True) or {}
    trait_ids = body.get('trait_ids')
    if trait_ids is not None and not isinstance(trait_ids, list):
        return error('trait_ids 必须为数组')
    payload = {
        'trait_ids': trait_ids,
        'limit': body.get('limit', 50),
        'only_missing': body.get('only_missing', True),
    }
    if not AsyncTaskService.is_task_enqueued_or_running('ai_s1_trait_compress', payload):
        if not AsyncTaskService.submit_task('ai_s1_trait_compress', payload):
            return error('特质压缩任务入队失败，请稍后重试')
    return success({'queued': True}, message='特质压缩任务已入队')


@adventurer_s1_bp.route('/admin/traits/<int:trait_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_trait(trait_id):
    """管理员修改特质。body: {name?: str, description?: str, is_archived?: bool}"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1Trait
    from plugins.adventurer_s1.services.adventurer_s1_service import (
        invalidate_adventurer_detail_cache,
    )
    from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
        sync_trait_description_fields,
    )

    trait = AdventurerS1Trait.query.session.get(AdventurerS1Trait, trait_id)
    if not trait:
        return error('特质不存在', 404)

    body = request.get_json(silent=True) or {}
    with db_manager.transaction():
        if 'name' in body:
            trait.name = body['name'].strip()
        if 'description' in body:
            sync_trait_description_fields(
                trait,
                body['description'],
            )
        if 'is_archived' in body:
            trait.is_archived = bool(body['is_archived'])

    invalidate_adventurer_detail_cache(trait.adventurer_id)
    return success({'message': f'已更新特质【{trait.name}】'})


@adventurer_s1_bp.route('/admin/traits/<int:trait_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_trait(trait_id):
    """管理员删除特质。"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1Trait

    trait = AdventurerS1Trait.query.session.get(AdventurerS1Trait, trait_id)
    if not trait:
        return error('特质不存在', 404)

    name = trait.name
    with db_manager.transaction():
        db_manager.delete_instance(trait)

    return success({'message': f'已删除特质【{name}】'})


# ── 管理员：技能管理 ──────────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/skills', methods=['GET'])
@login_required
@admin_required
def admin_list_skills(adv_id):
    """管理员查看冒险者全部技能（含未携带）。"""
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Skill

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)
    skills = AdventurerS1Skill.query.filter_by(adventurer_id=adv_id).order_by(
        AdventurerS1Skill.equipped.desc(), AdventurerS1Skill.id,
    ).all()
    return success([
        {'id': s.id, 'name': s.name, 'description': s.description, 'equipped': s.equipped}
        for s in skills
    ])


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/skills', methods=['POST'])
@login_required
@admin_required
def admin_add_skill(adv_id):
    """管理员添加技能。body: {name: str, description: str, equipped?: bool}"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Skill

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)

    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    desc = (body.get('description') or '').strip()
    equipped = body.get('equipped', True)
    if not name:
        return error('技能名称不能为空')

    with db_manager.transaction():
        skill = AdventurerS1Skill(adventurer_id=adv_id, name=name, description=desc, equipped=bool(equipped))
        db_manager.add(skill)

    return success({'message': f'已添加技能【{name}】'})


@adventurer_s1_bp.route('/admin/skills/<int:skill_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_skill(skill_id):
    """管理员修改技能。body: {name?: str, description?: str, equipped?: bool}"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1Skill

    skill = AdventurerS1Skill.query.session.get(AdventurerS1Skill, skill_id)
    if not skill:
        return error('技能不存在', 404)

    body = request.get_json(silent=True) or {}
    with db_manager.transaction():
        old_skill_name = str(skill.name or '').strip()
        if 'name' in body:
            skill.name = body['name'].strip()
        if 'description' in body:
            skill.description = body['description'].strip()
        if 'equipped' in body:
            skill.equipped = bool(body['equipped'])
        from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import (
            sync_trait_asset_renames,
        )
        sync_trait_asset_renames(skill.adventurer_id, [{
            'asset_kind': 'skill',
            'asset_id': skill.id,
            'asset_location': 'skill',
            'old_name': old_skill_name,
            'new_name': skill.name,
        }])

    return success({'message': f'已更新技能【{skill.name}】'})


@adventurer_s1_bp.route('/admin/skills/<int:skill_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_skill(skill_id):
    """管理员删除技能。"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1Skill

    skill = AdventurerS1Skill.query.session.get(AdventurerS1Skill, skill_id)
    if not skill:
        return error('技能不存在', 404)

    name = skill.name
    with db_manager.transaction():
        db_manager.delete_instance(skill)

    return success({'message': f'已删除技能【{name}】'})


# ── 管理员：装备（背包）管理 ─────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/equipment', methods=['GET'])
@login_required
@admin_required
def admin_list_equipment(adv_id):
    """管理员查看冒险者全部随身装备。"""
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Equipment, AdventurerS1Item

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)
    items = AdventurerS1Equipment.query.filter_by(adventurer_id=adv_id).order_by(
        AdventurerS1Equipment.id,
    ).all()
    # 批量查图鉴描述，custom_desc 为空时回退图鉴
    names = [e.name for e in items if not e.custom_desc]
    catalog_map: dict[str, str] = {}
    if names:
        catalog_map = {
            i.name: i.description
            for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
        }
    return success([
        {
            'id': e.id, 'name': e.name, 'type': e.type, 'rarity': e.rarity,
            'custom_desc': e.custom_desc or catalog_map.get(e.name, ''),
            'quantity': int(e.quantity or 1), 'uses_left': e.uses_left,
            'damage_stage': e.damage_stage, 'damage_note': e.damage_note,
            'evolution_stage': max(0, int(getattr(e, 'evolution_stage', 0) or 0)),
        }
        for e in items
    ])


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/equipment', methods=['POST'])
@login_required
@admin_required
def admin_add_equipment(adv_id):
    """管理员添加随身装备。body: {name, type?, rarity?, custom_desc?, uses_left?}"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Equipment
    from plugins.adventurer_s1.services.s1_item_quantity_service import (
        coerce_item_quantity,
        split_stack_suffix,
    )

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)

    body = request.get_json(silent=True) or {}
    name, quantity = split_stack_suffix(body.get('name') or '', body.get('quantity', 1))
    if not name:
        return error('装备名称不能为空')

    from plugins.adventurer_s1.services.s1_item_world_unique_service import (
        can_grant_item_to_adventurer,
    )
    allowed, block_reason = can_grant_item_to_adventurer(
        name,
        adv_id,
    )
    if not allowed:
        return error(block_reason or '该物品已被其他冒险者持有，不可复制发放')

    from plugins.adventurer_s1.services.s1_item_instance_service import lock_warehouse_aggregate
    with db_manager.transaction():
        lock_warehouse_aggregate(adv_id)
        from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
        grant_data = {
            'type': (body.get('type') or '').strip(),
            'rarity': (body.get('rarity') or 'common').strip(),
            'quantity': coerce_item_quantity(quantity, 1),
            'uses_left': int(body['uses_left']) if body.get('uses_left') is not None else None,
            'desc': (body.get('custom_desc') or '').strip() or None,
        }
        if body.get('evolution_stage') is not None:
            try:
                grant_data['evolution_stage'] = max(0, int(body['evolution_stage']))
            except (TypeError, ValueError):
                return error('evolution_stage 必须为非负整数')
        _, grant_err = grant_raw_item(
            adv, name,
            grant_data,
            source_type='admin',
            description=f'GM发放「{name}」',
            force_destination='equipment',  # GM 强制随身
            silent=bool(body.get('silent')),
        )
        if grant_err:
            raise RuntimeError(grant_err)

    return success({'message': f'已添加装备【{name}】'})


@adventurer_s1_bp.route('/admin/equipment/<int:eq_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_equipment(eq_id):
    """管理员修改装备。body: {name?, type?, rarity?, custom_desc?, uses_left?}"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1Equipment
    from plugins.adventurer_s1.services.s1_item_quantity_service import (
        coerce_item_quantity,
        split_stack_suffix,
    )

    eq = AdventurerS1Equipment.query.session.get(AdventurerS1Equipment, eq_id)
    if not eq:
        return error('装备不存在', 404)

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error('请求体必须是对象')
    projection_name = eq.name
    adventurer_id = int(eq.adventurer_id)
    if 'name' in body:
        proposed_name, parsed_quantity = split_stack_suffix(
            body['name'],
            getattr(eq, 'quantity', 1),
        )
    else:
        proposed_name = eq.name
        parsed_quantity = coerce_item_quantity(getattr(eq, 'quantity', 1), 1)
    if not proposed_name:
        return error('装备名称不能为空')
    proposed_quantity = (
        coerce_item_quantity(body.get('quantity'), 1)
        if 'quantity' in body
        else parsed_quantity
    )
    try:
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            CatalogBusyError,
            lock_catalog_names,
        )
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            lock_warehouse_aggregate,
        )
        from plugins.adventurer_s1.services.s1_item_world_unique_service import (
            world_unique_instance_update_error,
        )

        with db_manager.transaction():
            lock_warehouse_aggregate(adventurer_id)
            if not lock_catalog_names(
                [projection_name, proposed_name],
                shared=False,
                wait=False,
            ):
                raise CatalogBusyError()
            eq = (
                AdventurerS1Equipment.query
                .filter_by(id=eq_id)
                .populate_existing()
                .with_for_update()
                .first()
            )
            if not eq:
                raise AdminItemUpdateError('装备不存在', 404)
            if eq.name != projection_name or int(eq.adventurer_id) != adventurer_id:
                raise RuntimeError('装备状态已变化，请刷新后重试')
            proposed_type = (
                str(body.get('type') or '').strip()
                if 'type' in body
                else str(eq.type or '').strip()
            )
            proposed_desc = (
                str(body.get('custom_desc') or '').strip()
                if 'custom_desc' in body
                else str(eq.custom_desc or '').strip()
            )
            unique_error = world_unique_instance_update_error(
                name=proposed_name,
                item_type=proposed_type,
                description=proposed_desc,
                quantity=proposed_quantity,
                adventurer_id=adventurer_id,
                equipment_item_id=eq.id,
            )
            if unique_error:
                raise AdminItemUpdateError(unique_error)

            eq.name = proposed_name
            eq.quantity = proposed_quantity
            if 'type' in body:
                eq.type = proposed_type
            if 'rarity' in body:
                eq.rarity = body['rarity'].strip()
            if 'custom_desc' in body:
                eq.custom_desc = proposed_desc or None
            if 'uses_left' in body:
                eq.uses_left = int(body['uses_left']) if body['uses_left'] is not None else None
            if 'evolution_stage' in body:
                from plugins.adventurer_s1.s1_item_types import (
                    clamp_evolution_stage,
                )
                try:
                    proposed = max(0, int(body['evolution_stage']))
                except (TypeError, ValueError):
                    raise AdminItemUpdateError('evolution_stage 必须为非负整数')
                if body.get('force_evolution_stage_downgrade'):
                    eq.evolution_stage = proposed
                else:
                    eq.evolution_stage = clamp_evolution_stage(
                        eq.evolution_stage,
                        proposed,
                    )
            from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import (
                sync_trait_asset_renames,
            )
            sync_trait_asset_renames(adventurer_id, [{
                'asset_kind': 'equipment',
                'asset_id': eq.id,
                'asset_location': 'equipment',
                'old_name': projection_name,
                'new_name': eq.name,
            }])
    except AdminItemUpdateError as exc:
        return error(exc.message, exc.status_code)
    except Exception as exc:
        return error(f'更新装备失败：{exc}')

    return success({'message': f'已更新装备【{eq.name}】'})


@adventurer_s1_bp.route('/admin/equipment/<int:eq_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_equipment(eq_id):
    """管理员删除装备。"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1Equipment

    eq = AdventurerS1Equipment.query.session.get(AdventurerS1Equipment, eq_id)
    if not eq:
        return error('装备不存在', 404)

    name = eq.name
    from plugins.adventurer_s1.services.s1_item_instance_service import remove_item_row
    with db_manager.transaction():
        _, del_err = remove_item_row(
            eq, source_type='admin', description='管理员删除装备', hidden_from_player=True,
        )
    if del_err:
        return error(del_err, 400)

    return success({'message': f'已删除装备【{name}】'})


# ── 管理员：仓库物品管理 ─────────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/warehouse', methods=['GET'])
@login_required
@admin_required
def admin_list_warehouse(adv_id):
    """管理员查看冒险者全部仓库物品。"""
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1WarehouseItem, AdventurerS1Item

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)
    items = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adv_id).order_by(
        AdventurerS1WarehouseItem.id,
    ).all()
    # 批量查图鉴描述，custom_desc 为空时回退图鉴
    names = [w.name for w in items if not w.custom_desc]
    catalog_map: dict[str, str] = {}
    if names:
        catalog_map = {
            i.name: i.description
            for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
        }
    return success([
        {
            'id': w.id, 'name': w.name, 'type': w.type, 'rarity': w.rarity,
            'custom_desc': w.custom_desc or catalog_map.get(w.name, ''),
            'quantity': int(w.quantity or 1),
            'uses_left': w.uses_left,
        }
        for w in items
    ])


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/warehouse', methods=['POST'])
@login_required
@admin_required
def admin_add_warehouse_item(adv_id):
    """管理员添加仓库物品。body: {name, type?, rarity?, custom_desc?, uses_left?}"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1WarehouseItem
    from plugins.adventurer_s1.services.s1_item_quantity_service import (
        coerce_item_quantity,
        split_stack_suffix,
    )

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)

    body = request.get_json(silent=True) or {}
    name, quantity = split_stack_suffix(body.get('name') or '', body.get('quantity', 1))
    if not name:
        return error('物品名称不能为空')

    from plugins.adventurer_s1.services.s1_item_world_unique_service import (
        can_grant_item_to_adventurer,
    )
    allowed, block_reason = can_grant_item_to_adventurer(
        name,
        adv_id,
    )
    if not allowed:
        return error(block_reason or '该物品已被其他冒险者持有，不可复制发放')

    from plugins.adventurer_s1.services.s1_item_instance_service import lock_warehouse_aggregate
    with db_manager.transaction():
        lock_warehouse_aggregate(adv_id)
        from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
        _, grant_err = grant_raw_item(
            adv, name,
            {'type': (body.get('type') or '').strip(),
             'rarity': (body.get('rarity') or 'common').strip(),
             'quantity': coerce_item_quantity(quantity, 1),
             'uses_left': int(body['uses_left']) if body.get('uses_left') is not None else None,
             'desc': (body.get('custom_desc') or '').strip() or None},
            source_type='admin',
            description=f'GM发放「{name}」（仓库）',
            force_destination='warehouse',
            silent=bool(body.get('silent')),
        )
        if grant_err:
            raise RuntimeError(grant_err)

    return success({'message': f'已添加仓库物品【{name}】'})


@adventurer_s1_bp.route('/admin/warehouse/<int:wh_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_warehouse_item(wh_id):
    """管理员修改仓库物品。body: {name?, type?, rarity?, custom_desc?, uses_left?}"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1WarehouseItem
    from plugins.adventurer_s1.services.s1_item_quantity_service import (
        coerce_item_quantity,
        split_stack_suffix,
    )

    w = AdventurerS1WarehouseItem.query.session.get(AdventurerS1WarehouseItem, wh_id)
    if not w:
        return error('仓库物品不存在', 404)

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error('请求体必须是对象')
    projection_name = w.name
    adventurer_id = int(w.adventurer_id)
    if 'name' in body:
        proposed_name, parsed_quantity = split_stack_suffix(
            body['name'],
            getattr(w, 'quantity', 1),
        )
    else:
        proposed_name = w.name
        parsed_quantity = coerce_item_quantity(getattr(w, 'quantity', 1), 1)
    if not proposed_name:
        return error('物品名称不能为空')
    proposed_quantity = (
        coerce_item_quantity(body.get('quantity'), 1)
        if 'quantity' in body
        else parsed_quantity
    )
    try:
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            CatalogBusyError,
            lock_catalog_names,
        )
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            lock_warehouse_aggregate,
        )
        from plugins.adventurer_s1.services.s1_item_world_unique_service import (
            world_unique_instance_update_error,
        )

        with db_manager.transaction():
            lock_warehouse_aggregate(adventurer_id)
            if not lock_catalog_names(
                [projection_name, proposed_name],
                shared=False,
                wait=False,
            ):
                raise CatalogBusyError()
            w = (
                AdventurerS1WarehouseItem.query
                .filter_by(id=wh_id)
                .populate_existing()
                .with_for_update()
                .first()
            )
            if not w:
                raise AdminItemUpdateError('仓库物品不存在', 404)
            if w.name != projection_name or int(w.adventurer_id) != adventurer_id:
                raise RuntimeError('仓库物品状态已变化，请刷新后重试')
            proposed_type = (
                str(body.get('type') or '').strip()
                if 'type' in body
                else str(w.type or '').strip()
            )
            proposed_desc = (
                str(body.get('custom_desc') or '').strip()
                if 'custom_desc' in body
                else str(w.custom_desc or '').strip()
            )
            unique_error = world_unique_instance_update_error(
                name=proposed_name,
                item_type=proposed_type,
                description=proposed_desc,
                quantity=proposed_quantity,
                adventurer_id=adventurer_id,
                warehouse_item_id=w.id,
            )
            if unique_error:
                raise AdminItemUpdateError(unique_error)

            w.name = proposed_name
            w.quantity = proposed_quantity
            if 'type' in body:
                w.type = proposed_type
            if 'rarity' in body:
                w.rarity = body['rarity'].strip()
            if 'custom_desc' in body:
                w.custom_desc = proposed_desc or None
            if 'uses_left' in body:
                w.uses_left = int(body['uses_left']) if body['uses_left'] is not None else None
            from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import (
                sync_trait_asset_renames,
            )
            sync_trait_asset_renames(adventurer_id, [{
                'asset_kind': 'equipment',
                'asset_id': w.id,
                'asset_location': 'warehouse',
                'old_name': projection_name,
                'new_name': w.name,
            }])
    except AdminItemUpdateError as exc:
        return error(exc.message, exc.status_code)
    except Exception as exc:
        return error(f'更新仓库物品失败：{exc}')

    return success({'message': f'已更新仓库物品【{w.name}】'})


@adventurer_s1_bp.route('/admin/warehouse/<int:wh_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_warehouse_item(wh_id):
    """管理员删除仓库物品。"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1WarehouseItem

    w = AdventurerS1WarehouseItem.query.session.get(AdventurerS1WarehouseItem, wh_id)
    if not w:
        return error('仓库物品不存在', 404)

    name = w.name
    from plugins.adventurer_s1.services.s1_item_instance_service import (
        lock_warehouse_aggregate,
        remove_item_row,
    )
    try:
        with db_manager.transaction():
            lock_warehouse_aggregate(w.adventurer_id)
            w = (
                AdventurerS1WarehouseItem.query
                .filter_by(id=wh_id)
                .populate_existing()
                .with_for_update()
                .first()
            )
            if not w:
                raise AdminItemUpdateError('仓库物品不存在', 404)
            _, del_err = remove_item_row(
                w, source_type='admin', description='管理员删除仓库物品', hidden_from_player=True,
            )
    except AdminItemUpdateError as exc:
        return error(exc.message, exc.status_code)
    if del_err:
        return error(del_err, 400)

    return success({'message': f'已删除仓库物品【{name}】'})


# ── 管理员：隐藏剧情线 ───────────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/story-threads/stats', methods=['GET'])
@login_required
@admin_required
def admin_story_thread_stats():
    """全服隐藏剧情线概览统计。"""
    from plugins.adventurer_s1.services.s1_story_thread_service import get_admin_story_thread_stats

    return success(get_admin_story_thread_stats())


@adventurer_s1_bp.route('/admin/story-threads', methods=['GET'])
@login_required
@admin_required
def admin_search_story_threads():
    """分页搜索全服隐藏剧情线。query: search, status, adventurer_id, page, per_page"""
    from plugins.adventurer_s1.services.s1_story_thread_service import search_admin_story_threads

    page, per_page = get_pagination_params(default_per_page=30)
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()
    adv_id_raw = request.args.get('adventurer_id', '').strip()
    adventurer_id = int(adv_id_raw) if adv_id_raw.isdigit() else None

    data, err = search_admin_story_threads(
        search=search,
        status=status,
        adventurer_id=adventurer_id,
        page=page,
        per_page=per_page,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/story-threads', methods=['POST'])
@login_required
@admin_required
def admin_create_story_thread():
    """管理员新增隐藏剧情线。body 可含经存在性校验的 entity_refs。"""
    from plugins.adventurer_s1.services.s1_story_thread_service import create_admin_story_thread

    body = request.get_json(silent=True) or {}
    item, err = create_admin_story_thread(body)
    if err:
        return error(err)
    return created(item)


@adventurer_s1_bp.route('/admin/story-threads/<int:thread_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_story_thread(thread_id):
    """管理员修改隐藏剧情线。"""
    from plugins.adventurer_s1.services.s1_story_thread_service import update_admin_story_thread

    body = request.get_json(silent=True) or {}
    item, err = update_admin_story_thread(thread_id, body)
    if err:
        status_code = 404 if '不存在' in err else 400
        return error(err, status_code)
    return success(item)


@adventurer_s1_bp.route('/admin/story-threads/<int:thread_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_story_thread(thread_id):
    """管理员删除隐藏剧情线。"""
    from plugins.adventurer_s1.services.s1_story_thread_service import delete_admin_story_thread

    data, err = delete_admin_story_thread(thread_id)
    if err:
        return error(err, 404)
    return success(data)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/story-threads', methods=['GET'])
@login_required
@admin_read_required
def admin_list_story_threads(adv_id):
    """管理员查看冒险者隐藏剧情线。仅供后台调试，不向玩家暴露。"""
    from plugins.adventurer_s1.services.s1_story_thread_service import list_admin_story_threads

    items, err = list_admin_story_threads(adv_id)
    if err:
        return error(err, 404)
    return success(items)


# ── 管理员：物品图鉴查询 ─────────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/items/dungeon-list', methods=['GET'])
@login_required
@admin_required
def admin_dungeon_list():
    """返回 S1 地牢名称列表，供物品图鉴管理中的副本限定下拉使用。"""
    from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_DUNGEONS
    return success([d['name'] for d in S1_DUNGEONS])


@adventurer_s1_bp.route('/admin/items/catalog', methods=['GET'])
@login_required
@admin_read_required
def admin_search_item_catalog():
    """搜索物品图鉴（供添加装备/仓库物品时选择）。支持 search / type / rarity 过滤。"""
    from plugins.adventurer_s1.models import AdventurerS1Item

    query = AdventurerS1Item.query
    search = request.args.get('search', '').strip()
    item_type = request.args.get('type', '').strip()
    rarity = request.args.get('rarity', '').strip()

    if search:
        query = query.filter(AdventurerS1Item.name.ilike(f'%{search}%'))
    if item_type:
        query = query.filter(AdventurerS1Item.type == item_type)
    if rarity:
        query = query.filter(AdventurerS1Item.rarity == rarity)

    items = query.order_by(AdventurerS1Item.name).limit(50).all()
    return success([i.to_admin_dict() for i in items])


@adventurer_s1_bp.route('/admin/items/catalog', methods=['POST'])
@login_required
@admin_required
def admin_create_catalog_item():
    """管理员新增图鉴条目。body: {name, type, description, rarity, world_unique?, is_starter?, use_condition?, uses_left?}"""
    from plugins.adventurer_s1.models import AdventurerS1Item
    from plugins.adventurer_s1.services.s1_item_use_condition_service import (
        apply_catalog_default_uses_left,
        normalize_use_condition,
    )
    from db_repository import db_manager

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error('请求体必须是对象')
    name = (body.get('name') or '').strip()
    if not name:
        return error('物品名称不能为空')
    try:
        uses_left, uses_left_provided = _parse_admin_uses_left(body)
    except ValueError as exc:
        return error(str(exc))

    requested_world_unique = (
        bool(body.get('world_unique'))
        if 'world_unique' in body
        else None
    )
    item = AdventurerS1Item(
        name=name,
        type=(body.get('type') or 'special').strip(),
        description=(body.get('description') or '').strip(),
        rarity=(body.get('rarity') or 'common').strip(),
        world_unique=bool(requested_world_unique),
        is_starter=bool(body.get('is_starter', False)),
        use_condition=normalize_use_condition(body.get('use_condition')),
    )
    if uses_left_provided:
        uses_err = apply_catalog_default_uses_left(item, uses_left, provided=True)
        if uses_err:
            return error(uses_err)
    from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
        lock_catalog_names,
    )

    try:
        with db_manager.transaction():
            lock_catalog_names([name], shared=False, wait=True)
            if AdventurerS1Item.query.filter_by(name=name).first():
                raise AdminItemUpdateError(f'物品「{name}」已存在')
            from plugins.adventurer_s1.services.s1_item_world_unique_service import (
                resolve_world_unique_flag,
                world_unique_promotion_error,
            )

            effective_world_unique = resolve_world_unique_flag(
                item.name,
                item_type=item.type or '',
                desc=item.description or '',
                catalog_flag=requested_world_unique,
            )
            if effective_world_unique:
                promotion_error = world_unique_promotion_error(
                    item.name,
                    item_type=item.type or '',
                )
                if promotion_error:
                    raise AdminItemUpdateError(promotion_error)
            item.world_unique = effective_world_unique
            ok, add_err = db_manager.add(item)
            if not ok:
                raise RuntimeError(add_err or '新增图鉴失败')
    except AdminItemUpdateError as exc:
        return error(exc.message, exc.status_code)
    except Exception as exc:
        return error(f'新增图鉴失败：{exc}', 500)

    from plugins.adventurer_s1.services.s1_item_catalog_service import (
        S1ItemCatalogService,
    )

    S1ItemCatalogService.refresh_after_catalog_change(int(item.id))
    return created(item.to_admin_dict())


@adventurer_s1_bp.route('/admin/items/catalog/<int:item_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_catalog_item(item_id):
    """管理员编辑图鉴条目。body: {name?, type?, description?, rarity?, world_unique?, is_starter?, use_condition?, uses_left?}"""
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return error('请求体必须是对象')
    from plugins.adventurer_s1.services.s1_item_catalog_service import (
        S1ItemCatalogService,
    )

    data, err = S1ItemCatalogService.update_catalog_item(item_id, body)
    if err:
        if err == '物品不存在':
            status = 404
        elif err.startswith('更新失败：'):
            status = 500
        else:
            status = 400
        return error(err, status)

    sync = (data or {}).get('sync') or {}
    default_uses = sync.get('default_uses') or {}
    if sync.get('content_changed'):
        message = (
            f'已更新「{data["name"]}」：同步随身 {sync.get("equipment", 0)} 件、'
            f'仓库 {sync.get("warehouse", 0)} 件、'
            f'进行中市场快照 {sync.get("market_total", 0)} 条'
        )
        if sync.get('default_uses_changed'):
            message += f'；同步未消耗默认次数 {default_uses.get("total", 0)} 条'
    elif sync.get('default_uses_changed'):
        message = (
            f'已更新「{data["name"]}」：'
            f'同步未消耗默认次数 {default_uses.get("total", 0)} 条'
        )
    else:
        message = (
            f'已更新「{data["name"]}」；'
            '没有需要同步的名称、类型、描述、稀有度或默认次数'
        )
    return success(data, message=message)


@adventurer_s1_bp.route(
    '/admin/items/catalog/<int:item_id>/history-sync/preview',
    methods=['POST'],
)
@login_required
@admin_required
def admin_preview_catalog_history_sync(item_id):
    """管理员按修改前完整图鉴值预览旧标准实例修复。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求体必须是对象')
    from plugins.adventurer_s1.services.s1_item_catalog_history_repair_service import (
        S1ItemCatalogHistoryRepairService,
    )

    data, err = S1ItemCatalogHistoryRepairService.preview(
        item_id,
        body,
        request.current_user.id,
    )
    if err:
        if err == '物品不存在':
            status = 404
        elif err.startswith('预览失败：'):
            status = 500
        else:
            status = 400
        return error(err, status)
    return success(data)


@adventurer_s1_bp.route('/admin/items/authority-review/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_item_authority():
    from plugins.adventurer_s1.services.s1_item_authority_service import admin_item_authority_action
    data, err = admin_item_authority_action(
        request.get_json(silent=True), action='preview', admin_user_id=request.current_user.id,
    )
    return error(err) if err else success(data)


@adventurer_s1_bp.route('/admin/items/authority-review/execute', methods=['POST'])
@login_required
@admin_required
def admin_execute_item_authority():
    from plugins.adventurer_s1.services.s1_item_authority_service import admin_item_authority_action
    data, err = admin_item_authority_action(
        request.get_json(silent=True), action='execute', admin_user_id=request.current_user.id,
    )
    return error(err) if err else success(data)


@adventurer_s1_bp.route('/admin/items/authority-review/restore', methods=['POST'])
@login_required
@admin_required
def admin_restore_item_authority():
    from plugins.adventurer_s1.services.s1_item_authority_service import admin_item_authority_action
    data, err = admin_item_authority_action(
        request.get_json(silent=True), action='restore', admin_user_id=request.current_user.id,
    )
    return error(err) if err else success(data)


@adventurer_s1_bp.route(
    '/admin/items/catalog/<int:item_id>/history-sync/execute',
    methods=['POST'],
)
@login_required
@admin_required
def admin_execute_catalog_history_sync(item_id):
    """管理员按短期预览快照执行图鉴历史同步修复。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求体必须是对象')
    preview_token = str(body.get('preview_token') or '').strip()
    if not preview_token:
        return error('请先预览，再执行历史同步修复')
    from plugins.adventurer_s1.services.s1_item_catalog_history_repair_service import (
        S1ItemCatalogHistoryRepairService,
    )

    data, err = S1ItemCatalogHistoryRepairService.execute(
        item_id,
        request.current_user.id,
        preview_token,
        note=str(body.get('note') or '').strip(),
    )
    if err:
        if err == '物品不存在':
            status = 404
        elif err.startswith('修复失败：'):
            status = 500
        elif (
            '发生变化' in err
            or '发生过还原' in err
            or '市场合同' in err
            or '已被另一条当前图鉴占用' in err
        ):
            status = 409
        else:
            status = 400
        return error(err, status)
    repaired = (data or {}).get('repaired') or {}
    return success(
        data,
        message=f'已修复 {repaired.get("total", 0)} 条当前资产快照',
    )


@adventurer_s1_bp.route(
    '/admin/items/catalog/<int:item_id>/history-sync/restore',
    methods=['POST'],
)
@login_required
@admin_required
def admin_restore_catalog_history_sync(item_id):
    """管理员还原指定的、仍可安全撤销的最近历史同步批次。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求体必须是对象')
    run_id = str(body.get('run_id') or '').strip()
    if not run_id:
        return error('请选择要还原的历史同步批次')
    from plugins.adventurer_s1.services.s1_item_catalog_history_repair_service import (
        S1ItemCatalogHistoryRepairService,
    )

    data, err = S1ItemCatalogHistoryRepairService.restore(
        item_id,
        request.current_user.id,
        run_id,
    )
    if err:
        if err == '物品不存在':
            status = 404
        elif err.startswith('还原失败：'):
            status = 500
        elif (
            '又被修改' in err
            or '批次已变化' in err
            or '已被另一条当前图鉴占用' in err
        ):
            status = 409
        else:
            status = 400
        return error(err, status)
    restored = (data or {}).get('restored') or {}
    if (data or {}).get('already_restored'):
        message = '该历史同步批次已经还原，本次未重复写入'
    else:
        message = f'已还原 {restored.get("total", 0)} 条当前资产快照'
    return success(
        data,
        message=message,
    )


@adventurer_s1_bp.route('/admin/items/catalog/<int:item_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_catalog_item(item_id):
    """管理员删除没有当前资产、交易或未完成修复引用的图鉴条目。"""
    from plugins.adventurer_s1.services.s1_item_catalog_service import (
        S1ItemCatalogService,
    )

    data, err = S1ItemCatalogService.delete_catalog_item(item_id)
    if err:
        status = 404 if err == '物品不存在' else 400
        if err.startswith('删除失败：'):
            status = 500
        return error(err, status)
    return success(data)


@adventurer_s1_bp.route('/admin/items/holders', methods=['GET'])
@login_required
@admin_read_required
def admin_search_item_holders():
    """按物品名搜索全服持有者（随身 + 仓库）。query: search, match=contains|exact, page, per_page"""
    from plugins.adventurer_s1.services.adventurer_s1_admin_service import AdventurerS1AdminService

    page, per_page = get_pagination_params(default_per_page=30)
    search = request.args.get('search', '').strip()
    match = request.args.get('match', 'contains').strip()
    data, err = AdventurerS1AdminService.search_item_holders(
        search=search,
        page=page,
        per_page=per_page,
        match=match,
    )
    if err:
        return error(err)
    return success(data)


# ── 探险（管理端手动触发：第一期端到端验证；自动调度留第二期） ──────────────────

@adventurer_s1_bp.route('/admin/expedition/form-parties', methods=['POST'])
@login_required
@admin_required
def admin_form_parties():
    """管理员手动跑一轮匹配 tick：按当前发车配额从匹配队列成团。"""
    from plugins.adventurer_s1.services.adventurer_s1_party_service import tick_party_formation
    return success(tick_party_formation())


@adventurer_s1_bp.route('/admin/expedition/<int:party_id>/start', methods=['POST'])
@login_required
@admin_required
def admin_start_expedition(party_id):
    """管理员对指定小队发起 AI 探险（生成剧本，跳过①行囊整理与②商店）。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import run_s1_expedition
    log_id, err = run_s1_expedition(party_id)
    if err:
        return error(err)
    return success({'log_id': log_id})


@adventurer_s1_bp.route('/admin/expedition/<int:party_id>/progress', methods=['POST'])
@login_required
@admin_required
def admin_progress_expedition(party_id):
    """管理员推进指定小队探险日志（本期一次推进到完成并结算写回）。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import progress_s1_expedition
    status, err = progress_s1_expedition(party_id)
    if err:
        return error(err)
    return success({'status': status})


@adventurer_s1_bp.route('/admin/expedition/logs/<int:log_id>/step', methods=['POST'])
@login_required
@admin_required
def admin_step_expedition_log(log_id):
    """管理员手动推进一批探险日志。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        step_s1_expedition_log,
    )
    data, err = step_s1_expedition_log(log_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/logs/<int:log_id>/force-complete', methods=['POST'])
@login_required
@admin_required
def admin_force_complete_expedition_log(log_id):
    """管理员直接释放剩余事件并结算探险日志。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        force_complete_s1_expedition_log,
    )
    data, err = force_complete_s1_expedition_log(log_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/expedition/logs/<int:log_id>/settlement-conflict-repair/preview',
    methods=['GET'],
)
@login_required
@admin_required
def admin_preview_expedition_settlement_conflict_repair(log_id):
    """只读预览可由冻结稳定装备 ID 证明的旧结算冲突。"""
    from plugins.adventurer_s1.services.s1_expedition_settlement_conflict_repair_service import (
        S1ExpeditionSettlementConflictRepairService,
    )
    data, err = S1ExpeditionSettlementConflictRepairService.preview(
        log_id,
        request.current_user.id,
    )
    if err:
        return error(err, 404 if err == '探险日志不存在' else 400)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/expedition/logs/<int:log_id>/settlement-conflict-repair/execute',
    methods=['POST'],
)
@login_required
@admin_required
def admin_execute_expedition_settlement_conflict_repair(log_id):
    """应用已冻结的机械补丁，并在同一日志上重放原结算。"""
    from plugins.adventurer_s1.services.s1_expedition_settlement_conflict_repair_service import (
        S1ExpeditionSettlementConflictRepairService,
    )
    payload = request.get_json(silent=True) or {}
    preview_token = str(payload.get('preview_token') or '').strip()
    if not preview_token:
        return error('必须提供结算冲突恢复预览令牌')
    data, err = S1ExpeditionSettlementConflictRepairService.execute(
        log_id,
        request.current_user.id,
        preview_token,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/expedition/logs/<int:log_id>/settlement-conflict-repair/restore',
    methods=['POST'],
)
@login_required
@admin_required
def admin_restore_expedition_settlement_conflict_repair(log_id):
    """在结算仍未落地且状态未漂移时回滚日志机械补丁。"""
    from plugins.adventurer_s1.services.s1_expedition_settlement_conflict_repair_service import (
        S1ExpeditionSettlementConflictRepairService,
    )
    payload = request.get_json(silent=True) or {}
    preview_token = str(payload.get('preview_token') or '').strip()
    if not preview_token:
        return error('必须提供结算冲突恢复预览令牌')
    data, err = S1ExpeditionSettlementConflictRepairService.restore(
        log_id,
        request.current_user.id,
        preview_token,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/logs/<int:log_id>/edit-text', methods=['PUT'])
@login_required
@admin_required
def admin_edit_expedition_log_text(log_id):
    """管理员编辑探险日志文本字段。"""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog
    log = AdventurerS1ExpeditionLog.query.session.get(AdventurerS1ExpeditionLog, log_id)
    if not log:
        return error('日志不存在', 404)

    data = request.get_json(force=True) or {}

    # 直接覆盖 result_summary
    if 'result_summary' in data:
        log.result_summary = data['result_summary']

    # full_log 定点修改：patches = [{path: "events.5.text", value: "..."}]
    patches = data.get('full_log_patches') or []
    if patches:
        import copy
        full_log = copy.deepcopy(log.full_log or {})
        for patch in patches:
            path = patch.get('path', '')
            value = patch.get('value')
            if not path:
                continue
            keys = path.split('.')
            target = full_log
            for key in keys[:-1]:
                if isinstance(target, list):
                    key = int(key)
                if not isinstance(target, (dict, list)) or key not in target:
                    target = None
                    break
                target = target[key]
            if target is not None:
                last_key = keys[-1]
                if isinstance(target, list):
                    last_key = int(last_key)
                if isinstance(target, (dict, list)) and (last_key in target if isinstance(target, dict) else 0 <= last_key < len(target)):
                    target[last_key] = value
        log.full_log = full_log

    db_manager.commit()
    return success({'id': log.id, 'updated': True})


@adventurer_s1_bp.route('/admin/expedition/logs/<int:log_id>/regenerate', methods=['POST'])
@login_required
@admin_required
def admin_regenerate_expedition_log(log_id):
    """管理员重新生成已失败（解散）的探险日志。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        regenerate_s1_expedition_log,
    )
    data, err = regenerate_s1_expedition_log(log_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/logs/<int:log_id>/retry-generating', methods=['POST'])
@login_required
@admin_required
def admin_retry_generating_expedition_log(log_id):
    """管理员重试超时卡在 generating 的探险日志。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        retry_generating_s1_expedition_log,
    )
    ret_log_id, err = retry_generating_s1_expedition_log(log_id)
    if err:
        return error(err)
    return success({'log_id': ret_log_id, 'retried': True})


@adventurer_s1_bp.route('/admin/expedition/logs/<int:log_id>/observer-review', methods=['GET'])
@login_required
@admin_required
def admin_get_expedition_observer_review(log_id):
    """读取指定探险日志最近一次 MIMO 只读剧情审核。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_observer_ai_service import (
        get_latest_expedition_observer_review,
    )
    data, err = get_latest_expedition_observer_review(log_id)
    if err:
        return error(err, 404 if err == '日志不存在' else 400)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/observer-reviews/<int:review_id>/approve', methods=['POST'])
@login_required
@admin_required
def admin_approve_expedition_observer_review(review_id):
    """管理员通过独立剧情审核并应用覆盖。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_observer_ai_service import (
        approve_expedition_observer_review,
    )
    data, err = approve_expedition_observer_review(review_id)
    if err:
        return error(err, 404 if err == '日志不存在' else 400)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/logs/<int:log_id>/observer-review/retry', methods=['POST'])
@login_required
@admin_required
def admin_retry_expedition_observer_review(log_id):
    """GM 手动重试指定日志的独立剧情审核。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_observer_ai_service import (
        retry_observer_review,
    )
    data, err = retry_observer_review(log_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/dispatch', methods=['POST'])
@login_required
@admin_required
def admin_dispatch_departed():
    """管理员手动跑一轮自动发车：扫已到 departure_time 的 forming 队,串行 run + progress。

    与 scheduler 的 adventurer_s1_expedition_dispatch_tick 复用同一函数,供调试与兜底用。
    """
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import dispatch_departed_expeditions
    return success(dispatch_departed_expeditions(manual=True))


@adventurer_s1_bp.route('/admin/party/<int:party_id>/force-dispatch', methods=['POST'])
@login_required
@admin_required
def admin_force_dispatch(party_id):
    """管理员强制出发：跳过 departure_time / 满员要求，直接对 forming 小队发起探险。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import force_dispatch_party
    log_id, err = force_dispatch_party(party_id)
    if err:
        return error(err)
    return success({'log_id': log_id})


@adventurer_s1_bp.route('/admin/party/<int:party_id>/force-disband', methods=['POST'])
@login_required
@admin_required
def admin_force_disband(party_id):
    """管理员强制解散小队（forming / exploring 均可）。exploring 的关联日志标为 failed。"""
    from plugins.adventurer_s1.services.adventurer_s1_party_service import force_disband_party
    ok, err = force_disband_party(party_id)
    if err:
        return error(err)
    return success({'disbanded': True})


@adventurer_s1_bp.route('/admin/party/<int:party_id>/remove-member', methods=['POST'])
@login_required
@admin_required
def admin_remove_party_member(party_id):
    """管理员从小队中移除指定成员（forming / exploring 均可）。body: {adventurer_id: int}"""
    from plugins.adventurer_s1.services.adventurer_s1_party_service import admin_remove_member
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    ok, err = admin_remove_member(party_id, int(adventurer_id))
    if err:
        return error(err)
    return success({'removed': True})


@adventurer_s1_bp.route('/admin/party-match-overview', methods=['GET'])
@login_required
@admin_required
def admin_party_match_overview():
    """管理员只读：谁在匹配、有哪些小队、成员与所属玩家。"""
    from plugins.adventurer_s1.services.adventurer_s1_admin_service import AdventurerS1AdminService

    data, err = AdventurerS1AdminService.get_party_match_overview(
        party_status=request.args.get('party_status', 'forming'),
        ticket_status=request.args.get('ticket_status', 'active'),
        party_limit=request.args.get('party_limit', 100, type=int),
        ticket_limit=request.args.get('ticket_limit', 100, type=int),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/ghost-in-party/preview', methods=['GET'])
@login_required
@admin_required
def admin_preview_ghost_in_party():
    """预览 status=in_party 但无活跃小队/匹配的「幽灵组队」冒险者。"""
    from plugins.adventurer_s1.services.adventurer_s1_party_service import admin_preview_ghost_in_party
    data, err = admin_preview_ghost_in_party()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/ghost-in-party/repair', methods=['POST'])
@login_required
@admin_required
def admin_repair_ghost_in_party():
    """修复幽灵组队：enter_rest + 清理 disbanded 队伍上的 stale member 行。"""
    from plugins.adventurer_s1.services.adventurer_s1_party_service import admin_repair_ghost_in_party
    data, err = admin_repair_ghost_in_party()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/fate-reset', methods=['GET'])
@login_required
@admin_required
def admin_get_fate_reset_state(adv_id):
    """读取冒险者命途及待处理重置确认。"""
    from plugins.adventurer_s1.services.s1_fate_journey_service import S1FateJourneyService
    data, err = S1FateJourneyService.get_admin_reset_state(adv_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/adventurers/<int:adv_id>/fate-reset', methods=['POST'])
@login_required
@admin_required
def admin_request_fate_reset(adv_id):
    """向绑定玩家发送命途重置确认，不直接改写旅程。"""
    payload = request.get_json(silent=True) or {}
    reason = payload.get('reason') or ''
    if not isinstance(reason, str):
        return error('reason 必须为字符串')
    from plugins.adventurer_s1.services.s1_fate_journey_service import S1FateJourneyService
    data, err = S1FateJourneyService.request_reset(
        adv_id,
        request.current_user.id,
        reason,
    )
    if err:
        return error(err)
    return success(data, message='命途重置确认已发送给绑定玩家')


@adventurer_s1_bp.route('/admin/daily-solo-ticket-repair/preview', methods=['GET'])
@login_required
@admin_required
def admin_preview_daily_solo_ticket_repair():
    """只读预览近期完成随机单排、但每日任务仍未结算的记录。"""
    from plugins.adventurer_s1.services.s1_daily_solo_ticket_repair_service import (
        S1DailySoloTicketRepairService,
    )

    data, err = S1DailySoloTicketRepairService.preview(days=request.args.get('days'))
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/daily-solo-ticket-repair/repair', methods=['POST'])
@login_required
@admin_required
def admin_repair_daily_solo_tickets():
    """重新校验预览候选，并沿任务结算链路幂等补发抽奖券。"""
    from plugins.adventurer_s1.services.s1_daily_solo_ticket_repair_service import (
        S1DailySoloTicketRepairService,
    )

    body = request.get_json(silent=True) or {}
    data, err = S1DailySoloTicketRepairService.repair(days=body.get('days'))
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/dispatch-diagnosis', methods=['GET'])
@login_required
@admin_required
def admin_expedition_dispatch_diagnosis():
    """只读发车配速诊断：当日「实际累计 vs 目标曲线」+ 此刻放行量 + 满员积压 + status + 昨夜耗尽。

    用于定位「集结完不发车」：区分凌晨超发顶过曲线 / 慢时段曲线压制 / failed 灌虚 used。
    """
    from plugins.adventurer_s1.services.adventurer_s1_expedition_throttle_service import (
        get_dispatch_diagnosis,
    )
    return success(get_dispatch_diagnosis())


@adventurer_s1_bp.route('/admin/expedition/log-dedup/preview', methods=['GET'])
@login_required
@admin_required
def admin_preview_expedition_log_dedup():
    """只读比较旧探险日志去重方案；不会改写日志或创建映射表。"""
    raw_cold_only = request.args.get('cold_only', '1')
    if raw_cold_only not in ('0', '1'):
        return error('cold_only 必须是 0 或 1')
    from plugins.adventurer_s1.services.s1_expedition_log_dedup_preview_service import (
        S1ExpeditionLogDedupPreviewService,
    )

    data, err = S1ExpeditionLogDedupPreviewService.preview(
        admin_user_id=int(request.current_user.id),
        before_log_id=request.args.get('before_log_id'),
        limit=request.args.get('limit'),
        cold_only=raw_cold_only == '1',
    )
    if err:
        return error(err, 500 if err.startswith('预览失败：') else 400)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/log-dedup/global-preview', methods=['GET'])
@login_required
@admin_required
def admin_preview_expedition_log_dedup_global():
    """只读估算跨日志共享内容字典；不会改写日志或创建映射表。"""
    raw_cold_only = request.args.get('cold_only', '1')
    if raw_cold_only not in ('0', '1'):
        return error('cold_only 必须是 0 或 1')
    from plugins.adventurer_s1.services.s1_expedition_log_dedup_global_preview_service import (
        S1ExpeditionLogDedupGlobalPreviewService,
    )

    data, err = S1ExpeditionLogDedupGlobalPreviewService.preview(
        admin_user_id=int(request.current_user.id),
        before_log_id=request.args.get('before_log_id'),
        scan_limit=request.args.get('scan_limit'),
        cold_only=raw_cold_only == '1',
    )
    if err:
        return error(err, 500 if err.startswith('预览失败：') else 400)
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/config', methods=['GET'])
@login_required
@admin_required
def admin_get_expedition_config():
    """查看当前探险/组队配置（持久化 settings 表，含每日配额使用进度）。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        get_daily_expedition_quota_state,
    )
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    quota_used, quota = get_daily_expedition_quota_state()
    data = S1SettingsService.get_expedition_config()
    data['daily_expedition_quota'] = quota
    data['daily_expedition_used'] = quota_used
    return success(data)


@adventurer_s1_bp.route('/admin/expedition/config', methods=['POST'])
@login_required
@admin_required
def admin_update_expedition_config():
    """持久化更新探险/组队配置，并热更新调度 tick 间隔。

    body: {
        party_min_size: int, party_max_size: int,
        departure_minutes: int, rank_cooldown_minutes: int,
        party_form_interval_minutes: int, expedition_dispatch_interval_minutes: int,
        expedition_progress_interval_minutes: int, daily_expedition_quota: int,
        hidden_dungeon_enabled: bool,
    }
    只传要改的字段即可。
    """
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import (
        EXPEDITION_API_FIELDS,
        S1SettingsService,
    )
    body = request.get_json(silent=True) or {}
    if 'ai_blackout_windows' in body:
        from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
            normalize_ai_blackout_windows,
        )
        windows, windows_err = normalize_ai_blackout_windows(body.get('ai_blackout_windows'))
        if windows_err:
            return error(windows_err)
        body['ai_blackout_windows'] = windows
    min_sz = body.get('party_min_size', S1SettingsService.get('party_min_size'))
    max_sz = body.get('party_max_size', S1SettingsService.get('party_max_size'))
    try:
        min_sz = int(min_sz)
        max_sz = int(max_sz)
    except (TypeError, ValueError):
        return error('队伍人数配置无效')
    if min_sz > max_sz:
        return error('队伍最小人数不能大于最大人数')

    updated = {}
    for field in EXPEDITION_API_FIELDS:
        if field not in body:
            continue
        ok, err = S1SettingsService.set(
            field, body[field], updated_by_user_id=request.current_user.id,
        )
        if not ok:
            return error(err or f'{field} 更新失败')
        updated[field] = S1SettingsService.get(field)

    if 'party_min_size' in updated:
        from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import invalidate_matchmaking_read_cache
        invalidate_matchmaking_read_cache()
    S1SettingsService.sync_expedition_settings_to_app_config()
    rescheduled = S1SettingsService.reschedule_expedition_scheduler_jobs()
    return success({
        **updated,
        'rescheduled_jobs': rescheduled,
    })


def _s1_ai_config_success(data):
    """Identify the Runtime that actually owns the returned configuration."""
    data = dict(data)
    data['realm_code'] = (
        current_app.config.get('ADVENTURER_S1_FIXED_REALM_CODE') or 'region_1'
    )
    response, status = success(data)
    response.headers['Cache-Control'] = 'private, no-store'
    return response, status


@adventurer_s1_bp.route('/admin/ai/config', methods=['GET'])
@login_required
@admin_required
def admin_get_ai_config():
    """读取 S1 多模型配置及功能分配；API Key 仅返回是否已配置。"""
    from plugins.adventurer_s1.services.s1_ai_runtime_config_service import get_ai_provider_config

    return _s1_ai_config_success(get_ai_provider_config())








_REGION_1_AI_SYNC_SOURCE_FIELDS = frozenset({
    'profile_sync_mode',
    'profiles',
    'profile_credentials',
    'default_profile_id',
    'assignments',
    'reasoning_efforts',
})








@adventurer_s1_bp.route('/admin/ai-feedback', methods=['GET'])
@login_required
@admin_required
def admin_ai_feedback():
    """汇总玩家 AI 评分；模型与站点来源仅完整管理权限可见。"""
    from plugins.adventurer_s1.services.s1_ai_feedback_service import (
        S1AiFeedbackService,
    )

    raw_days = str(request.args.get('days') or '30').strip().lower()
    if raw_days == 'all':
        days = None
    else:
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return error('days 必须是正整数或 all')
    page, per_page = get_pagination_params(default_per_page=20, max_per_page=100)
    data, err = S1AiFeedbackService.get_admin_dashboard(
        days=days,
        target_type=request.args.get('target_type'),
        page=page,
        per_page=per_page,
    )
    if err:
        return error(err)
    return success(data)


# ── AI Token 用量统计 ────────────────────────────────────────────────────────

def _parse_token_usage_time_window(default_days: int | None = 7):
    """解析统计时间窗；start_at 必须携带时区并转换为 UTC naive。"""
    raw_days = request.args.get('days', None, type=str)
    if raw_days in (None, ''):
        days = default_days
    else:
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return None, None, error('days 必须是正整数')
        if days < 1:
            return None, None, error('days 必须是正整数')

    raw_start_at = (request.args.get('start_at', '', type=str) or '').strip()
    if not raw_start_at:
        return days, None, None
    try:
        start_at = datetime.fromisoformat(raw_start_at.replace('Z', '+00:00'))
    except ValueError:
        return None, None, error('start_at 必须是带时区的 ISO 8601 时间')
    if start_at.tzinfo is None:
        return None, None, error('start_at 必须是带时区的 ISO 8601 时间')
    start_at = start_at.astimezone(timezone.utc).replace(tzinfo=None)
    return days, start_at, None

@adventurer_s1_bp.route('/admin/token-usage', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage():
    """查看 AI 调用 token 用量总览统计（含每日明细）。

    query params:
        days: int = 7  统计最近 N 天
        start_at: ISO 8601 = None  精确统计起点，优先于 days
        call_type: str = None  按调用类型过滤
    """
    from plugins.adventurer_s1.services.s1_token_usage_service import get_stats
    days, start_at, parse_error = _parse_token_usage_time_window()
    if parse_error is not None:
        return parse_error
    call_type = request.args.get('call_type', None, type=str)
    stats = get_stats(days=days, call_type=call_type, start_at=start_at)
    return success(stats)


@adventurer_s1_bp.route('/admin/token-usage/by-type', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage_by_type():
    """按调用类型（call_type）分组统计 token 用量。

    query params:
        days: int = 7
        start_at: ISO 8601 = None  精确统计起点，优先于 days
    """
    from plugins.adventurer_s1.services.s1_token_usage_service import get_stats_by_call_type
    days, start_at, parse_error = _parse_token_usage_time_window()
    if parse_error is not None:
        return parse_error
    return success(get_stats_by_call_type(days=days, start_at=start_at))


@adventurer_s1_bp.route('/admin/token-usage/by-party', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage_by_party():
    """按小队（party）分组统计 token 用量，附带队员名称。

    query params:
        days: int = 7
        start_at: ISO 8601 = None  精确统计起点，优先于 days
        call_type: str = None
    """
    from plugins.adventurer_s1.services.s1_token_usage_service import get_stats_by_party
    days, start_at, parse_error = _parse_token_usage_time_window()
    if parse_error is not None:
        return parse_error
    call_type = request.args.get('call_type', None, type=str)
    return success(get_stats_by_party(
        days=days,
        call_type=call_type,
        start_at=start_at,
    ))


@adventurer_s1_bp.route('/admin/token-usage/by-expedition', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage_by_expedition():
    """按探险日志（expedition_log）分组统计 token 用量。

    query params:
        days: int = 7
        start_at: ISO 8601 = None  精确统计起点，优先于 days
        call_type: str = None
    """
    from plugins.adventurer_s1.services.s1_token_usage_service import get_stats_by_expedition
    days, start_at, parse_error = _parse_token_usage_time_window()
    if parse_error is not None:
        return parse_error
    call_type = request.args.get('call_type', None, type=str)
    return success(get_stats_by_expedition(
        days=days,
        call_type=call_type,
        start_at=start_at,
    ))


@adventurer_s1_bp.route('/admin/token-usage/call-types', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage_call_types():
    """返回全部已注册的 AI call_type 及中文标签。"""
    from plugins.adventurer_s1.services.s1_token_usage_service import list_call_types
    return success(list_call_types())


@adventurer_s1_bp.route('/admin/token-usage/records', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage_records():
    """分页查询 token 用量原始记录。

    query params:
        page: int = 1
        per_page: int = 50
        call_type: str = None
        party_id: int = None
        expedition_log_id: int = None
        correlation_id: str = None
        days: int = None
        start_at: ISO 8601 = None  精确统计起点，优先于 days
        is_success: bool = None
        sort_by: str = created_at
        sort_order: str = desc
    """
    from plugins.adventurer_s1.services.s1_token_usage_service import get_records
    days, start_at, parse_error = _parse_token_usage_time_window(default_days=None)
    if parse_error is not None:
        return parse_error
    success_value = request.args.get('is_success', None, type=str)
    is_success = None
    if success_value in {'true', 'false'}:
        is_success = success_value == 'true'
    return success(get_records(
        page=request.args.get('page', 1, type=int),
        per_page=request.args.get('per_page', 50, type=int),
        call_type=request.args.get('call_type', None, type=str),
        party_id=request.args.get('party_id', None, type=int),
        expedition_log_id=request.args.get('expedition_log_id', None, type=int),
        correlation_id=request.args.get('correlation_id', None, type=str),
        days=days,
        start_at=start_at,
        is_success=is_success,
        sort_by=request.args.get('sort_by', 'created_at', type=str),
        sort_order=request.args.get('sort_order', 'desc', type=str),
    ))


@adventurer_s1_bp.route('/admin/token-usage/records/<int:record_id>/failure-detail', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage_failure_detail(record_id):
    from plugins.adventurer_s1.services.s1_token_usage_service import get_failure_detail
    detail = get_failure_detail(record_id)
    if detail is None:
        return error('该调用没有保存全文，或全文已清理', 404)
    return success(detail)


@adventurer_s1_bp.route('/admin/token-usage/failure-details/clear', methods=['POST'])
@login_required
@admin_primary_required
def admin_token_usage_clear_failure_details():
    from plugins.adventurer_s1.services.s1_token_usage_service import clear_failure_details
    payload = request.get_json(silent=True)
    try:
        result = clear_failure_details(payload.get('record_ids') if isinstance(payload, dict) else None)
    except ValueError as exc:
        return error(str(exc), 400)
    return success(result, message='已清理所选全文，调用统计和错误摘要保留')


@adventurer_s1_bp.route('/admin/token-usage/appeal-by-adventurer', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage_appeal_by_adventurer():
    """按冒险者聚合数据申诉 AI token 用量（含每日明细与均值）。

    query params:
        days: int = 7
        start_at: ISO 8601 = None  精确统计起点，优先于 days
        date: str = None  YYYY-MM-DD，指定则只看单日
        sort_by: str = total_tokens  total_tokens | avg_tokens | total_calls
        sort_order: str = desc  desc | asc
    """
    from plugins.adventurer_s1.services.s1_token_usage_service import get_appeal_stats_by_adventurer
    days, start_at, parse_error = _parse_token_usage_time_window()
    if parse_error is not None:
        return parse_error
    return success(get_appeal_stats_by_adventurer(
        days=days,
        sort_by=request.args.get('sort_by', 'total_tokens', type=str),
        sort_order=request.args.get('sort_order', 'desc', type=str),
        date=request.args.get('date', None, type=str) or None,
        start_at=start_at,
    ))


@adventurer_s1_bp.route('/admin/token-usage/appeal-link-backfill/preview', methods=['GET'])
@login_required
@admin_primary_required
def admin_token_usage_appeal_link_backfill_preview():
    """预览历史申诉 token 行可回填的 appeal_id / adventurer_id 数量。"""
    from plugins.adventurer_s1.services.s1_token_usage_service import preview_appeal_token_link_backfill
    return success(preview_appeal_token_link_backfill())


@adventurer_s1_bp.route('/admin/token-usage/appeal-link-backfill', methods=['POST'])
@login_required
@admin_primary_required
def admin_token_usage_appeal_link_backfill():
    """将推断的申诉 token 归属写回数据库（幂等）。"""
    from plugins.adventurer_s1.services.s1_token_usage_service import execute_appeal_token_link_backfill
    result, err = execute_appeal_token_link_backfill()
    if err:
        return error(err)
    return success(result)


# ── 宿舍区 ──────────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/dormitory', methods=['GET'])
@login_required
def get_dormitory():
    """按当前玩家关系优先级分页获取宿舍房间与床位。"""
    page, per_page = get_pagination_params(default_per_page=12)
    data, err = AdventurerS1Service.get_dormitory(
        user_id=request.current_user.id,
        page=page,
        per_page=per_page,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/rooms/<int:room_id>/donate-upgrade', methods=['POST'])
@login_required
@generic_anti_cheat_check('dormitory')
def donate_dorm_room_upgrade(room_id):
    """向宿舍 Lv2 升级捐献材料。body: {material_name, amount, adventurer_id?}"""
    body = request.get_json(silent=True) or {}
    try:
        amount = int(body.get('amount'))
    except (TypeError, ValueError):
        return error('参数不合法')
    material_name = body.get('material_name')
    if not isinstance(material_name, str) or not material_name:
        return error('material_name 必填')

    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)

    data, err = AdventurerS1Service.donate_to_dorm_room_upgrade(
        user_id=request.current_user.id,
        room_id=room_id,
        adventurer_id=adventurer_id,
        material_name=material_name,
        amount=amount,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/rooms/<int:room_id>/help-upgrade', methods=['POST'])
@login_required
def help_dorm_room_upgrade(room_id):
    """派遣冒险者帮工宿舍升级。body: {adventurer_id?}"""
    body = request.get_json(silent=True) or {}
    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)
    data, err = AdventurerS1Service.help_dorm_room_upgrade(
        room_id=room_id,
        user_id=request.current_user.id,
        adventurer_id=adventurer_id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/rooms/<int:room_id>/name', methods=['POST'])
@login_required
@generic_anti_cheat_check('social')
def name_dorm_room(room_id):
    """最大贡献者行使一次性命名权。body: {name: str, kind?: 'build'|'upgrade'}"""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return error('缺少 name')
    kind = (body.get('kind') or 'build').strip().lower()
    data, err = AdventurerS1Service.name_dorm_room(
        user_id=request.current_user.id,
        room_id=room_id,
        new_name=name,
        naming_kind=kind,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/move', methods=['POST'])
@login_required
@generic_anti_cheat_check('social')
def move_to_empty_bed():
    """搬到空床位。body: {adventurer_id: int, room_id: int, bed_index: int}"""
    body = request.get_json(silent=True) or {}
    adv_id = body.get('adventurer_id')
    room_id = body.get('room_id')
    bed_index = body.get('bed_index')
    if not adv_id or room_id is None or bed_index is None:
        return error('缺少 adventurer_id / room_id / bed_index')
    data, err = AdventurerS1Service.move_to_empty_bed(
        user_id=request.current_user.id,
        adv_id=adv_id, target_room_id=room_id, target_bed_index=bed_index,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/swap-request', methods=['POST'])
@login_required
@generic_anti_cheat_check('social')
def request_bed_swap():
    """发送床位互换请求。body: {adventurer_id: int, target_adventurer_id: int}"""
    body = request.get_json(silent=True) or {}
    adv_id = body.get('adventurer_id')
    target_id = body.get('target_adventurer_id')
    if not adv_id or not target_id:
        return error('缺少 adventurer_id 或 target_adventurer_id')
    data, err = AdventurerS1Service.request_bed_swap(
        user_id=request.current_user.id,
        requester_adv_id=adv_id, target_adv_id=target_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/swap-request/<int:request_id>/accept', methods=['POST'])
@login_required
def accept_bed_swap(request_id):
    """接受床位互换请求。"""
    data, err = AdventurerS1Service.accept_bed_swap(
        user_id=request.current_user.id, request_id=request_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/swap-request/<int:request_id>/reject', methods=['POST'])
@login_required
def reject_bed_swap(request_id):
    """拒绝床位互换请求。"""
    data, err = AdventurerS1Service.reject_bed_swap(
        user_id=request.current_user.id, request_id=request_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/swap-request/<int:request_id>/cancel', methods=['POST'])
@login_required
def cancel_bed_swap(request_id):
    """取消自己发起的床位互换请求。"""
    data, err = AdventurerS1Service.cancel_bed_swap(
        user_id=request.current_user.id, request_id=request_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/swap-requests', methods=['GET'])
@login_required
def get_swap_requests():
    """获取待处理的床位互换请求（incoming + outgoing）。"""
    data, err = AdventurerS1Service.get_pending_swap_requests(
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/rooms/<int:room_id>/upgrade', methods=['POST'])
@login_required
def upgrade_room(room_id):
    """材料捐满后开始宿舍 Lv2 升级。body: {adventurer_id?: int}"""
    body = request.get_json(silent=True) or {}
    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)
    data, err = AdventurerS1Service.upgrade_room(
        room_id,
        user_id=request.current_user.id,
        adventurer_id=adventurer_id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/my-status', methods=['GET'])
@login_required
def get_dormitory_my_status():
    """玩家宿舍楼面板状态：我的材料汇总 + 我申请中的实例 + 全服并发情况。

    一个接口拿齐前端宿舍区面板渲染所需的全部动态数据。
    """
    data, err = AdventurerS1Service.get_dormitory_my_status(
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/request', methods=['POST'])
@login_required
def request_new_dormitory():
    """玩家申请新建宿舍楼：等概率抽 1/2/4 床房型，创建 locked 实例。

    返回抽到的房型 + 材料需求，玩家凑齐材料后调 /dormitory/<id>/start 开工。
    """
    data, err = AdventurerS1Service.request_new_dormitory(
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/<int:building_instance_id>/donate', methods=['POST'])
@login_required
@generic_anti_cheat_check('dormitory')
def donate_to_dormitory(building_instance_id):
    """玩家向 locked 宿舍楼捐献材料。

    body: {amount: int, material_name: 'wood'/'stone', adventurer_id?: int}
    adventurer_id 可省略——默认取 owner 名下首个非死冒险者（MAX_PER_PLAYER=1 场景）。
    捐到所有材料都满时自动转 building，无需另外点开工。
    """
    body = request.get_json(silent=True) or {}
    try:
        amount = int(body.get('amount'))
    except (TypeError, ValueError):
        return error('参数不合法')
    material_name = body.get('material_name')
    if not isinstance(material_name, str) or not material_name:
        return error('material_name 必填')

    adventurer_id = body.get('adventurer_id')
    if adventurer_id is None:
        adventurer_id, err = AdventurerS1Service.resolve_active_adventurer_id(
            request.current_user.id,
        )
        if err:
            return error(err)
    else:
        try:
            adventurer_id = int(adventurer_id)
        except (TypeError, ValueError):
            return error('adventurer_id 不合法')

    data, err = AdventurerS1Service.donate_to_building(
        user_id=request.current_user.id,
        adventurer_id=adventurer_id,
        building_instance_id=building_instance_id,
        material_name=material_name,
        amount=amount,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/<int:building_instance_id>/cancel', methods=['POST'])
@login_required
def player_cancel_dormitory(building_instance_id):
    """玩家取消自己申请的 locked 阶段宿舍楼，按捐献明细原路退还材料。"""
    from plugins.adventurer_s1.models import AdventurerS1BuildingInstance

    inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
    if not inst:
        return error('建筑不存在', 404)
    if inst.applicant_user_id != request.current_user.id:
        return error('只能取消自己申请的宿舍楼', 403)
    data, err = AdventurerS1Service.cancel_dormitory_construction(
        admin_user_id=request.current_user.id,
        building_instance_id=building_instance_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/<int:building_instance_id>', methods=['DELETE'])
@login_required
@admin_required
def cancel_dormitory(building_instance_id):
    """管理员停止 locked 阶段的宿舍楼，按捐献明细原路退还材料。"""
    data, err = AdventurerS1Service.cancel_dormitory_construction(
        admin_user_id=request.current_user.id,
        building_instance_id=building_instance_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/dormitory/settings', methods=['GET'])
@login_required
def get_dormitory_settings():
    """读取宿舍楼相关运营参数（任何登录用户可读，用于前端展示当前上限）。"""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    is_admin = getattr(request.current_user, 'is_admin', False)
    settings = (
        S1SettingsService.list_all(include_admin_only=True)
        if is_admin else S1SettingsService.list_player_dormitory_settings()
    )
    return success({'settings': settings})


@adventurer_s1_bp.route('/dormitory/settings', methods=['PUT'])
@login_required
@admin_required
def update_dormitory_settings():
    """管理员修改宿舍楼运营参数。

    body: {key: str, value: any}
    """
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    body = request.get_json(silent=True) or {}
    key = body.get('key')
    if not isinstance(key, str) or not key:
        return error('key 必填')
    if key == 'region_2_enabled':
        return error('平台酒馆不支持旧区服准入设置')
    if 'value' not in body:
        return error('value 必填')
    ok, err = S1SettingsService.set(
        key=key, value=body['value'],
        updated_by_user_id=request.current_user.id,
    )
    if not ok:
        return error(err)
    return success({'message': '已更新', 'settings': S1SettingsService.list_all(include_admin_only=True)})


# ── 建筑系统 ────────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/buildings', methods=['GET'])
@login_required
def list_buildings():
    """获取建筑列表（普通玩家只看已开放/建造中，管理员可看 locked）。"""
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.get_buildings(
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return _conditional_get_success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>', methods=['GET'])
@login_required
def building_detail(building_id):
    """获取建筑详情（含帮忙者列表 + 今日使用次数）。"""
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.get_building_detail(
        building_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/contributors', methods=['GET'])

@login_required
def building_contributors(building_id):
    """获取建筑贡献者排行（帮忙时长 + 材料捐献）。"""
    data, err = AdventurerS1Service.get_building_contributors(building_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/start', methods=['POST'])
@login_required
@admin_required
def start_building(building_id):
    """管理员开始建造建筑。"""
    data, err = AdventurerS1Service.start_building(building_id, is_admin=True)
    if err:
        return error(err)
    return success(data)


def _resolve_adventurer_id_from_body(body, current_user_id):
    """从 body 取 adventurer_id，缺失时自动取 owner 首个非死冒险者。

    返回 (adventurer_id, err)，err 非空时调用方应直接返回 error(err)。
    """
    from plugins.adventurer_s1.models import AdventurerS1

    raw = body.get('adventurer_id')
    if raw is None:
        return AdventurerS1Service.resolve_active_adventurer_id(current_user_id)
    try:
        adv_id = int(raw)
    except (TypeError, ValueError):
        return None, 'adventurer_id 不合法'
    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return None, '冒险者不存在'
    err = AdventurerS1Service.check_owner_playable(adv, current_user_id)
    if err:
        return None, err
    return adv_id, None


@adventurer_s1_bp.route('/buildings/<int:building_id>/use-start', methods=['POST'])
@login_required
def start_building_usage(building_id):
    """派遣冒险者去使用已建成的建筑（伐木 / 采石 / 打工 / 修炼 等）。

    body: {adventurer_id?: int, target_traits?: str[], treatment_gold?: number, rental_minutes?: number,
           food_code?: str, skill_code?: str, operation?: "learn"|"advance", direction?: str,
           target_skill_id?: int, join_queue?: bool, cookware_code?: str, forge_intent?: str}
      — 省略 adventurer_id 时自动取 owner 首个非死冒险者
    target_traits: 治疗所必填，指定要治疗的特质名列表（当前限 1 个）
    item_ref: 治疗所 Lv2 可选，携带 1 件治疗物品辅助 {source: "carry"|"warehouse", id, name?}
    treatment_gold: 医院可选，自定义治疗花费金币（按 gold_cost_per_hp 向下取整到整点 HP）
    rental_minutes: 深渊手术室可选，自选驻留分钟（未传则按回满所需时长计费）
    food_code: 深渊食堂必填，餐品 code
    skill_code: 技能书阁经典传授必填，要学习的技能 code
    operation + direction: 技能书阁自定义研修必填；advance 还须传 target_skill_id
    join_queue: 导师或支持候场的异步设施按钮显式传 true；普通抢位失败不会自动入队
    cookware_code: 余烬灶坊必填，只接受建筑自带炊具的固定 code
    forge_intent: 铁匠铺可选，只接受服务端登记的固定锻造取向 code；省略时为自由创作
    settlement_config 决定本次使用消耗的精力、时长、属性需求、每日上限和产出奖励。
    """
    body = request.get_json(silent=True) or {}
    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)
    # 提取 extra_params（治疗所的 target_traits 等）
    extra_params = {}
    if 'target_traits' in body:
        extra_params['target_traits'] = body['target_traits']
    if body.get('equipment_name'):
        extra_params['equipment_name'] = body['equipment_name']
    if body.get('material_names'):
        extra_params['material_names'] = body['material_names']
    if body.get('item_refs'):
        extra_params['item_refs'] = body['item_refs']
    if body.get('cookware_code'):
        extra_params['cookware_code'] = body['cookware_code']
    if 'forge_intent' in body:
        extra_params['forge_intent'] = body['forge_intent']
    if body.get('layout_mode'):
        extra_params['layout_mode'] = body['layout_mode']
    if body.get('layout_slots'):
        extra_params['layout_slots'] = body['layout_slots']
    if 'desired_direction' in body:
        extra_params['desired_direction'] = body['desired_direction']
    if body.get('item_ref'):
        extra_params['item_ref'] = body['item_ref']
    if 'treatment_gold' in body:
        extra_params['treatment_gold'] = body['treatment_gold']
    if body.get('food_code'):
        extra_params['food_code'] = body['food_code']
    if body.get('skill_code'):
        extra_params['skill_code'] = body['skill_code']
    if 'operation' in body:
        extra_params['operation'] = body['operation']
    if 'direction' in body:
        extra_params['direction'] = body['direction']
    if 'target_skill_id' in body:
        extra_params['target_skill_id'] = body['target_skill_id']
    if body.get('join_queue') is True:
        extra_params['join_queue'] = True
    if 'seat_index' in body:
        extra_params['seat_index'] = body['seat_index']
    if 'rental_hours' in body:
        extra_params['rental_hours'] = body['rental_hours']
    if 'rental_minutes' in body:
        extra_params['rental_minutes'] = body['rental_minutes']
    if 'search_hours' in body:
        extra_params['search_hours'] = body['search_hours']
    if 'companion_id' in body:
        extra_params['companion_id'] = body['companion_id']
    if 'auxiliary_material_ref' in body:
        extra_params['auxiliary_material_ref'] = body['auxiliary_material_ref']
    data, err = AdventurerS1Service.start_building_usage(
        adventurer_id=adventurer_id,
        building_instance_id=building_id,
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
        extra_params=extra_params or None,
    )
    if err:
        return error(err)
    from plugins.adventurer_s1.services.s1_gambling_service import S1GamblingService
    S1GamblingService.maybe_emit_seat_changed(building_id)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/use-complete', methods=['POST'])
@login_required
def complete_building_usage(building_id):
    """完成建筑使用：结算奖励 + 释放冒险者回床位休息。

    body: {
        adventurer_id?: int,
        item_refs?: [{source: "carry" | "warehouse", name: str}], -- 铁匠铺：投入的两件物品
        equipment_name?: str,      -- 兼容旧字段
        material_names?: str[],    -- 兼容旧字段
    }
    省略 adventurer_id 时自动取 owner 首个非死冒险者。
    普通玩家不可手动提交异步 handler（治疗所/铁匠铺等）；到期提交与失败恢复
    统一由 scheduler / building AI recovery 处理。本接口继续服务同步建筑与后台流程。
    """
    body = request.get_json(silent=True) or {}
    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)

    # 提取异步 handler 额外参数
    extra_params = {}
    if body.get('equipment_name'):
        extra_params['equipment_name'] = body['equipment_name']
    if body.get('material_names'):
        extra_params['material_names'] = body['material_names']
    if body.get('item_refs'):
        extra_params['item_refs'] = body['item_refs']
    if body.get('cookware_code'):
        extra_params['cookware_code'] = body['cookware_code']
    if body.get('layout_mode'):
        extra_params['layout_mode'] = body['layout_mode']
    if body.get('layout_slots'):
        extra_params['layout_slots'] = body['layout_slots']
    if 'desired_direction' in body:
        extra_params['desired_direction'] = body['desired_direction']
    if body.get('target_traits'):
        extra_params['target_traits'] = body['target_traits']
    if 'companion_id' in body:
        extra_params['companion_id'] = body['companion_id']
    if 'auxiliary_material_ref' in body:
        extra_params['auxiliary_material_ref'] = body['auxiliary_material_ref']

    data, err = AdventurerS1Service.complete_building_usage(
        adventurer_id=adventurer_id,
        building_instance_id=building_id,
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
        extra_params=extra_params or None,
    )
    if err:
        return error(err)
    from plugins.adventurer_s1.services.s1_gambling_service import S1GamblingService
    S1GamblingService.maybe_emit_seat_changed(building_id)
    return success(data)


@adventurer_s1_bp.route('/adventurers/<int:adv_id>/building-ai-status', methods=['GET'])
@login_required
def building_ai_status(adv_id):
    """查询冒险者的建筑 AI 处理状态（治疗所/铁匠铺异步结果轮询用）。

    返回：
      - in_building: bool — 是否在建筑中等待 AI 结果
      - building_name: str | None — 建筑名称
      - handler_key: str | None — handler 类型
    """
    from plugins.adventurer_s1.models import AdventurerS1
    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在')

    in_building = adv.status == 'building' and adv.building_instance_id is not None
    building_name = None
    handler_key = None
    if in_building and adv.building_instance_id:
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, adv.building_instance_id)
        if inst and inst.template:
            building_name = inst.template.name
            handler_key = inst.template.handler_key

    return success({
        'in_building': in_building,
        'building_name': building_name,
        'handler_key': handler_key,
    })


@adventurer_s1_bp.route('/buildings/<int:building_id>/help', methods=['POST'])
@login_required
@generic_anti_cheat_check('social')
def help_building(building_id):
    """派遣冒险者帮忙建造。body: {adventurer_id?: int}  — 省略时自动取 owner 首个非死冒险者"""
    body = request.get_json(silent=True) or {}
    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)
    data, err = AdventurerS1Service.help_building(
        building_instance_id=building_id,
        adventurer_id=adventurer_id,
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/donate-upgrade', methods=['POST'])
@login_required
@generic_anti_cheat_check('dormitory')
def donate_building_upgrade(building_id):
    """向建筑 Lv2 升级捐献材料。body: {material_name, amount, adventurer_id?}"""
    body = request.get_json(silent=True) or {}
    try:
        amount = int(body.get('amount'))
    except (TypeError, ValueError):
        return error('参数不合法')
    material_name = body.get('material_name')
    if not isinstance(material_name, str) or not material_name:
        return error('material_name 必填')

    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)

    data, err = AdventurerS1Service.donate_to_building_upgrade(
        user_id=request.current_user.id,
        building_instance_id=building_id,
        adventurer_id=adventurer_id,
        material_name=material_name,
        amount=amount,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/upgrade', methods=['POST'])
@login_required
def upgrade_building(building_id):
    """材料捐满后开始升级已建成建筑。body: {adventurer_id?: int} — 省略时自动取 owner 首个非死冒险者。"""
    body = request.get_json(silent=True) or {}
    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)
    data, err = AdventurerS1Service.upgrade_building(
        building_instance_id=building_id,
        adventurer_id=adventurer_id,
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/recall/<int:adventurer_id>', methods=['POST'])
@login_required
def recall_from_building(adventurer_id):
    """召回正在建造的冒险者。"""
    data, err = AdventurerS1Service.recall_from_building(
        adventurer_id=adventurer_id,
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/dining-menu', methods=['GET'])
@login_required
def list_dining_menu(building_id):
    """列出深渊食堂菜单（需已建成且 handler=dining_hall）。"""
    from plugins.adventurer_s1.models import AdventurerS1BuildingInstance
    from plugins.adventurer_s1.services.s1_dining_service import S1DiningService

    inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_id)
    if not inst:
        return error('建筑不存在')
    if inst.status != 'built':
        return error('建筑尚未建成')
    handler_key = inst.template.handler_key if inst.template else None
    if handler_key != 'dining_hall':
        return error('该建筑不是食堂')
    return success({'items': S1DiningService.list_menu_items()})


@adventurer_s1_bp.route('/clue-bureau/targets', methods=['GET'])
@login_required
def list_clue_bureau_targets():
    """线索追查处追查池（独立于任务栏隐藏 Boss 讨伐进度）。"""
    from plugins.adventurer_s1.services.s1_clue_bureau_service import S1ClueBureauService

    return success(S1ClueBureauService.summarize_pool(for_client=True))


@adventurer_s1_bp.route('/admin/clue-bureau/trace-pool/preview', methods=['GET'])
@login_required
@admin_required
def admin_preview_clue_bureau_trace_pool():
    """预览线索追查池补充（dry-run）。"""
    from plugins.adventurer_s1.services.s1_clue_bureau_service import S1ClueBureauService

    data, err = S1ClueBureauService.admin_preview_supplement_trace_pool()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/clue-bureau/trace-pool/supplement', methods=['POST'])
@login_required
@admin_required
def admin_supplement_clue_bureau_trace_pool():
    """GM：将追查池补充至 cap（默认 10 条，受图鉴 Boss 总数上限）。"""
    from services.async_task_service import AsyncTaskService
    payload = {'updated_by_user_id': int(request.current_user.id)}
    if not AsyncTaskService.is_task_enqueued_or_running('ai_s1_clue_bureau_supplement', payload):
        if not AsyncTaskService.submit_task('ai_s1_clue_bureau_supplement', payload):
            return error('追查池补充任务入队失败，请稍后重试')
    return success({'queued': True}, message='追查池补充任务已入队')


@adventurer_s1_bp.route('/admin/clue-bureau/bosses/<boss_id>/release-lock', methods=['POST'])
@login_required
@admin_required
def admin_release_clue_bureau_boss_lock(boss_id):
    """GM：强制释放追查 Boss 的 active 锁定（探险失败等遗留）。"""
    from plugins.adventurer_s1.services.s1_clue_bureau_catalog_service import (
        S1ClueBureauCatalogService,
    )

    data, err = S1ClueBureauCatalogService.admin_force_release_active_lock(boss_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/parties/<int:party_id>/clue-progress', methods=['GET'])
@login_required
def party_hidden_boss_clue_progress(party_id):
    """当前小队针对某副本的线索集结进度（仅统计随身装备）。"""
    from plugins.adventurer_s1.models import AdventurerS1Party, AdventurerS1PartyMember
    from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_DUNGEONS
    from plugins.adventurer_s1.services.s1_hidden_boss_service import (
        S1HiddenBossService,
    )
    from sqlalchemy.orm import joinedload, selectinload
    from plugins.adventurer_s1.models import AdventurerS1

    target_dungeon = (request.args.get('dungeon') or '').strip()
    catalog_boss_id = (request.args.get('boss_id') or '').strip() or None
    if not target_dungeon:
        return error('缺少 dungeon 参数')
    if target_dungeon not in VALID_DUNGEON_NAMES:
        return error('未知副本')

    party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
    if not party:
        return error('小队不存在')

    members = [
        row.adventurer
        for row in (
            AdventurerS1PartyMember.query
            .filter_by(party_id=party_id)
            .options(
                joinedload(AdventurerS1PartyMember.adventurer)
                .selectinload(AdventurerS1.equipment_items)
            )
            .all()
        )
        if row.adventurer
    ]

    if catalog_boss_id:
        from plugins.adventurer_s1.services.s1_clue_bureau_catalog_service import (
            S1ClueBureauCatalogService,
        )
        boss = S1ClueBureauCatalogService.get_boss(catalog_boss_id)
        if not boss or boss.dungeon != target_dungeon:
            return error('未知追查 Boss')

    scanned = S1HiddenBossService.scan_party_clue_progress(
        members, target_dungeon, catalog_boss_id=catalog_boss_id,
    )
    clues = (scanned or {}).get('clues') or {}

    indices = sorted(int(k) for k in clues.keys())
    return success({
        'collected_indices': indices,
        'collected_count': len(indices),
        'required_count': 4,
        'ready': len(indices) >= 4,
        'entries': [
            {
                'index': entry.get('index'),
                'holder_name': entry.get('holder_name'),
            }
            for entry in sorted(clues.values(), key=lambda x: int(x.get('index') or 0))
        ],
    })


# ── 保险系统 ──────────────────────────────────────────────────────────────


@adventurer_s1_bp.route('/insurance/buy', methods=['POST'])
@login_required
@generic_anti_cheat_check('trade')
def buy_insurance():
    """为冒险者购买指定保险；不同保单允许同时生效。"""
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    insurance_type = str(body.get('insurance_type') or 'injury').strip()
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    from plugins.adventurer_s1.services.s1_insurance_service import S1InsuranceService
    ok, err = S1InsuranceService.buy_insurance(
        request.current_user.id,
        int(adventurer_id),
        insurance_type=insurance_type,
    )
    if err:
        return error(err)
    return success({'message': '购买成功，下次探险结算后将自动判定赔付'})


@adventurer_s1_bp.route('/insurance/status', methods=['GET'])
@login_required
def insurance_status():
    """查询冒险者的全部保单状态与赔付规则。"""
    adventurer_id = request.args.get('adventurer_id', type=int)
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    from plugins.adventurer_s1.services.s1_insurance_service import S1InsuranceService
    data, err = S1InsuranceService.get_status(request.current_user.id, adventurer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/shop-items', methods=['GET'])
@login_required
def list_shop_items(building_id):
    """列出商店建筑可售商品（草药店 / 军需店 / 熔炉杂货铺）。"""
    is_admin = getattr(request.current_user, 'is_admin', False)
    data, err = AdventurerS1Service.list_shop_items(
        building_instance_id=building_id,
        user_id=request.current_user.id,
        is_admin=is_admin,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/buy', methods=['POST'])
@login_required
@generic_anti_cheat_check('trade')
def buy_from_shop(building_id):
    """从商店建筑购物：用冒险者金币买一份 ShopItem，装入随身或仓库。

    body: {adventurer_id: int, shop_item_id: int}
    草药店 / 军需店 / 熔炉杂货铺；建筑必须 status='built'，无需派遣冒险者占位。
    """
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    shop_item_id = body.get('shop_item_id')
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    if not shop_item_id:
        return error('缺少 shop_item_id 参数')
    data, err = AdventurerS1Service.buy_from_shop(
        building_instance_id=building_id,
        adventurer_id=int(adventurer_id),
        shop_item_id=int(shop_item_id),
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/buyback-items', methods=['GET'])
@login_required
def list_buyback_items(building_id):
    """列出收购商行可出售物品（随身 + 仓库）及回收价预览。"""
    from plugins.adventurer_s1.services.s1_buyback_service import S1BuybackService

    data, err = S1BuybackService.list_sellable(
        building_instance_id=building_id,
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/sell', methods=['POST'])
@login_required
@generic_anti_cheat_check('trade')
def sell_to_buyback(building_id):
    """向收购商行出售物品。

    单卖 body: {adventurer_id: int, source: 'equipment'|'warehouse', item_id: int, quantity?: int}
    批量 body: {adventurer_id: int, items: [{source, item_id, quantity?}, ...]}
    """
    from plugins.adventurer_s1.services.s1_buyback_service import S1BuybackService

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error('请求体必须是 JSON 对象')
    adventurer_id = body.get('adventurer_id')
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    try:
        adventurer_id = int(adventurer_id)
    except (TypeError, ValueError):
        return error('adventurer_id 必须是正整数')
    if adventurer_id <= 0:
        return error('adventurer_id 必须是正整数')

    if 'items' in body:
        data, err = S1BuybackService.sell_items(
            building_instance_id=building_id,
            adventurer_id=adventurer_id,
            user_id=request.current_user.id,
            items=body.get('items'),
            is_admin=getattr(request.current_user, 'is_admin', False),
        )
        if err:
            return error(err)
        return success(data)

    source = body.get('source')
    item_id = body.get('item_id')
    quantity = body.get('quantity')
    if not source:
        return error('缺少 source 参数')
    if not item_id:
        return error('缺少 item_id 参数')
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return error('item_id 必须是正整数')
    if item_id <= 0:
        return error('item_id 必须是正整数')
    if quantity is not None:
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            return error('quantity 必须是正整数')
        if quantity <= 0:
            return error('quantity 必须是正整数')
    data, err = S1BuybackService.sell_item(
        building_instance_id=building_id,
        adventurer_id=adventurer_id,
        user_id=request.current_user.id,
        source=str(source),
        item_id=item_id,
        quantity=quantity,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/gambling/status', methods=['GET'])
@login_required
def gambling_status(building_id):
    """深渊赌坊状态：是否开市、荷官在岗人数、下注限额与今日剩余次数。"""
    from plugins.adventurer_s1.services.s1_gambling_service import S1GamblingService

    data, err = S1GamblingService.get_status(
        building_instance_id=building_id,
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/gambling/play', methods=['POST'])
@login_required
@generic_anti_cheat_check('trade')
def gambling_play(building_id):
    """深渊赌坊本轮下注（须已入座）。

    body: {adventurer_id: int, bet_amount: int, choice: 'high'|'low'}
    """
    from plugins.adventurer_s1.services.s1_gambling_service import S1GamblingService

    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    bet_amount = body.get('bet_amount')
    choice = body.get('choice')
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    if bet_amount is None:
        return error('缺少 bet_amount 参数')
    if not choice:
        return error('缺少 choice 参数')
    try:
        bet_amount = int(bet_amount)
    except (TypeError, ValueError):
        return error('bet_amount 必须是正整数')

    data, err = S1GamblingService.place_bet(
        building_instance_id=building_id,
        adventurer_id=int(adventurer_id),
        user_id=request.current_user.id,
        bet_amount=bet_amount,
        choice=str(choice),
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/gambling/tip', methods=['POST'])
@login_required
@generic_anti_cheat_check('trade')
def gambling_tip(building_id):
    """打赏在岗荷官（须已入座赌桌）。

    body: {adventurer_id: int, dealer_id: int, amount: int}
    """
    from plugins.adventurer_s1.services.s1_gambling_service import S1GamblingService

    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    dealer_id = body.get('dealer_id')
    amount = body.get('amount')
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    if not dealer_id:
        return error('缺少 dealer_id 参数')
    if amount is None:
        return error('缺少 amount 参数')
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return error('amount 必须是正整数')

    data, err = S1GamblingService.tip_dealer(
        building_instance_id=building_id,
        tipper_adventurer_id=int(adventurer_id),
        dealer_adventurer_id=int(dealer_id),
        user_id=request.current_user.id,
        amount=amount,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/lucky-wheel/pool', methods=['GET'])
@login_required
def lucky_wheel_pool(building_id):
    """幸运大转盘奖池预览（按稀有度分组）。"""
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService

    adventurer_id = request.args.get('adventurer_id', type=int)
    data, err = S1LuckyWheelService.get_pool_preview(
        building_instance_id=building_id,
        user_id=request.current_user.id,
        adventurer_id=adventurer_id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/hidden-dungeon-exchange/recipes', methods=['GET'])
@login_required
def hidden_dungeon_exchange_recipes(building_id):
    """查看当前冒险者的秘境藏品数量与可兑换配方。"""
    from plugins.adventurer_s1.services.s1_hidden_dungeon_exchange_service import (
        S1HiddenDungeonExchangeService,
    )

    adventurer_id = request.args.get('adventurer_id', type=int)
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    data, err = S1HiddenDungeonExchangeService.get_recipes(
        building_id,
        adventurer_id,
        request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/hidden-dungeon-exchange/redeem', methods=['POST'])
@login_required
@generic_anti_cheat_check('trade')
def hidden_dungeon_exchange_redeem(building_id):
    """按固定配方消耗普通物品并兑换一件珍贵物品。"""
    from plugins.adventurer_s1.services.s1_hidden_dungeon_exchange_service import (
        S1HiddenDungeonExchangeService,
    )

    body = request.get_json(silent=True) or {}
    try:
        adventurer_id = int(body.get('adventurer_id'))
    except (TypeError, ValueError):
        adventurer_id = 0
    recipe_id = str(body.get('recipe_id') or '').strip()
    if adventurer_id <= 0:
        return error('adventurer_id 参数无效')
    if not recipe_id:
        return error('缺少 recipe_id 参数')
    data, err = S1HiddenDungeonExchangeService.redeem(
        building_id,
        adventurer_id,
        request.current_user.id,
        recipe_id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/lucky-wheel/history', methods=['GET'])
@login_required
def lucky_wheel_history(building_id):
    """当前冒险者的大转盘成功抽取历史（分页）。"""
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService

    adventurer_id = request.args.get('adventurer_id', type=int)
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    data, err = S1LuckyWheelService.get_spin_history(
        building_instance_id=building_id,
        adventurer_id=adventurer_id,
        user_id=request.current_user.id,
        page=request.args.get('page', default=1, type=int),
        page_size=request.args.get('page_size', default=10, type=int),
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/lucky-wheel/spin', methods=['POST'])
@login_required
@generic_anti_cheat_check('trade')
def lucky_wheel_spin(building_id):
    """幸运大转盘抽奖。body: {adventurer_id: int, payment?: 'ticket'}"""
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService

    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    payment = body.get('payment', 'ticket')
    if not payment:
        payment = 'ticket'
    data, err = S1LuckyWheelService.spin(
        building_instance_id=building_id,
        adventurer_id=int(adventurer_id),
        user_id=request.current_user.id,
        payment=str(payment),
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/lucky-wheel/spin-batch', methods=['POST'])
@login_required
@generic_anti_cheat_check('trade')
def lucky_wheel_spin_batch(building_id):
    """抽奖券连抽，按已查看的抽取记录版本防止重复提交。"""
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求参数须为对象')
    adventurer_id = body.get('adventurer_id')
    if type(adventurer_id) is not int or adventurer_id <= 0:
        return error('adventurer_id 须为正整数')
    data, err = S1LuckyWheelService.spin_batch(
        building_id, adventurer_id, request.current_user.id,
        count=body.get('count'), expected_spin_id=body.get('expected_spin_id'),
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


# ── 管理员：幸运大转盘奖池管理 ──────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/lucky-wheel/pool', methods=['GET'])
@login_required
@admin_required
def admin_lucky_wheel_pool():
    """管理员查看完整奖池（基础 + 自定义，按稀有度分组）。"""
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService
    data, err = S1LuckyWheelService.get_admin_pool_overview()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/lucky-wheel/pool', methods=['POST'])
@login_required
@admin_required
def admin_lucky_wheel_add_item():
    """管理员添加奖池物品。

    自定义：body {name, description, rarity, item_type?, world_unique?, text_locked?, legendary_position?}
    图鉴：body {from_catalog: true, name, rarity, text_locked?, legendary_position?}
    """
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService
    body = request.get_json(silent=True) or {}
    try:
        uses_left, uses_left_provided = _parse_admin_uses_left(body)
    except ValueError as exc:
        return error(str(exc))
    if body.get('from_catalog'):
        data, err = S1LuckyWheelService.add_catalog_pool_item(
            name=body.get('name', ''),
            rarity=body.get('rarity', 'common'),
            legendary_position=body.get('legendary_position', 'next'),
            text_locked=bool(body.get('text_locked', False)),
            uses_left=uses_left,
            uses_left_provided=uses_left_provided,
        )
    else:
        data, err = S1LuckyWheelService.add_custom_pool_item(
            name=body.get('name', ''),
            description=body.get('description', ''),
            rarity=body.get('rarity', 'common'),
            item_type=body.get('item_type', 'special'),
            world_unique=(
                bool(body.get('world_unique'))
                if 'world_unique' in body
                else None
            ),
            legendary_position=body.get('legendary_position', 'next'),
            text_locked=bool(body.get('text_locked', False)),
            uses_left=uses_left,
            uses_left_provided=uses_left_provided,
        )
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/admin/lucky-wheel/pool/<item_name>', methods=['DELETE'])
@login_required
@admin_required
def admin_lucky_wheel_remove_item(item_name):
    """管理员从自定义奖池移除物品（不删图鉴）。query: rarity"""
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService
    rarity = request.args.get('rarity', '')
    data, err = S1LuckyWheelService.remove_custom_pool_item(item_name, rarity)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/lucky-wheel/pool/<item_name>', methods=['PUT'])
@login_required
@admin_required
def admin_lucky_wheel_edit_item(item_name):
    """管理员编辑奖池物品属性（同步更新图鉴）。

    body: {rarity: str, new_name?: str, description?: str, item_type?: str, world_unique?: bool, text_locked?: bool}
    """
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService
    body = request.get_json(silent=True) or {}
    try:
        uses_left, uses_left_provided = _parse_admin_uses_left(body)
    except ValueError as exc:
        return error(str(exc))
    data, err = S1LuckyWheelService.edit_pool_item(
        old_name=item_name,
        old_rarity=body.get('rarity', ''),
        new_name=body.get('new_name', ''),
        description=body.get('description'),
        item_type=body.get('item_type'),
        world_unique=body.get('world_unique'),
        text_locked=body.get('text_locked'),
        uses_left=uses_left,
        uses_left_provided=uses_left_provided,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/lucky-wheel/pool/<item_name>/restore', methods=['POST'])
@login_required
@admin_required
def admin_lucky_wheel_restore_item(item_name):
    """管理员恢复被排除的基础物品。query: rarity"""
    from plugins.adventurer_s1.services.s1_lucky_wheel_service import S1LuckyWheelService
    rarity = request.args.get('rarity', '')
    data, err = S1LuckyWheelService.restore_base_pool_item(item_name, rarity)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/tombstones', methods=['GET'])
@login_required
def list_building_tombstones(building_id):
    """英灵碑林：分页列出阵亡冒险者墓碑。"""
    from plugins.adventurer_s1.services.s1_tombstone_service import S1TombstoneService

    page = request.args.get('page', default=1, type=int)
    page_size = request.args.get('page_size', default=20, type=int)
    mine_only = request.args.get('mine', default='0') in ('1', 'true', 'yes')
    search = request.args.get('search', '').strip()
    data, err = S1TombstoneService.list_tombstones(
        building_instance_id=building_id,
        page=page,
        page_size=page_size,
        owner_id=request.current_user.id,
        mine_only=mine_only,
        search=search,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/tombstones/<int:tombstone_id>/messages', methods=['GET'])
@login_required
def list_tombstone_messages(building_id, tombstone_id):
    """英灵碑林：分页读取某块墓碑下的留言。"""
    from plugins.adventurer_s1.services.s1_tombstone_service import S1TombstoneService

    page = request.args.get('page', default=1, type=int)
    page_size = request.args.get('page_size', default=20, type=int)
    data, err = S1TombstoneService.list_messages(
        building_instance_id=building_id,
        tombstone_id=tombstone_id,
        page=page,
        page_size=page_size,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/tombstones/<int:tombstone_id>/messages', methods=['POST'])
@login_required
@generic_anti_cheat_check('chat')
def post_tombstone_message(building_id, tombstone_id):
    """英灵碑林：在某块墓碑下留言。body: {content: str}"""
    from plugins.adventurer_s1.services.s1_tombstone_service import S1TombstoneService

    body = request.get_json(silent=True) or {}
    content = body.get('content')
    if content is None:
        return error('缺少 content 参数')
    data, err = S1TombstoneService.post_message(
        building_instance_id=building_id,
        tombstone_id=tombstone_id,
        user_id=request.current_user.id,
        content=str(content),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/tombstones/<int:tombstone_id>/tributes', methods=['GET'])
@login_required
def list_tombstone_tributes(building_id, tombstone_id):
    """英灵碑林：分页读取某块墓碑的献花记录。"""
    from plugins.adventurer_s1.services.s1_tombstone_service import S1TombstoneService

    page = request.args.get('page', default=1, type=int)
    page_size = request.args.get('page_size', default=20, type=int)
    data, err = S1TombstoneService.list_tributes(
        building_instance_id=building_id,
        tombstone_id=tombstone_id,
        page=page,
        page_size=page_size,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/tombstones/<int:tombstone_id>/tributes', methods=['POST'])
@login_required
@generic_anti_cheat_check('chat')
def post_tombstone_tribute(building_id, tombstone_id):
    """英灵碑林：从鲜花店购买鲜花并送到墓碑。body: {adventurer_id, flower_code, quantity, message?}"""
    from plugins.adventurer_s1.services.s1_tombstone_service import S1TombstoneService

    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    flower_code = body.get('flower_code')
    quantity = body.get('quantity')
    message = body.get('message')
    if adventurer_id is None or flower_code is None or quantity is None:
        return error('缺少 adventurer_id / flower_code / quantity 参数')
    try:
        parsed_adventurer_id = int(adventurer_id)
        parsed_quantity = int(quantity)
    except (TypeError, ValueError):
        return error('adventurer_id / quantity 必须为整数')
    data, err = S1TombstoneService.post_tribute(
        building_instance_id=building_id,
        tombstone_id=tombstone_id,
        user_id=request.current_user.id,
        adventurer_id=parsed_adventurer_id,
        flower_code=str(flower_code),
        quantity=parsed_quantity,
        message=message,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/flower-catalog', methods=['GET'])
@login_required
def list_flower_catalog():
    """返回鲜花目录、鲜花店状态和可支付金币的本人冒险者。"""
    from plugins.adventurer_s1.services.s1_tombstone_service import S1TombstoneService

    data, err = S1TombstoneService.list_flower_catalog_and_inventory(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/skill-school/catalog', methods=['GET'])
@login_required
def list_skill_school_catalog(building_id):
    """列出经典传授目录，以及自定义学习/进阶配置、技能和冷却状态。"""
    from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService

    data, err = S1SkillSchoolService.list_teachable(
        building_instance_id=building_id,
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/buildings/<int:building_id>/skill-school/retry', methods=['POST'])
@login_required
def retry_skill_school_training(building_id):
    """使用当前玩家服务端留存的原课堂凭据，不接受客户端替换研修参数。"""
    from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService

    data, err = S1SkillSchoolService.retry_custom_training(
        building_instance_id=building_id,
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='导师已按上回练法直接开始补课')


# ── 任务栏接口 ─────────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/buildings/<int:building_id>/skill-school/queue/cancel', methods=['POST'])
@login_required
def cancel_skill_school_queue(building_id):
    """取消当前玩家冒险者尚未入席的导师指点候场。"""
    body = request.get_json(silent=True) or {}
    adventurer_id, err = _resolve_adventurer_id_from_body(body, request.current_user.id)
    if err:
        return error(err)
    from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService
    data, err = S1SkillSchoolService.cancel_custom_queue(
        adventurer_id,
        building_id,
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已取消导师候场，束脩与精力均已退回')


@adventurer_s1_bp.route('/buildings/<int:building_id>/usage-queue/cancel', methods=['POST'])
@login_required
def cancel_building_usage_queue(building_id):
    """取消当前玩家冒险者尚未入席的异步设施候场。"""
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    elif not isinstance(body, dict):
        return error('请求体必须是 JSON 对象')
    adventurer_id, err = _resolve_adventurer_id_from_body(
        body,
        request.current_user.id,
    )
    if err:
        return error(err)
    from plugins.adventurer_s1.services.s1_building_usage_queue_service import (
        S1BuildingUsageQueueService,
    )
    data, err = S1BuildingUsageQueueService.cancel_queue(
        adventurer_id,
        building_id,
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data, message='已取消设施候场，预留费用与精力均已退回')


@adventurer_s1_bp.route('/tasks', methods=['GET'])
@login_required
def list_tasks():
    """获取可用任务列表。adventurer_id 可选，传入时标记是否满足接取条件。"""
    adventurer_id = request.args.get('adventurer_id', type=int)
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.get_player_task_board_data(
        request.current_user.id, adventurer_id,
    )
    if err:
        return error(err)
    return success(data['tasks'])


@adventurer_s1_bp.route('/tasks/accept', methods=['POST'])
@login_required
def accept_task():
    """接取任务。body: {task_id: str, adventurer_id: int, building_instance_id: int}"""
    body = request.get_json(silent=True) or {}
    task_id = body.get('task_id')
    adventurer_id = body.get('adventurer_id')
    building_instance_id = body.get('building_instance_id')
    if not task_id:
        return error('缺少 task_id 参数')
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    if not building_instance_id:
        return error('缺少 building_instance_id 参数')
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.accept_task(
        adventurer_id=int(adventurer_id),
        task_template_id=str(task_id),
        building_instance_id=int(building_instance_id),
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tasks/complete', methods=['POST'])
@login_required
def complete_task():
    """完成任务。body: {task_instance_id: int}"""
    body = request.get_json(silent=True) or {}
    task_instance_id = body.get('task_instance_id')
    if not task_instance_id:
        return error('缺少 task_instance_id 参数')
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.complete_task(
        task_instance_id=int(task_instance_id),
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tasks/abandon', methods=['POST'])
@login_required
def abandon_task():
    """放弃进行中的任务。body: {task_instance_id: int}"""
    body = request.get_json(silent=True) or {}
    task_instance_id = body.get('task_instance_id')
    if not task_instance_id:
        return error('缺少 task_instance_id 参数')
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.abandon_task(
        task_instance_id=int(task_instance_id),
        user_id=request.current_user.id,
        is_admin=getattr(request.current_user, 'is_admin', False),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tasks/active', methods=['GET'])
@login_required
def active_task():
    """获取冒险者的活跃任务。"""
    adventurer_id = request.args.get('adventurer_id', type=int)
    if not adventurer_id:
        return error('缺少 adventurer_id 参数')
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.get_player_task_board_data(
        request.current_user.id, adventurer_id, include_tasks=False,
    )
    if err:
        return error(err)
    return success(data['active_task'] or {})


@adventurer_s1_bp.route('/admin/tasks', methods=['GET'])
@login_required
@admin_required
def admin_list_tasks():
    """管理员：查看全部任务栏模板及运营覆盖配置。"""
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.admin_list_tasks()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/tasks/<task_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_task(task_id):
    """管理员：更新任务启用状态与可运营字段。

    body: { enabled?: bool, overrides?: { name, description, max_completions, rewards, ... } }
    """
    body = request.get_json(silent=True) or {}
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.admin_update_task(
        str(task_id),
        body,
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/tasks/<task_id>/reenable', methods=['POST'])
@login_required
@admin_required
def admin_reenable_task(task_id):
    """管理员：发布任务新版本（新 ID，名额从零计）。

    body: { max_completions?: int }
    """
    body = request.get_json(silent=True) or {}
    max_completions = body.get('max_completions')
    from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
    data, err = AdventurerS1TaskService.admin_reenable_task(
        str(task_id),
        user_id=request.current_user.id,
        max_completions=max_completions,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-instances/<int:instance_id>/rename', methods=['POST'])
@login_required
@admin_required
def admin_rename_building_instance(instance_id):
    """管理员重命名建筑实例。body: {custom_name: str}"""
    body = request.get_json(silent=True) or {}
    custom_name = body.get('custom_name', '').strip()
    data, err = AdventurerS1Service.rename_building_instance(instance_id, custom_name)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-instances', methods=['POST'])
@login_required
@admin_required
def admin_create_building_instance():
    """管理员创建建筑实例（仅限可多个建筑类型）。body: {template_id: int}"""
    body = request.get_json(silent=True) or {}
    template_id = body.get('template_id')
    if not template_id:
        return error('缺少 template_id 参数')
    data, err = AdventurerS1Service.create_building_instance(int(template_id))
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/admin/building-instances/<int:instance_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_building_instance(instance_id):
    """管理员删除建筑实例（仅限可多个建筑类型）。"""
    data, err = AdventurerS1Service.delete_building_instance(instance_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-instances/<int:instance_id>/toggle-disable', methods=['POST'])
@login_required
@admin_required
def admin_toggle_building_disable(instance_id):
    """GM 切换建筑实例禁用/启用状态。"""
    data, err = AdventurerS1Service.toggle_building_disabled(instance_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/lv2-orphan-instances/preview', methods=['GET'])
@login_required
@admin_required
def admin_preview_orphan_lv2_instances():
    """预览将被清理的 Lv2 锁定建筑无主残留实例（dry-run）。"""
    data, err = AdventurerS1Service.admin_preview_orphan_lv2_instances()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/lv2-orphan-instances/purge', methods=['POST'])
@login_required
@admin_required
def admin_purge_orphan_lv2_instances():
    """清理 Lv2 锁定建筑的无主残留实例（酒馆未升 Lv2 时的脏数据，只删无帮工 + 无捐献）。"""
    data, err = AdventurerS1Service.admin_purge_orphan_lv2_instances()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/lv2-locked-donations/preview', methods=['GET'])
@login_required
@admin_required
def admin_preview_lv2_locked_donation_instances():
    """预览酒馆未升 Lv2 时误开放捐献的 Lv2 锁定建筑实例（dry-run）。"""
    data, err = AdventurerS1Service.admin_preview_lv2_locked_donation_instances()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-instances/<int:instance_id>/refund-lv2-locked', methods=['POST'])
@login_required
@admin_required
def admin_refund_lv2_locked_building_donations(instance_id):
    """退还 Lv2 锁定建筑误捐材料并删除实例，恢复未解锁态。"""
    data, err = AdventurerS1Service.admin_refund_lv2_locked_building_donations(
        admin_user_id=request.current_user.id,
        building_instance_id=instance_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-instances/all', methods=['GET'])
@login_required
@admin_required
def admin_list_all_building_instances():
    """管理员查看所有建筑实例（含已禁用）。"""
    data, err = AdventurerS1Service.get_all_building_instances_for_admin()
    if err:
        return error(err)
    return success(data)


# ── 管理员：建筑模板管理 ────────────────────────────────────────────────────

@adventurer_s1_bp.route('/admin/limited-shops', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_limited_shops():
    from plugins.adventurer_s1.services.s1_limited_shop_admin_service import list_shops, save_shop
    if request.method == 'GET':
        return success(list_shops(max(1, request.args.get('page', 1, type=int))))
    data, err = save_shop(request.get_json(silent=True))
    return error(err) if err else created(data)


@adventurer_s1_bp.route('/admin/limited-shops/<int:template_id>', methods=['GET', 'PUT'])
@login_required
@admin_required
def admin_limited_shop_detail(template_id):
    from plugins.adventurer_s1.services.s1_limited_shop_admin_service import get_shop, save_shop
    if request.method == 'GET':
        data, err = get_shop(template_id)
    else:
        data, err = save_shop(request.get_json(silent=True), template_id)
    return error(err) if err else success(data)


@adventurer_s1_bp.route('/admin/limited-shop-mooncakes', methods=['GET'])
@login_required
@admin_required
def admin_limited_shop_mooncakes():
    from flask import current_app
    from plugins.adventurer_s1.services.s1_mooncake_catalog import mooncake_catalog
    return success({
        'name': '月满归途·中秋限时摊位', 'code': 'midautumn_20260925',
        'description': '月圆人安，旅途有甜。挑一枚喜欢的月饼，带着酒馆的祝福出发吧。',
        'starts_at': current_app.config['ADVENTURER_S1_MIDAUTUMN_START'],
        'ends_at': current_app.config['ADVENTURER_S1_MIDAUTUMN_END'],
        'items': [{'preset_name': name, 'name': name, 'description': item['description'],
                   'rarity': item['rarity'], 'immutable': True,
                   'price': current_app.config['ADVENTURER_S1_MIDAUTUMN_PRICE'],
                   'stock': None, 'stock_delta': 0}
                  for name, item in mooncake_catalog().items()],
    })

@adventurer_s1_bp.route('/admin/building-handlers', methods=['GET'])
@login_required
@admin_required
def admin_list_building_handlers():
    """返回所有可用的建筑处理器列表，供 GM 新建模板时选择。"""
    from plugins.adventurer_s1.services.adventurer_s1_building_handler import get_all_handler_info
    return success(get_all_handler_info())


@adventurer_s1_bp.route('/admin/building-templates', methods=['GET'])
@login_required
@admin_required
def admin_list_building_templates():
    """管理员查看所有建筑模板。"""
    data, err = AdventurerS1Service.list_building_templates()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-templates', methods=['POST'])
@login_required
@admin_required
def admin_create_building_template():
    """管理员新建建筑模板。"""
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1Service.create_building_template(
        code=body.get('code', ''),
        name=body.get('name', ''),
        icon=body.get('icon', '🏗️'),
        description=body.get('description', ''),
        building_type=body.get('building_type', 'unique'),
        category=body.get('category', 'general'),
        build_hours=body.get('build_hours', 4),
        helper_slots=body.get('helper_slots', 2),
        stamina_cost_per_help=body.get('stamina_cost_per_help', 20),
        speedup_minutes=body.get('speedup_minutes', 15),
        sort_order=body.get('sort_order', 0),
        handler_key=body.get('handler_key'),
    )
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/admin/building-templates/<int:template_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_building_template(template_id):
    """管理员编辑建筑模板。"""
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1Service.update_building_template(
        template_id=template_id,
        name=body.get('name'),
        icon=body.get('icon'),
        description=body.get('description'),
        building_type=body.get('building_type'),
        category=body.get('category'),
        build_hours=body.get('build_hours'),
        helper_slots=body.get('helper_slots'),
        stamina_cost_per_help=body.get('stamina_cost_per_help'),
        speedup_minutes=body.get('speedup_minutes'),
        sort_order=body.get('sort_order'),
        is_active=body.get('is_active'),
        handler_key=body.get('handler_key'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-templates/<int:template_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_building_template(template_id):
    """管理员删除建筑模板（同时删除对应实例和帮忙记录）。"""
    data, err = AdventurerS1Service.delete_building_template(template_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-instances/<int:instance_id>/reset', methods=['POST'])
@login_required
@admin_required
def admin_reset_building(instance_id):
    """管理员重置建筑实例状态为 locked。"""
    data, err = AdventurerS1Service.reset_building(instance_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-instances/<int:instance_id>/complete', methods=['POST'])
@login_required
@admin_required
def admin_complete_building(instance_id):
    """GM 一键建成：把 building 中的建筑立即置为 built（释放在位冒险者、补建宿舍房间）。"""
    data, err = AdventurerS1Service.complete_building(instance_id, is_admin=True)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/building-instances/<int:instance_id>/fill-materials', methods=['POST'])
@login_required
@admin_required
def admin_fill_building_materials(instance_id):
    """GM 一键补满材料：把 locked 宿舍楼缺的材料加到申请人第一个非死亡冒险者头上。"""
    data, err = AdventurerS1Service.fill_dormitory_materials(instance_id, is_admin=True)
    if err:
        return error(err)
    return success(data)


# ── 货币系统 ────────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/<int:adv_id>/currencies', methods=['GET'])
@login_required
def get_adventurer_currencies(adv_id):
    """查询冒险者所有货币余额。"""
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)
    is_admin = bool(getattr(request.current_user, 'is_admin', False))
    if adv.owner_id != request.current_user.id and not is_admin:
        return error('只能查看自己冒险者的货币', 403)
    data, err = S1CurrencyService.get_all_balances(adv_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/currencies/<currency_code>/logs', methods=['GET'])
@login_required
def get_adventurer_currency_logs(adv_id, currency_code):
    """查询冒险者某货币的变动日志。"""
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return error('冒险者不存在', 404)
    is_admin = bool(getattr(request.current_user, 'is_admin', False))
    if adv.owner_id != request.current_user.id and not is_admin:
        return error('只能查看自己冒险者的货币流水', 403)
    limit = request.args.get('limit', 50, type=int)
    data, err = S1CurrencyService.get_logs(adv_id, currency_code=currency_code, limit=limit)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/currencies/adjust', methods=['POST'])
@login_required
@admin_required
def admin_adjust_currency(adv_id):
    """管理员调整冒险者货币。body: {currency_code, amount, description, silent?}。正数增加，负数扣除。"""
    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
    body = request.get_json(silent=True) or {}
    currency_code = body.get('currency_code', '')
    amount = body.get('amount', 0)
    description = body.get('description', '管理员调整')
    silent = bool(body.get('silent'))

    if not currency_code:
        return error('缺少 currency_code')
    if not isinstance(amount, (int, float)) or amount == 0:
        return error('金额不能为0')

    if amount > 0:
        data, err = S1CurrencyService.add(
            adv_id, currency_code, amount,
            source_type='admin_adjust', description=description, silent=silent,
        )
    else:
        data, err = S1CurrencyService.deduct(
            adv_id, currency_code, abs(amount),
            source_type='admin_adjust', description=description, silent=silent,
        )
    if err:
        return error(err)
    return success({'new_balance': data})


@adventurer_s1_bp.route('/currency-types', methods=['GET'])
def get_currency_types():
    """获取所有货币类型定义（无需登录）。"""
    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
    data, err = S1CurrencyService.get_currency_types()
    if err:
        return error(err)
    return success(data)


# ── 个人交易市场（以物易物 + 卖钱） ────────────────────────────────────────

@adventurer_s1_bp.route('/market/listings', methods=['GET'])
@login_required
def market_listings():
    """市场在售挂单列表（分页，可按 rarity / listing_type / q 过滤），附手续费表和卖钱价格范围。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    page, per_page = get_pagination_params(default_per_page=20)
    rarity = request.args.get('rarity') or None
    listing_type = request.args.get('listing_type') or None
    search = request.args.get('q') or request.args.get('search') or None
    data, err = S1MarketService.list_listings(
        page, per_page, rarity=rarity, listing_type=listing_type, search=search,
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/my', methods=['GET'])
@login_required
def market_my_listings():
    """我的挂单：在售全部 + 最近成交/取消回执。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    data, err = S1MarketService.get_my_listings(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/listings', methods=['POST'])
@generic_anti_cheat_check('trade')
@login_required
def market_create_listing():
    """挂单。body: {adventurer_id, warehouse_item_id, listing_type?, price?, want_note?}。

    listing_type: 'barter'（默认，以物易物）或 'sell'（卖钱，需传 price）。
    want_note: barter 模式卖家备注想要什么（可选，max 200 字符）。
    """
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    warehouse_item_id = body.get('warehouse_item_id') or body.get('item_id')
    item_name = (body.get('item_name') or '').strip() or None
    listing_type = (body.get('listing_type') or 'barter').strip()
    price = body.get('price')
    want_note = body.get('want_note')
    quantity = body.get('quantity')
    if not isinstance(adventurer_id, int) or (warehouse_item_id is None and not item_name):
        return error('缺少 adventurer_id 或 warehouse_item_id')
    data, err = S1MarketService.create_listing(
        request.current_user.id, adventurer_id,
        warehouse_item_id=warehouse_item_id, item_name=item_name,
        listing_type=listing_type, price=price, want_note=want_note,
        quantity=quantity,
    )
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/market/listings/<int:listing_id>/cancel', methods=['POST'])
@login_required
def market_cancel_listing(listing_id):
    """取消自己的挂单，物品退回仓库。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    data, err = S1MarketService.cancel_listing(request.current_user.id, listing_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/listings/<int:listing_id>/exchange', methods=['POST'])
@generic_anti_cheat_check('trade')
@login_required
def market_exchange(listing_id):
    """用同稀有度物品交换挂单物品（barter 模式）。body: {adventurer_id, offered_warehouse_item_id, quantity?}"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    offered_warehouse_item_id = body.get('offered_warehouse_item_id') or body.get('offered_item_id')
    offered_item_name = (body.get('offered_item_name') or '').strip() or None
    quantity = body.get('quantity')
    if not isinstance(adventurer_id, int) or (offered_warehouse_item_id is None and not offered_item_name):
        return error('缺少 adventurer_id 或 offered_warehouse_item_id')
    if quantity is not None and not isinstance(quantity, int):
        return error('quantity 须为整数')
    data, err = S1MarketService.exchange(
        request.current_user.id, adventurer_id, listing_id,
        offered_warehouse_item_id=offered_warehouse_item_id,
        offered_item_name=offered_item_name,
        quantity=quantity,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/listings/<int:listing_id>/buy', methods=['POST'])
@generic_anti_cheat_check('trade')
@login_required
def market_buy_listing(listing_id):
    """用金币购买 sell 模式的挂单物品。body: {adventurer_id, quantity?}。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not isinstance(adventurer_id, int):
        return error('缺少 adventurer_id')
    quantity = body.get('quantity')
    if quantity is not None and not isinstance(quantity, int):
        return error('quantity 须为整数')
    data, err = S1MarketService.buy_listing(
        request.current_user.id, adventurer_id, listing_id, quantity=quantity,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/slot-info', methods=['GET'])
@login_required
def market_slot_info():
    """获取冒险者的挂单格子信息。query: adventurer_id"""
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    adventurer_id = request.args.get('adventurer_id', type=int)
    if not adventurer_id:
        return error('缺少 adventurer_id')
    # 校验归属
    adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
    if not adv or adv.owner_id != request.current_user.id:
        return error('无权操作此冒险者')
    data, err = S1MarketService.get_slot_info(adventurer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/purchase-slots', methods=['POST'])
@login_required
def market_purchase_slots():
    """购买额外挂单格子。body: {adventurer_id}"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not isinstance(adventurer_id, int):
        return error('缺少 adventurer_id')
    data, err = S1MarketService.purchase_listing_slots(request.current_user.id, adventurer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/listings/<int:listing_id>', methods=['PATCH'])
@login_required
def market_update_want_note(listing_id):
    """卖家修改 barter 挂单的「想要什么」备注。body: {want_note}"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    body = request.get_json(silent=True) or {}
    want_note = body.get('want_note', '')
    data, err = S1MarketService.update_want_note(request.current_user.id, listing_id, want_note)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/listings/<int:listing_id>/offers', methods=['POST'])
@generic_anti_cheat_check('trade')
@login_required
def market_submit_offer(listing_id):
    """买家对 barter 挂单提交出价。body: {adventurer_id, offered_warehouse_item_id, quantity?}"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    offered_warehouse_item_id = body.get('offered_warehouse_item_id') or body.get('offered_item_id')
    offered_item_name = (body.get('offered_item_name') or '').strip() or None
    quantity = body.get('quantity')
    if not isinstance(adventurer_id, int) or (offered_warehouse_item_id is None and not offered_item_name):
        return error('缺少 adventurer_id 或 offered_warehouse_item_id')
    if quantity is not None and not isinstance(quantity, int):
        return error('quantity 须为整数')
    data, err = S1MarketService.submit_offer(
        request.current_user.id, adventurer_id, listing_id,
        offered_warehouse_item_id=offered_warehouse_item_id,
        offered_item_name=offered_item_name,
        quantity=quantity,
    )
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/market/listings/<int:listing_id>/offers', methods=['GET'])
@login_required
def market_get_offers(listing_id):
    """卖家查看某挂单的所有出价。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    data, err = S1MarketService.get_offers_for_listing(request.current_user.id, listing_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/offers/<int:offer_id>/cancel', methods=['POST'])
@login_required
def market_cancel_offer(offer_id):
    """买家取消自己 pending 状态的出价。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    data, err = S1MarketService.cancel_offer(request.current_user.id, offer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/listings/<int:listing_id>/offers/<int:offer_id>/accept', methods=['POST'])
@login_required
def market_accept_offer(listing_id, offer_id):
    """卖家选定出价，原子完成物品互转。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    data, err = S1MarketService.accept_offer(request.current_user.id, listing_id, offer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/listings/<int:listing_id>/offers/<int:offer_id>/reject', methods=['POST'])
@login_required
def market_reject_offer(listing_id, offer_id):
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    data, err = S1MarketService.reject_offer(request.current_user.id, listing_id, offer_id)
    return error(err) if err else success(data)


@adventurer_s1_bp.route('/market/buy-orders/<int:order_id>/offers/<int:offer_id>/reject', methods=['POST'])
@login_required
def market_reject_buy_offer(order_id, offer_id):
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    data, err = S1MarketBuyOrderService.reject_offer(request.current_user.id, order_id, offer_id)
    return error(err) if err else success(data)


@adventurer_s1_bp.route('/market/my-offers', methods=['GET'])
@login_required
def market_my_offers():
    """买家查看自己的出价记录。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
    data, err = S1MarketService.get_my_offers(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/buy-orders', methods=['GET'])
@login_required
def market_buy_orders():
    """求购单列表（分页，可按 rarity / q 过滤）。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    page, per_page = get_pagination_params(default_per_page=20)
    rarity = request.args.get('rarity') or None
    search = request.args.get('q') or request.args.get('search') or None
    data, err = S1MarketBuyOrderService.list_orders(
        page,
        per_page,
        rarity=rarity,
        search=search,
        user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/buy-orders', methods=['POST'])
@generic_anti_cheat_check('trade')
@login_required
def market_create_buy_order():
    """发布求购。body: {adventurer_id, prices, note?, keep_open?}"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not isinstance(adventurer_id, int):
        return error('缺少 adventurer_id')
    data, err = S1MarketBuyOrderService.create_order(
        request.current_user.id,
        adventurer_id,
        body.get('prices'),
        note=body.get('note'),
        keep_open=body.get('keep_open', False),
    )
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/market/my-buy-orders', methods=['GET'])
@login_required
def market_my_buy_orders():
    """我的求购：进行中全部 + 最近成交/取消回执。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    data, err = S1MarketBuyOrderService.get_my_orders(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/buy-orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def market_cancel_buy_order(order_id):
    """取消自己的求购单，并撤下该单的待处理展示。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    data, err = S1MarketBuyOrderService.cancel_order(request.current_user.id, order_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/buy-orders/<int:order_id>/offers', methods=['POST'])
@generic_anti_cheat_check('trade')
@login_required
def market_submit_buy_offer(order_id):
    """向求购单展示仓库物品。body: {adventurer_id, warehouse_item_id, quantity?}"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    warehouse_item_id = body.get('warehouse_item_id') or body.get('item_id')
    quantity = body.get('quantity')
    if not isinstance(adventurer_id, int) or not isinstance(warehouse_item_id, int):
        return error('缺少 adventurer_id 或 warehouse_item_id')
    if quantity is not None and not isinstance(quantity, int):
        return error('quantity 须为整数')
    data, err = S1MarketBuyOrderService.submit_offer(
        request.current_user.id,
        adventurer_id,
        order_id,
        warehouse_item_id,
        quantity=quantity,
    )
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/market/buy-orders/<int:order_id>/offers', methods=['GET'])
@login_required
def market_get_buy_offers(order_id):
    """求购者查看某求购单的物品展示。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    data, err = S1MarketBuyOrderService.get_offers_for_order(
        request.current_user.id,
        order_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/buy-offers/<int:offer_id>/cancel', methods=['POST'])
@login_required
def market_cancel_buy_offer(offer_id):
    """展示者取消一条待处理展示。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    data, err = S1MarketBuyOrderService.cancel_offer(request.current_user.id, offer_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/market/buy-orders/<int:order_id>/offers/accept',
    methods=['POST'],
)
@generic_anti_cheat_check('trade')
@login_required
def market_accept_buy_offers(order_id):
    """求购者一次选定多个展示；任一失败则整批回滚。body: {offer_ids: [...]}"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('请求体必须是 JSON 对象')
    data, err = S1MarketBuyOrderService.accept_offers(
        request.current_user.id,
        order_id,
        body.get('offer_ids'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/market/buy-orders/<int:order_id>/offers/<int:offer_id>/accept',
    methods=['POST'],
)
@generic_anti_cheat_check('trade')
@login_required
def market_accept_buy_offer(order_id, offer_id):
    """求购者选定展示，原子完成扣款、物品转移和不再满足剩余数量的展示撤下。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    data, err = S1MarketBuyOrderService.accept_offer(
        request.current_user.id,
        order_id,
        offer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/market/my-buy-offers', methods=['GET'])
@login_required
def market_my_buy_offers():
    """展示者查看自己向求购单提交的物品。"""
    from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
    data, err = S1MarketBuyOrderService.get_my_offers(request.current_user.id)
    if err:
        return error(err)
    return success(data)


# ── 隐秘拍卖行 ──────────────────────────────────────────────────────────────

@adventurer_s1_bp.route('/secret-auction/summary', methods=['GET'])
@login_required
def secret_auction_summary():
    """进行中拍品数量（建筑分区入口展示，无需打开拍卖行）。"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    data, err = S1SecretAuctionService.open_lot_summary()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/secret-auction/lots', methods=['GET'])
@login_required
def secret_auction_list_lots():
    """进行中拍品列表（密封，仅本人可见 my_bid_amount）。"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    page, per_page = get_pagination_params(default_per_page=20)
    adventurer_id = request.args.get('adventurer_id', type=int)
    data, err = S1SecretAuctionService.list_lots(
        page, per_page, request.current_user.id,
        status='open', adventurer_id=adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/secret-auction/history', methods=['GET'])
@login_required
def secret_auction_history_lots():
    """已结束拍品历史：含中标者名称与本人结果，不含任何价格。"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    page, per_page = get_pagination_params(default_per_page=20)
    adventurer_id = request.args.get('adventurer_id', type=int)
    data, err = S1SecretAuctionService.list_history_lots(
        page, per_page, request.current_user.id, adventurer_id=adventurer_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/secret-auction/lots/<int:lot_id>/bid', methods=['POST'])
@generic_anti_cheat_check('trade')
@login_required
def secret_auction_place_bid(lot_id):
    """密封出价。body: {adventurer_id, amount}"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    amount = body.get('amount')
    if not isinstance(adventurer_id, int) or amount is None:
        return error('缺少 adventurer_id 或 amount')
    data, err = S1SecretAuctionService.place_bid(
        request.current_user.id, adventurer_id, lot_id, amount,
    )
    if err:
        return error(err)
    S1SecretAuctionService._emit_changed(lot_id, 'bid')
    return success(data)


@adventurer_s1_bp.route('/secret-auction/lots/<int:lot_id>/bid', methods=['DELETE'])
@generic_anti_cheat_check('trade')
@login_required
def secret_auction_cancel_bid(lot_id):
    """取消出价。body: {adventurer_id}"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    body = request.get_json(silent=True) or {}
    adventurer_id = body.get('adventurer_id')
    if not isinstance(adventurer_id, int):
        return error('缺少 adventurer_id')
    data, err = S1SecretAuctionService.cancel_bid(
        request.current_user.id, adventurer_id, lot_id,
    )
    if err:
        return error(err)
    S1SecretAuctionService._emit_changed(lot_id, 'bid_cancelled')
    return success(data)


@adventurer_s1_bp.route('/admin/secret-auction/lots', methods=['GET'])
@login_required
@admin_read_required
def admin_secret_auction_list_lots():
    """管理员拍品列表；只读 scope 在拍卖进行中隐藏密封出价金额。"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    from services.admin_secondary_auth_service import AdminSecondaryAuthService
    page, per_page = get_pagination_params(default_per_page=20)
    status = request.args.get('status') or None
    secondary_auth_enabled = current_app.config.get(
        'ADMIN_SECONDARY_AUTH_ENABLED',
        False,
    )
    grant = (
        AdminSecondaryAuthService.current_grant(request.current_user.id)
        if secondary_auth_enabled
        else None
    )
    include_live_prices = (
        not secondary_auth_enabled
        or bool(
            grant
            and grant.get('scope') == AdminSecondaryAuthService.SCOPE_FULL
        )
    )
    data, err = S1SecretAuctionService.admin_list_lots(
        page,
        per_page,
        status=status,
        include_live_prices=include_live_prices,
    )
    if err:
        return error(err)
    response, status_code = success(data)
    response.headers['Cache-Control'] = 'private, no-store'
    return response, status_code


@adventurer_s1_bp.route('/admin/secret-auction/lots', methods=['POST'])
@login_required
@admin_required
def admin_secret_auction_create_lots():
    """批量上架拍品。body: {ends_at?, reserve_price?, lots: [{..., reserve_price?}]}"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    body = request.get_json(silent=True) or {}
    lots = body.get('lots')
    ends_at = body.get('ends_at')
    reserve_price = body.get('reserve_price')
    data, err = S1SecretAuctionService.admin_create_lots(
        request.current_user.id, lots,
        default_ends_at=ends_at, default_reserve_price=reserve_price,
    )
    if err:
        return error(err)
    for lot in data.get('lots', []):
        S1SecretAuctionService._emit_changed(lot['id'], 'created')
    return created(data)


@adventurer_s1_bp.route('/admin/secret-auction/lots/<int:lot_id>/cancel', methods=['POST'])
@login_required
@admin_required
def admin_secret_auction_cancel_lot(lot_id):
    """取消进行中的拍品并退还所有出价。"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    data, err = S1SecretAuctionService.admin_cancel_lot(lot_id)
    if err:
        return error(err)
    S1SecretAuctionService._emit_changed(lot_id, 'cancelled')
    return success(data)


@adventurer_s1_bp.route('/admin/secret-auction/fix-unfunded-preview', methods=['GET'])
@login_required
@admin_required
def admin_secret_auction_fix_unfunded_preview():
    """预览：已结算拍品中 status=held 但 escrow=0 的残留 bid。"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    data, err = S1SecretAuctionService.preview_unfunded_held_fix()
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/secret-auction/fix-unfunded', methods=['POST'])
@login_required
@admin_required
def admin_secret_auction_fix_unfunded():
    """执行：将已结算拍品中 status=held 且 escrow=0 的 bid 标记为 refunded。"""
    from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
    data, err = S1SecretAuctionService.apply_unfunded_held_fix()
    if err:
        return error(err)
    return success(data)


# ── 收益异常监控（管理员只读审计 + 历史回溯） ──────────────────────────────

@adventurer_s1_bp.route('/admin/gain-anomalies', methods=['GET'])
@login_required
@admin_required
def admin_list_gain_anomalies():
    """分页查询 S1 金币/物品异常增长监控记录。"""
    from plugins.adventurer_s1.services.s1_gain_anomaly_service import S1GainAnomalyService

    page, per_page = get_pagination_params(default_per_page=20)
    data, err = S1GainAnomalyService.list_records(
        page,
        per_page,
        status=request.args.get('status') or None,
        asset_kind=request.args.get('asset_kind') or None,
        severity=request.args.get('severity') or None,
        search=request.args.get('search') or '',
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/gain-anomalies/<int:anomaly_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_gain_anomaly(anomaly_id):
    """更新监控状态；只改变审计状态，不修改玩家资产。"""
    from plugins.adventurer_s1.services.s1_gain_anomaly_service import S1GainAnomalyService

    body = request.get_json(silent=True) or {}
    data, err = S1GainAnomalyService.update_status(
        anomaly_id,
        request.current_user.id,
        str(body.get('status') or ''),
        str(body.get('note') or ''),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/gain-anomalies/backfill/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_gain_anomaly_backfill():
    """扫描监控上线前的账本，返回候选样本与服务端快照。"""
    from plugins.adventurer_s1.services.s1_gain_anomaly_service import S1GainAnomalyService

    body = request.get_json(silent=True) or {}
    data, err = S1GainAnomalyService.preview_history(
        request.current_user.id,
        body.get('lookback_days', 30),
        str(body.get('asset_kind') or 'all'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/gain-anomalies/backfill/execute', methods=['POST'])
@login_required
@admin_required
def admin_execute_gain_anomaly_backfill():
    """把已预览的历史候选幂等导入监控记录。"""
    from plugins.adventurer_s1.services.s1_gain_anomaly_service import S1GainAnomalyService

    body = request.get_json(silent=True) or {}
    data, err = S1GainAnomalyService.execute_history(
        request.current_user.id,
        body.get('snapshot_token'),
    )
    if err:
        return error(err)
    return success(data)


# ── 数据异常申诉（玩家提交 → 主 AI → 高权限独立复核 → 自动落库） ────────────────

@adventurer_s1_bp.route('/<int:adv_id>/text-correction/preview', methods=['GET'])
@login_required
def player_text_correction_preview(adv_id):
    """预览：扫描技能/特质/装备/冒险史中的残留探险代号。"""
    from plugins.adventurer_s1.services.adventurer_s1_player_text_correction_service import (
        AdventurerS1PlayerTextCorrectionService,
    )
    data, err = AdventurerS1PlayerTextCorrectionService.preview(
        user_id=request.current_user.id,
        adventurer_id=adv_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/text-correction/apply', methods=['POST'])
@login_required
def player_text_correction_apply(adv_id):
    """应用玩家勾选的词条条目替换。body: {selections: [{occurrence_id, replacements:[{token,name}]}]}"""
    from plugins.adventurer_s1.services.adventurer_s1_player_text_correction_service import (
        AdventurerS1PlayerTextCorrectionService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1PlayerTextCorrectionService.apply(
        user_id=request.current_user.id,
        adventurer_id=adv_id,
        selections=body.get('selections') or body.get('mappings') or [],
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/text-correction/dismiss', methods=['POST'])
@login_required
def player_text_correction_dismiss(adv_id):
    """标记词条无错，后续 preview 不再展示。body: {occurrence_id}"""
    from plugins.adventurer_s1.services.adventurer_s1_player_text_correction_service import (
        AdventurerS1PlayerTextCorrectionService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1PlayerTextCorrectionService.dismiss(
        user_id=request.current_user.id,
        adventurer_id=adv_id,
        occurrence_id=body.get('occurrence_id') or body.get('id') or '',
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/<int:adv_id>/text-correction/restore', methods=['POST'])
@login_required
def player_text_correction_restore(adv_id):
    """撤回上一次文本代号更正（从备份还原）。"""
    from plugins.adventurer_s1.services.adventurer_s1_player_text_correction_service import (
        AdventurerS1PlayerTextCorrectionService,
    )
    data, err = AdventurerS1PlayerTextCorrectionService.restore(
        user_id=request.current_user.id,
        adventurer_id=adv_id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/data-appeals/targets', methods=['GET'])
@login_required
def data_appeal_targets():
    """玩家可申诉的条目列表（特质/装备/仓库/技能，含 id）。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.get_appeal_targets(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tavern-relief', methods=['GET'])
@login_required
def tavern_relief_preview():
    """玩家：预览最近一次已完成探险是否可提交酒馆补贴 AI 核定。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.get_tavern_relief_preview(
        request.current_user.id,
        client_request_id=request.headers.get('Idempotency-Key'),
        appeal_id=request.args.get('appeal_id', type=int),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/tavern-relief', methods=['POST'])
@login_required
def tavern_relief_claim():
    """玩家：持久化酒馆补贴申请并提交后台 AI 核定。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.submit_tavern_relief(
        request.current_user.id,
        client_request_id=request.headers.get('Idempotency-Key'),
    )
    if err:
        return error(err)
    return success(data, message=data.get('message') or '酒馆补贴申请已提交')


@adventurer_s1_bp.route('/appeal-projects', methods=['GET'])
@login_required
def appeal_project_overview():
    """玩家：只读预览当前可执行的确定性申诉专项项目。"""
    from plugins.adventurer_s1.services.s1_appeal_project_service import (
        S1AppealProjectService,
    )
    data, err = S1AppealProjectService.get_overview(request.current_user.id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/appeal-projects/equipment-integrity/preview',
    methods=['GET'],
)
@login_required
def appeal_project_equipment_integrity_preview():
    """玩家：聚合扫描可由冻结快照唯一还原的装备污染。"""
    from plugins.adventurer_s1.services.s1_player_equipment_integrity_service import (
        S1PlayerEquipmentIntegrityService,
    )
    data, err = S1PlayerEquipmentIntegrityService.preview(
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/appeal-projects/equipment-integrity/execute',
    methods=['POST'],
)
@login_required
def appeal_project_equipment_integrity_execute():
    """玩家：按预览快照条件修复全部唯一绑定的装备污染。"""
    from plugins.adventurer_s1.services.s1_player_equipment_integrity_service import (
        S1PlayerEquipmentIntegrityService,
    )
    body = request.get_json(silent=True) or {}
    data, err = S1PlayerEquipmentIntegrityService.execute(
        request.current_user.id,
        body.get('preview_token'),
    )
    if err:
        return error(err)
    return success(data, message='装备聚合修复已完成')


@adventurer_s1_bp.route(
    '/appeal-projects/equipment-integrity/restore',
    methods=['POST'],
)
@login_required
def appeal_project_equipment_integrity_restore():
    """玩家：仅在修复后状态未漂移时撤销上一次装备聚合修复。"""
    from plugins.adventurer_s1.services.s1_player_equipment_integrity_service import (
        S1PlayerEquipmentIntegrityService,
    )
    data, err = S1PlayerEquipmentIntegrityService.restore(
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='上一次装备聚合修复已撤销')


@adventurer_s1_bp.route('/appeal-projects/pet-evolution-refund', methods=['POST'])
@login_required
def appeal_project_pet_evolution_refund():
    """玩家：补退旧嬗变技术中止时已扣未退的辅助物品。"""
    from plugins.adventurer_s1.services.s1_appeal_project_service import (
        S1AppealProjectService,
    )
    data, err = S1AppealProjectService.claim_pet_evolution_refunds(
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message=data.get('message') or '嬗变补退核对完成')


@adventurer_s1_bp.route('/appeal-projects/buyback-repurchase', methods=['POST'])
@login_required
def appeal_project_buyback_repurchase():
    """玩家：按原收购总价加价买回一条误售物品。"""
    from plugins.adventurer_s1.services.s1_appeal_project_service import (
        S1AppealProjectService,
    )
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error('买回请求格式无效')
    data, err = S1AppealProjectService.repurchase_buyback_sale(
        request.current_user.id,
        body.get('token'),
    )
    if err:
        return error(err)
    return success(data, message=data.get('message') or '物品买回完成')


@adventurer_s1_bp.route('/data-appeals', methods=['POST'])
@login_required
def submit_data_appeal():
    """提交数据异常申诉。探险类申诉默认读取最近一次已完成探险全文。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return error('申诉请求格式无效')
    if body.get('activity_id') is not None or body.get('activity_match_id') is not None:
        return error('酒馆活动只结算赛事排名，不支持数据申诉')
    evidence = body.get('expedition_evidence')
    if evidence is not None and not isinstance(evidence, dict):
        return error('探险证据格式无效')
    data, err = AdventurerS1DataAppealService.submit_appeal(
        user_id=request.current_user.id,
        targets=body.get('targets') or [],
        player_note=body.get('player_note') or '',
        expedition_evidence=evidence,
        appeal_category=body.get('appeal_category') or 'other',
    )
    if err:
        return error(err)
    return created(data)


@adventurer_s1_bp.route('/data-appeals/mine', methods=['GET'])
@login_required
def my_data_appeals():
    """我的申诉记录。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    limit = request.args.get('limit', 20)
    data, err = AdventurerS1DataAppealService.list_my_appeals(request.current_user.id, limit)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/ai-review', methods=['POST'])
@login_required
def player_ai_review_data_appeal(appeal_id):
    """玩家：对某条数据异常申诉的 AI 处理结果发起独立复核。body: {request: str<=200}"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1DataAppealService.player_mimo_review_request(
        request.current_user.id,
        appeal_id,
        body.get('request') or '',
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals', methods=['GET'])
@login_required
@admin_read_required
def admin_list_data_appeals():
    """管理员：申诉列表。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    page, per_page = get_pagination_params(default_per_page=20)
    status = request.args.get('status') or None
    search = (request.args.get('search') or request.args.get('q') or '').strip() or None
    data, err = AdventurerS1DataAppealService.list_admin(
        status=status, page=page, per_page=per_page, search=search,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>', methods=['GET'])
@login_required
@admin_read_required
def admin_get_data_appeal(appeal_id):
    """管理员：申诉详情（含 AI patch 与 before/after 预览）。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.get_admin_detail(appeal_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/data-appeals/<int:appeal_id>/manual-takeover',
    methods=['POST'],
)
@login_required
@admin_required
def admin_claim_data_appeal_manual_takeover(appeal_id):
    """管理员：冻结自动申诉写回并切换为人工结构化提案。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.claim_manual_takeover(
        appeal_id,
        request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已切换为人工接管，AI 后续结果不会覆盖本工单')


@adventurer_s1_bp.route(
    '/admin/data-appeals/<int:appeal_id>/manual-draft',
    methods=['POST'],
)
@login_required
@admin_required
def admin_save_data_appeal_manual_draft(appeal_id):
    """管理员：保存人工申诉提案；仍需调用 approve 才会真实写入。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error('人工方案请求格式无效')
    data, err = AdventurerS1DataAppealService.save_manual_takeover_draft(
        appeal_id,
        request.current_user.id,
        summary=body.get('summary'),
        core_request_satisfied=body.get('core_request_satisfied'),
        request_checklist=body.get('request_checklist'),
        patch=body.get('patch'),
    )
    if err:
        return error(err)
    return success(data, message='人工方案已保存，请核对预览后再执行通过')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/approve', methods=['POST'])
@login_required
@admin_required
def admin_approve_data_appeal(appeal_id):
    """管理员：手工通过申诉并执行修复。"""
    from db_repository.exceptions import DatabaseError, DuplicateRecordError
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    try:
        data, err = AdventurerS1DataAppealService.approve(appeal_id, request.current_user.id)
    except DuplicateRecordError as exc:
        msg = str(exc)
        if 'uq_advs1_skill_adv_name' in msg:
            return error('技能名称冲突：合并落库失败，请稍后重试或联系开发排查')
        return error(f'数据冲突，无法执行修复：{exc}')
    except DatabaseError:
        current_app.logger.exception(
            'S1 data appeal approval failed: appeal_id=%s', appeal_id,
        )
        return error('执行修复失败，事务已回滚，请查看服务端日志')
    if err:
        return error(err)
    return success(data, message='申诉已通过并执行修复')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/reject', methods=['POST'])
@login_required
@admin_required
def admin_reject_data_appeal(appeal_id):
    """管理员：手工驳回申诉。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1DataAppealService.reject(
        appeal_id, request.current_user.id, body.get('reason') or body.get('reject_reason', ''),
    )
    if err:
        return error(err)
    return success(data, message='申诉已驳回')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/revert', methods=['POST'])
@login_required
@admin_required
def admin_revert_data_appeal(appeal_id):
    """管理员：一键还原已通过申诉的数据修复（按审计台账倒序反向）。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.revert_appeal(appeal_id, request.current_user.id)
    if err:
        return error(err)
    return success(data, message='已还原本次申诉的数据修复')


@adventurer_s1_bp.route('/admin/data-appeals/cross-character/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_cross_character_data_appeals():
    """管理员：预览新角色关联旧角色探险的异常申诉。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 100)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = AdventurerS1DataAppealService.preview_cross_character_appeals(limit=limit)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/cross-character/execute', methods=['POST'])
@login_required
@admin_required
def admin_execute_cross_character_data_appeal_reverts():
    """管理员：按预览 ID 一键撤销跨角色申诉已落库内容，并取消未落库工单。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )

    body = request.get_json(silent=True) or {}
    appeal_ids = body.get('appeal_ids')
    if not isinstance(appeal_ids, list):
        return error('appeal_ids 必须是数组')
    data, err = AdventurerS1DataAppealService.execute_cross_character_appeal_reverts(
        request.current_user.id,
        appeal_ids,
    )
    if err:
        return error(err)
    return success(data, message='跨角色申诉撤销已执行')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/send-revert-offer', methods=['POST'])
@login_required
@admin_required
def admin_send_data_appeal_revert_offer(appeal_id):
    """管理员：向玩家发送「同意则还原申诉修复」确认通知。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    admin_note = (body.get('admin_note') or body.get('note') or '').strip() or None
    data, err = AdventurerS1DataAppealService.send_revert_offer(
        appeal_id, request.current_user.id, admin_note=admin_note,
    )
    if err:
        return error(err)
    return success(data, message='已向玩家推送还原确认（个人页可见）')


@adventurer_s1_bp.route('/admin/data-appeals/send-revert-offers/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_data_appeal_revert_offers():
    """管理员：预览可推送的还原确认列表（默认只删不补）。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    appeal_ids = body.get('appeal_ids')
    if appeal_ids is not None and not isinstance(appeal_ids, list):
        return error('appeal_ids 须为数组')
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    only_likely_mistake = bool(body.get('only_likely_mistake', True))
    if body.get('only_with_deletes') is not None:
        only_likely_mistake = bool(body.get('only_with_deletes'))
    data, err = AdventurerS1DataAppealService.preview_revert_offers(
        appeal_ids=appeal_ids,
        only_likely_mistake=only_likely_mistake,
        limit=limit,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/send-revert-offers', methods=['POST'])
@login_required
@admin_required
def admin_bulk_send_data_appeal_revert_offers():
    """管理员：批量向已通过申诉的玩家发送还原确认。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    appeal_ids = body.get('appeal_ids')
    if appeal_ids is not None and not isinstance(appeal_ids, list):
        return error('appeal_ids 须为数组')
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    only_with_deletes = body.get('only_with_deletes')
    only_likely_mistake = body.get('only_likely_mistake')
    if only_likely_mistake is None and only_with_deletes is not None:
        only_likely_mistake = bool(only_with_deletes)
    elif only_likely_mistake is None:
        only_likely_mistake = True
    admin_note = (body.get('admin_note') or body.get('note') or '').strip() or None
    data, err = AdventurerS1DataAppealService.bulk_send_revert_offers(
        request.current_user.id,
        appeal_ids=appeal_ids,
        only_likely_mistake=bool(only_likely_mistake),
        limit=limit,
        admin_note=admin_note,
    )
    if err:
        return error(err)
    sent = len(data.get('sent') or [])
    return success(data, message=f'已发送 {sent} 条还原确认通知')


@adventurer_s1_bp.route('/data-appeals/pending-revert-offers', methods=['GET'])
@login_required
def list_pending_data_appeal_revert_offers():
    """玩家个人页：待确认的还原邀请。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    try:
        limit = int(request.args.get('limit') or 20)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = AdventurerS1DataAppealService.list_pending_revert_offers(
        request.current_user.id, limit=limit,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/revert-offer-status', methods=['GET'])
@login_required
def get_data_appeal_revert_offer_status(appeal_id):
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.get_revert_offer_status(
        appeal_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/accept-revert-offer', methods=['POST'])
@login_required
def accept_data_appeal_revert_offer(appeal_id):
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.accept_revert_offer(
        appeal_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已撤销本次申诉的数据修复并完成还原')


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/decline-revert-offer', methods=['POST'])
@login_required
def decline_data_appeal_revert_offer(appeal_id):
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.decline_revert_offer(
        appeal_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已拒绝还原邀请')


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/request-revert', methods=['POST'])
@login_required
def request_data_appeal_revert(appeal_id):
    """玩家：申请撤回某次已通过申诉的数据修复（消耗申诉额度，AI 审核）。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    note = body.get('player_note') or body.get('note') or ''
    data, err = AdventurerS1DataAppealService.request_player_revert(
        appeal_id, request.current_user.id, note,
    )
    if err:
        return error(err)
    return success(data, message=data.get('message') or '撤回申请已提交')


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/confirm-revert-request', methods=['POST'])
@login_required
def confirm_data_appeal_revert_request(appeal_id):
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.confirm_player_revert_request(
        appeal_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已撤回此次申诉的数据修复并完成还原')


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/decline-revert-request', methods=['POST'])
@login_required
def decline_data_appeal_revert_request(appeal_id):
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.decline_player_revert_request(
        appeal_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已放弃本次撤回')


@adventurer_s1_bp.route('/admin/data-appeals/bug-reclaims/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_data_appeal_bug_reclaims():
    """管理员：预览历史申诉漏洞造成的异常收益回收清单。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = S1BugReclaimService.preview(
        limit=limit,
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/class-scroll-reclaims/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_class_scroll_reclaims():
    """管理员：结构化预览历史漏扣转职凭证及其当前流向。"""
    from plugins.adventurer_s1.services.s1_class_scroll_reclaim_service import (
        S1ClassScrollReclaimService,
    )

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 20)
    except (TypeError, ValueError):
        return error('limit 无效')
    if limit < 1 or limit > 50:
        return error('limit 必须是 1-50 的整数')
    data, err = S1ClassScrollReclaimService.preview(
        limit=limit,
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/class-scroll-reclaims/execute', methods=['POST'])
@login_required
@admin_required
def admin_execute_class_scroll_reclaims():
    """管理员：事务化执行已预览的漏扣转职凭证回收。"""
    from plugins.adventurer_s1.services.s1_class_scroll_reclaim_service import (
        S1ClassScrollReclaimService,
    )

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 20)
    except (TypeError, ValueError):
        return error('limit 无效')
    if limit < 1 or limit > 50:
        return error('limit 必须是 1-50 的整数')
    data, err = S1ClassScrollReclaimService.execute(
        admin_user_id=request.current_user.id,
        limit=limit,
        preview_token=str(body.get('preview_token') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message='漏扣转职凭证回收已执行')


@adventurer_s1_bp.route('/admin/data-appeals/bug-reclaims/execute', methods=['POST'])
@login_required
@admin_required
def admin_execute_data_appeal_bug_reclaims():
    """管理员：执行历史申诉漏洞异常收益回收，金币不足允许扣成负数。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = S1BugReclaimService.execute(
        request.current_user.id,
        limit=limit,
        note=body.get('note') or '',
        preview_token=str(body.get('preview_token') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message='异常收益回收已执行')


@adventurer_s1_bp.route('/admin/data-appeals/bug-reclaims/dismiss', methods=['POST'])
@login_required
@admin_required
def admin_dismiss_data_appeal_bug_reclaim():
    """管理员：标记异常收益回收预览项为误报，后续预览不再出现。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    body = request.get_json(silent=True) or {}
    issue_key = body.get('issue_key')
    if not issue_key:
        return error('issue_key 不能为空')
    data, err = S1BugReclaimService.dismiss(
        request.current_user.id,
        str(issue_key),
        item=body.get('item') if isinstance(body.get('item'), dict) else None,
        note=str(body.get('note') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message='已标记误报')


@adventurer_s1_bp.route('/admin/data-appeals/bug-reclaims/adjust-quantity', methods=['POST'])
@login_required
@admin_required
def admin_adjust_data_appeal_bug_reclaim_quantity():
    """管理员：认定非法数量（可小于系统累计非法增量）。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    body = request.get_json(silent=True) or {}
    issue_key = body.get('issue_key')
    if not issue_key:
        return error('issue_key 不能为空')
    if body.get('illegal_quantity') is None:
        return error('illegal_quantity 不能为空')
    data, err = S1BugReclaimService.adjust_quantity(
        request.current_user.id,
        str(issue_key),
        body.get('illegal_quantity'),
        item=body.get('item') if isinstance(body.get('item'), dict) else None,
        note=str(body.get('note') or '').strip(),
    )
    if err:
        return error(err)
    msg = '已恢复系统计算非法量' if data.get('reverted') else '已认定非法数量'
    return success(data, message=msg)


@adventurer_s1_bp.route('/admin/data-appeals/bug-reclaims/adjust-text', methods=['POST'])
@login_required
@admin_required
def admin_adjust_data_appeal_bug_reclaim_text():
    """管理员：认定文字修正（改 name/desc，可选 quantity；执行时不扣物扣金）。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    body = request.get_json(silent=True) or {}
    issue_key = body.get('issue_key')
    if not issue_key:
        return error('issue_key 不能为空')
    data, err = S1BugReclaimService.adjust_text(
        request.current_user.id,
        str(issue_key),
        body.get('corrected_name') or '',
        corrected_desc=body.get('corrected_desc') if 'corrected_desc' in body else None,
        corrected_quantity=body.get('corrected_quantity'),
        item=body.get('item') if isinstance(body.get('item'), dict) else None,
        note=str(body.get('note') or '').strip(),
        revert=bool(body.get('revert')),
    )
    if err:
        return error(err)
    msg = '已取消文字修正' if data.get('reverted') else '已保存文字修正'
    return success(data, message=msg)


@adventurer_s1_bp.route('/admin/data-appeals/bug-reclaims/item-change-catalog', methods=['GET'])
@login_required
@admin_required
def admin_list_appeal_item_change_catalog():
    """管理员：申诉以来新增物品 + 改 quantity 总览。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    limit = request.args.get('limit', 500, type=int)
    sort = str(request.args.get('sort') or 'name').strip()
    data, err = S1BugReclaimService.item_change_catalog(limit=limit, sort=sort)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/bug-reclaims/history', methods=['GET'])
@login_required
@admin_required
def admin_list_data_appeal_bug_reclaims():
    """管理员：逐条分页查看异常收益回收历史。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    page, per_page = get_pagination_params(default_per_page=20)
    data, err = S1BugReclaimService.history(page=page, per_page=per_page)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/bug-reclaims/history/<int:item_id>', methods=['GET'])
@login_required
@admin_required
def admin_get_data_appeal_bug_reclaim(item_id):
    """管理员：按需查看一条异常收益回收的完整快照。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    data, err = S1BugReclaimService.history_detail(item_id)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route(
    '/admin/data-appeals/bug-reclaims/history/<int:item_id>/revert', methods=['POST'],
)
@login_required
@admin_required
def admin_revert_data_appeal_bug_reclaim(item_id):
    """管理员：按冻结恢复快照逐条回溯异常收益回收。"""
    from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

    body = request.get_json(silent=True) or {}
    data, err = S1BugReclaimService.revert_history_item(
        request.current_user.id,
        item_id,
        note=str(body.get('note') or '')[:200],
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/qty-shortfalls/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_data_appeal_qty_shortfalls():
    """管理员：预览历史申诉 name/desc 已归位但 quantity 漏写的补发清单。"""
    from plugins.adventurer_s1.services.s1_bug_qty_shortfall_service import S1BugQtyShortfallService

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = S1BugQtyShortfallService.preview(
        limit=limit,
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/qty-shortfalls/execute', methods=['POST'])
@login_required
@admin_required
def admin_execute_data_appeal_qty_shortfalls():
    """管理员：执行历史申诉异常数量漏给补发。"""
    from plugins.adventurer_s1.services.s1_bug_qty_shortfall_service import S1BugQtyShortfallService

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = S1BugQtyShortfallService.execute(
        request.current_user.id,
        limit=limit,
        note=body.get('note') or '',
        preview_token=str(body.get('preview_token') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message='异常数量漏给补发已执行')


@adventurer_s1_bp.route('/admin/data-appeals/qty-shortfalls/dismiss', methods=['POST'])
@login_required
@admin_required
def admin_dismiss_data_appeal_qty_shortfall():
    """管理员：标记异常数量漏给预览项为误报，后续预览不再出现。"""
    from plugins.adventurer_s1.services.s1_bug_qty_shortfall_service import S1BugQtyShortfallService

    body = request.get_json(silent=True) or {}
    issue_key = body.get('issue_key')
    if not issue_key:
        return error('issue_key 不能为空')
    data, err = S1BugQtyShortfallService.dismiss(
        request.current_user.id,
        str(issue_key),
        item=body.get('item') if isinstance(body.get('item'), dict) else None,
        note=str(body.get('note') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message='已标记误报')


@adventurer_s1_bp.route('/admin/data-appeals/qty-shortfalls/adjust-quantity', methods=['POST'])
@login_required
@admin_required
def admin_adjust_data_appeal_qty_shortfall_quantity():
    """管理员：认定异常数量漏给的实际补发数量。"""
    from plugins.adventurer_s1.services.s1_bug_qty_shortfall_service import S1BugQtyShortfallService

    body = request.get_json(silent=True) or {}
    issue_key = body.get('issue_key')
    if not issue_key:
        return error('issue_key 不能为空')
    if body.get('shortfall_quantity') is None:
        return error('shortfall_quantity 不能为空')
    data, err = S1BugQtyShortfallService.adjust_quantity(
        request.current_user.id,
        str(issue_key),
        body.get('shortfall_quantity'),
        item=body.get('item') if isinstance(body.get('item'), dict) else None,
        note=str(body.get('note') or '').strip(),
    )
    if err:
        return error(err)
    msg = '已恢复系统计算补发量' if data.get('reverted') else '已认定补发数量'
    return success(data, message=msg)


@adventurer_s1_bp.route('/admin/data-appeals/qty-shortfalls/history', methods=['GET'])
@login_required
@admin_required
def admin_list_data_appeal_qty_shortfalls():
    """管理员：查看异常数量漏给补发批次历史。"""
    from plugins.adventurer_s1.services.s1_bug_qty_shortfall_service import S1BugQtyShortfallService

    page, per_page = get_pagination_params(default_per_page=20)
    data, err = S1BugQtyShortfallService.history(page=page, per_page=per_page)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/catalog-desc-pollution/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_catalog_desc_pollution():
    """管理员：预览申诉 AI 误补/误清空图鉴物 custom_desc 的还原清单。"""
    from plugins.adventurer_s1.services.s1_catalog_desc_pollution_repair_service import (
        S1CatalogDescPollutionRepairService,
    )

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 100)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = S1CatalogDescPollutionRepairService.preview(
        limit=limit,
        admin_user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/catalog-desc-pollution/execute', methods=['POST'])
@login_required
@admin_required
def admin_execute_catalog_desc_pollution():
    """管理员：执行图鉴 custom_desc 污染批量还原。"""
    from plugins.adventurer_s1.services.s1_catalog_desc_pollution_repair_service import (
        S1CatalogDescPollutionRepairService,
    )

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 100)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = S1CatalogDescPollutionRepairService.execute(
        request.current_user.id,
        limit=limit,
        note=str(body.get('note') or '').strip(),
        preview_token=str(body.get('preview_token') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message=f'已还原 {data.get("repaired", 0)} 条 custom_desc')


@adventurer_s1_bp.route('/admin/data-appeals/catalog-desc-pollution/restore', methods=['POST'])
@login_required
@admin_required
def admin_restore_catalog_desc_pollution():
    """管理员：从最近一次图鉴 custom_desc 还原备份回滚。"""
    from plugins.adventurer_s1.services.s1_catalog_desc_pollution_repair_service import (
        S1CatalogDescPollutionRepairService,
    )

    data, err = S1CatalogDescPollutionRepairService.restore()
    if err:
        return error(err)
    return success(data, message=f'已回滚 {data.get("restored", 0)} 条 custom_desc')


@adventurer_s1_bp.route('/admin/data-appeals/catalog-desc-pollution/dismiss', methods=['POST'])
@login_required
@admin_required
def admin_dismiss_catalog_desc_pollution():
    """管理员：标记图鉴 custom_desc 污染候选为误报。"""
    from plugins.adventurer_s1.services.s1_catalog_desc_pollution_repair_service import (
        S1CatalogDescPollutionRepairService,
    )

    body = request.get_json(silent=True) or {}
    issue_key = body.get('issue_key')
    if not issue_key:
        return error('issue_key 不能为空')
    data, err = S1CatalogDescPollutionRepairService.dismiss(
        request.current_user.id,
        str(issue_key),
        note=str(body.get('note') or '').strip(),
    )
    if err:
        return error(err)
    return success(data, message='已标记误报')


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/continue', methods=['POST'])
@login_required
def continue_data_appeal(appeal_id):
    """玩家：继续本人因技术或在途锁中断的原申诉。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.retry_ai(
        appeal_id, user_id=request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='申诉已继续处理')


@adventurer_s1_bp.route('/data-appeals/<int:appeal_id>/cancel', methods=['POST'])
@login_required
def cancel_data_appeal(appeal_id):
    """玩家：取消 AI 处理中的申诉。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.cancel_by_player(
        request.current_user.id, appeal_id,
    )
    if err:
        return error(err)
    return success(data, message='申诉已取消')


@adventurer_s1_bp.route('/admin/data-appeals/stuck-revert-requests/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_stuck_revert_requests():
    """管理员：预览卡住的「申请撤回修复」AI（player_revert_request_status=processing）。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = AdventurerS1DataAppealService.preview_stuck_player_revert_requests(limit=limit)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/stuck-revert-requests/retry', methods=['POST'])
@login_required
@admin_required
def admin_retry_stuck_revert_requests():
    """管理员：批量重试卡住的修复撤回 AI。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    appeal_ids = body.get('appeal_ids')
    if not isinstance(appeal_ids, list) or not appeal_ids:
        return error('appeal_ids 不能为空')
    retried, skipped, errors = [], [], []
    for raw_id in appeal_ids:
        try:
            appeal_id = int(raw_id)
        except (TypeError, ValueError):
            errors.append({'id': raw_id, 'error': 'id 无效'})
            continue
        data, err = AdventurerS1DataAppealService.admin_retry_player_revert_request(
            appeal_id, request.current_user.id,
        )
        if err:
            errors.append({'id': appeal_id, 'error': err})
        elif data:
            retried.append(data)
        else:
            skipped.append({'id': appeal_id, 'reason': '不可重试'})
    return success(
        {'retried': retried, 'skipped': skipped, 'errors': errors},
        message=f'已重试 {len(retried)} 条，跳过 {len(skipped)} 条，失败 {len(errors)} 条',
    )


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/retry-revert-request', methods=['POST'])
@login_required
@admin_required
def admin_retry_revert_request(appeal_id):
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.admin_retry_player_revert_request(
        appeal_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message=data.get('message') or '已重新提交修复撤回 AI')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/reset-revert-request', methods=['POST'])
@login_required
@admin_required
def admin_reset_revert_request(appeal_id):
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.admin_reset_stuck_player_revert_request(
        appeal_id, request.current_user.id,
    )
    if err:
        return error(err)
    return success(data, message='已重置，玩家可重新申请撤回修复')


@adventurer_s1_bp.route('/admin/data-appeals/misclassified-withdrawals/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_misclassified_withdrawals():
    """管理员：预览误标为 rejected 的玩家撤回申诉。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = AdventurerS1DataAppealService.preview_misclassified_withdrawals(limit=limit)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/misclassified-withdrawals/execute', methods=['POST'])
@login_required
@admin_required
def admin_execute_misclassified_withdrawals():
    """管理员：将误标 rejected 的玩家撤回申诉修正为 cancelled。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    appeal_ids = body.get('appeal_ids')
    if appeal_ids is not None and not isinstance(appeal_ids, list):
        return error('appeal_ids 必须为数组')
    notify_player = body.get('notify_player', True) is not False
    data, err = AdventurerS1DataAppealService.execute_misclassified_withdrawals(
        request.current_user.id,
        appeal_ids=appeal_ids,
        limit=limit,
        notify_player=notify_player,
    )
    if err:
        return error(err)
    fixed = len(data.get('fixed') or [])
    skipped = len(data.get('skipped') or [])
    errors = len(data.get('errors') or [])
    return success(data, message=f'已修正 {fixed} 条，跳过 {skipped} 条，失败 {errors} 条')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/fix-misclassified-withdrawal', methods=['POST'])
@login_required
@admin_required
def admin_fix_misclassified_withdrawal(appeal_id):
    """管理员：单条修正误标 rejected 的玩家撤回申诉。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    notify_player = body.get('notify_player', True) is not False
    data, err = AdventurerS1DataAppealService.fix_misclassified_withdrawal(
        appeal_id,
        request.current_user.id,
        notify_player=notify_player,
    )
    if err:
        return error(err)
    return success(data, message='已修正为已撤回')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/cancel', methods=['POST'])
@login_required
@admin_required
def admin_cancel_data_appeal(appeal_id):
    """管理员：取消申诉（释放玩家提交额度）。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1DataAppealService.cancel_by_admin(
        appeal_id, request.current_user.id, body.get('reason') or '',
    )
    if err:
        return error(err)
    return success(data, message='申诉已取消')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/refund-rarity-review', methods=['POST'])
@login_required
@admin_required
def admin_refund_rarity_review_guard_failure(appeal_id):
    """管理员：关闭历史稀有度复核误杀单，并返还 1 次申诉次数。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1DataAppealService.refund_rarity_review_guard_failure(
        appeal_id,
        request.current_user.id,
        notify_player=body.get('notify_player', True) is not False,
    )
    if err:
        return error(err)
    return success(data, message='已关闭旧稀有度复核单并返还 1 次申诉次数')


@adventurer_s1_bp.route('/admin/data-appeals/rarity-review-guard-failures/preview', methods=['POST'])
@login_required
@admin_required
def admin_preview_rarity_review_guard_failures():
    """管理员：预览历史稀有度复核 guard 误杀单。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = AdventurerS1DataAppealService.preview_rarity_review_guard_failures(limit=limit)
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/rarity-review-guard-failures/refund', methods=['POST'])
@login_required
@admin_required
def admin_bulk_refund_rarity_review_guard_failures():
    """管理员：批量关闭历史稀有度复核误杀单，并返还次数。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    appeal_ids = body.get('appeal_ids')
    if appeal_ids is not None and not isinstance(appeal_ids, list):
        return error('appeal_ids 必须为数组')
    try:
        limit = int(body.get('limit') or 50)
    except (TypeError, ValueError):
        return error('limit 无效')
    data, err = AdventurerS1DataAppealService.bulk_refund_rarity_review_guard_failures(
        request.current_user.id,
        appeal_ids=appeal_ids,
        limit=limit,
        notify_player=body.get('notify_player', True) is not False,
    )
    if err:
        return error(err)
    refunded = len(data.get('refunded') or [])
    skipped = len(data.get('skipped') or [])
    errors = len(data.get('errors') or [])
    return success(data, message=f'已返还 {refunded} 条，跳过 {skipped} 条，失败 {errors} 条')


@adventurer_s1_bp.route('/admin/data-appeals/<int:appeal_id>/retry-ai', methods=['POST'])
@login_required
@admin_required
def admin_retry_data_appeal_ai(appeal_id):
    """管理员：重试 AI 分析（processing 卡住或 awaiting_review 结果不满意时）。"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.retry_ai(appeal_id)
    if err:
        return error(err)
    return success(data, message='已重新提交 AI 分析')


@adventurer_s1_bp.route('/admin/data-appeals/quota', methods=['GET'])
@login_required
@admin_required
def admin_get_data_appeal_quota():
    """管理员：查看某玩家的申诉额度使用情况。query: user_id 或 username"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    data, err = AdventurerS1DataAppealService.get_user_quota_admin(
        user_id=request.args.get('user_id'),
        username=request.args.get('username'),
    )
    if err:
        return error(err)
    return success(data)


@adventurer_s1_bp.route('/admin/data-appeals/quota/reset', methods=['POST'])
@login_required
@admin_required
def admin_reset_data_appeal_quota():
    """管理员：清零某玩家指定申诉池。body: {user_id, quota_pool}"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1DataAppealService.admin_reset_quota(
        body.get('user_id'),
        request.current_user.id,
        body.get('quota_pool') or 'standard',
    )
    if err:
        return error(err)
    return success(data, message='已重置该玩家申诉次数')


@adventurer_s1_bp.route('/admin/data-appeals/quota/add', methods=['POST'])
@login_required
@admin_required
def admin_add_data_appeal_quota():
    """管理员：给某玩家指定申诉池增加额度。body: {user_id, count, quota_pool}"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1DataAppealService.admin_add_quota(
        body.get('user_id'),
        body.get('count'),
        request.current_user.id,
        body.get('quota_pool') or 'standard',
    )
    if err:
        return error(err)
    return success(data, message='已增加申诉额度')


@adventurer_s1_bp.route('/admin/data-appeals/quota/reset-all', methods=['POST'])
@login_required
@admin_required
def admin_reset_all_data_appeal_quota():
    """管理员：全服重置指定申诉池。body: {quota_pool}"""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    body = request.get_json(silent=True) or {}
    data, err = AdventurerS1DataAppealService.admin_reset_all_quota(
        request.current_user.id,
        body.get('quota_pool') or 'standard',
    )
    if err:
        return error(err)
    return success(data, message='已全服重置申诉次数')
