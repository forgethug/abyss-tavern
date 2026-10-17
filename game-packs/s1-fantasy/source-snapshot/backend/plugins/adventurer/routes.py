from runtime_core.time import utc_now as _portable_utc_now
from flask import Blueprint, jsonify, request
from db_repository.manager import db_manager
from routes.auth import login_required
from services.currency_service import CurrencyService
from utils.response import success, error as err_resp, too_many_requests
from utils.pagination import get_pagination_params
from utils.time_utils import to_utc_iso
from datetime import datetime, timedelta
from models.user import User
from plugins.adventurer.models import AdventurerChatMessage
from extensions import db

adventurer_bp = Blueprint('adventurer', __name__, url_prefix='/api/adventurer')
admin_adventurer_bp = Blueprint('admin_adventurer', __name__, url_prefix='/api/admin')

def _emit_update(adventurer_ids=None, party_ids=None, log_ids=None, deleted_log_ids=None, chat_message_ids=None, reason=None):
    try:
        from plugins.adventurer.services.adventurer_log_service import emit_tavern_precise_updates
        emit_tavern_precise_updates(
            adventurer_ids=adventurer_ids,
            party_ids=party_ids,
            log_ids=log_ids,
            deleted_log_ids=deleted_log_ids,
            chat_message_ids=chat_message_ids,
            reason=reason
        )
    except Exception:
        pass

@adventurer_bp.route('/notice', methods=['GET'])
def get_notice():
    """获取深渊酒馆公告"""
    from models.system_config import SystemConfig
    config = SystemConfig.query.filter_by(config_key='adventurer_tavern_notice').first()
    notice = config.config_value if config else ''
    return jsonify({
        'code': 200,
        'data': {
            'notice': notice
        }
    })


@adventurer_bp.route('/auto-party-settings', methods=['GET'])
@login_required
def get_auto_party_settings_route():
    """获取自动建队限制设置（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    from plugins.adventurer.services.adventurer_service import get_auto_party_settings

    return jsonify({
        'code': 200,
        'data': get_auto_party_settings()
    })


@adventurer_bp.route('/auto-party-settings', methods=['POST'])
@login_required
def set_auto_party_settings_route():
    """保存自动建队限制设置（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    from plugins.adventurer.services.adventurer_service import set_auto_party_settings

    data = request.get_json() or {}
    raw_manual_limit_enabled = data.get('manual_limit_enabled', True)
    if isinstance(raw_manual_limit_enabled, str):
        manual_limit_enabled = raw_manual_limit_enabled.strip().lower() in ('1', 'true', 'yes', 'on')
    else:
        manual_limit_enabled = bool(raw_manual_limit_enabled)
    max_forming_parties = data.get('max_forming_parties', 1)

    result, error = set_auto_party_settings(
        manual_limit_enabled=manual_limit_enabled,
        max_forming_parties=max_forming_parties
    )
    if error:
        return jsonify({'code': 400, 'message': f'保存失败：{error}'})

    return jsonify({
        'code': 200,
        'message': '自动建队限制设置已保存',
        'data': result
    })


@adventurer_bp.route('/list', methods=['GET'])
def list_adventurers():
    """获取所有冒险者列表"""
    from utils.tavern_cache import get_cached_item_dict, get_cached_trait_dict, get_adventurer_list_cache

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 24, type=int)
    status_filter = request.args.get('status')
    show_dead = request.args.get('show_dead', 'false') == 'true'
    search = request.args.get('search')
    sort_field = request.args.get('sort_field')
    sort_order = request.args.get('sort_order')

    current_user_id = None
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if token:
        try:
            import jwt
            from flask import current_app
            payload = jwt.decode(token, current_app.config['JWT_SECRET_KEY'], algorithms=['HS256'])
            current_user_id = payload['user_id']
        except Exception:
            pass

    if hasattr(request, 'current_user') and request.current_user:
        current_user_id = request.current_user.id

    cache_key = f'adv:{page}:{per_page}:{status_filter}:{show_dead}:{current_user_id}:{search}:{sort_field}:{sort_order}'
    cache = get_adventurer_list_cache()
    cached_result, valid = cache.get(cache_key)
    if valid:
        return jsonify(cached_result)

    adventurers, total = db_manager.adventurer.list_paginated(
        page=page, per_page=per_page,
        status_filter=status_filter, show_dead=show_dead,
        current_user_id=current_user_id,
        search=search,
        sort_field=sort_field,
        sort_order=sort_order
    )

    item_dict = get_cached_item_dict()
    trait_dict = get_cached_trait_dict()

    followed_ids = set()
    if current_user_id:
        followed_ids = set(db_manager.adventurer_follow.get_followed_adventurer_ids(current_user_id))

    result = {
        'code': 200,
        'data': {
            'items': [adv.to_dict(item_dict=item_dict, trait_dict=trait_dict, is_followed=(adv.id in followed_ids)) for adv in adventurers],
            'total': total,
            'page': page,
            'pages': (total + per_page - 1) // per_page
        }
    }
    cache.set(cache_key, result)
    return jsonify(result)


@adventurer_bp.route('/batch', methods=['GET'])
def batch_get_adventurers():
    """批量获取指定 ID 的冒险者数据，供 Socket 增量更新使用。"""
    raw_ids = request.args.get('ids', '', type=str)
    if not raw_ids.strip():
        return jsonify({'code': 200, 'data': {'items': []}})

    try:
        adv_ids = list({int(part.strip()) for part in raw_ids.split(',') if part.strip()})
    except ValueError:
        return jsonify({'code': 400, 'message': 'ID 格式错误'}), 400

    if len(adv_ids) > 50:
        return jsonify({'code': 400, 'message': '单次最多查询 50 个'}), 400

    from utils.tavern_cache import get_cached_item_dict, get_cached_trait_dict

    current_user_id = None
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if token:
        try:
            import jwt
            from flask import current_app
            payload = jwt.decode(token, current_app.config['JWT_SECRET_KEY'], algorithms=['HS256'])
            current_user_id = payload['user_id']
        except Exception:
            pass

    if hasattr(request, 'current_user') and request.current_user:
        current_user_id = request.current_user.id

    item_dict = get_cached_item_dict()
    trait_dict = get_cached_trait_dict()

    followed_ids = set()
    if current_user_id:
        followed_ids = set(db_manager.adventurer_follow.get_followed_adventurer_ids(current_user_id))

    adventurers = db_manager.adventurer.find_all(id_in=adv_ids)
    items = [adv.to_dict(item_dict=item_dict, trait_dict=trait_dict, is_followed=(adv.id in followed_ids)) for adv in adventurers]
    return jsonify({'code': 200, 'data': {'items': items}})


@adventurer_bp.route('/parties', methods=['GET'])
def list_parties():
    """获取冒险者小队列表"""
    from utils.tavern_cache import get_cached_item_dict, get_cached_trait_dict, get_party_list_cache
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    status_filter = request.args.get('status', 'active', type=str)
    search = request.args.get('search')

    cache_key = f'party:{page}:{per_page}:{status_filter}:{search}'
    cache = get_party_list_cache()
    cached_result, valid = cache.get(cache_key)
    if valid:
        return jsonify(cached_result)

    parties, total = db_manager.adventurer_party.list_paginated(page=page, per_page=per_page, status_filter=status_filter, search=search)

    item_dict = get_cached_item_dict()
    trait_dict = get_cached_trait_dict()

    result = {
        'code': 200,
        'data': {
            'items': [p.to_dict(item_dict=item_dict, trait_dict=trait_dict) for p in parties],
            'total': total,
            'page': page,
            'pages': (total + per_page - 1) // per_page
        }
    }
    cache.set(cache_key, result)
    return jsonify(result)

@adventurer_bp.route('/logs', methods=['GET'])
def list_logs():
    """获取探险日志列表"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    party_name = request.args.get('party_name', type=str)
    include_details = request.args.get('include_details', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
    adventurer_id_raw = request.args.get('adventurer_id')
    adventurer_id = request.args.get('adventurer_id', type=int)
    if adventurer_id_raw is not None and adventurer_id is None:
        return err_resp('冒险者ID格式错误', 400)

    from plugins.adventurer.services.adventurer_log_service import list_expedition_logs

    result, error = list_expedition_logs(
        page=page,
        per_page=per_page,
        party_name=party_name,
        adventurer_id=adventurer_id,
        include_details=include_details,
    )
    if error:
        return err_resp(error, 400)
    return success(result)


@adventurer_bp.route('/logs/batch', methods=['GET'])
def get_log_details_batch():
    """批量获取多条探险日志详情"""
    raw_ids = request.args.get('ids', '', type=str)
    if not raw_ids.strip():
        return success({'items': [], 'missing_ids': []})

    try:
        log_ids = [int(part.strip()) for part in raw_ids.split(',') if part.strip()]
    except ValueError:
        return err_resp('日志ID格式错误', 400)

    from plugins.adventurer.services.adventurer_log_service import get_expedition_log_details_batch

    result, error = get_expedition_log_details_batch(log_ids)
    if error:
        return err_resp(error, 400)
    return success(result)


@adventurer_bp.route('/logs/<int:log_id>', methods=['GET'])
def get_log_detail(log_id):
    """获取单条探险日志详情"""
    from plugins.adventurer.services.adventurer_log_service import get_expedition_log_detail

    result, error = get_expedition_log_detail(log_id)
    if error:
        return err_resp(error, 404 if error == '日志不存在' else 400)
    return success(result)

@adventurer_bp.route('/generate', methods=['POST'])
@login_required
def generate_adventurers():
    """手动批量生成指定数量的冒险者（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    data = request.get_json() or {}
    count = int(data.get('count', 1))

    if count < 1 or count > 50:
        return jsonify({'code': 400, 'message': '生成数量需在1-50之间'})

    try:
        from plugins.adventurer.services.adventurer_party_service import generate_random_adventurers
        adventurers = generate_random_adventurers(count)
        _emit_update(adventurer_ids=[adv.id for adv in adventurers], reason='adventurer_generated')
        return jsonify({
            'code': 200,
            'message': f'成功生成了 {len(adventurers)} 名新的冒险者'
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': f'生成失败：{str(e)}'})

@adventurer_bp.route('/create-party', methods=['POST'])
@login_required
def create_party():
    """手动创建探险小队（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    data = request.get_json() or {}
    size = int(data.get('size', 4))
    dungeon = data.get('dungeon')

    if size < 2 or size > 8:
        return jsonify({'code': 400, 'message': '小队人数需在2-8人之间'})

    try:
        from flask import current_app
        created_party_id = None
        forming_parties = db_manager.adventurer_party.find_all(status='forming')
        needed_members_for_existing = 0
        for p in forming_parties:
            current_members = len(p.members)
            needed_members_for_existing += (p.target_size - current_members)

        idle_count = db_manager.adventurer.count(status='idle')

        if (idle_count - needed_members_for_existing) < 10:
            return jsonify({
                'code': 400,
                'message': f'当前空闲人数不足以支撑新小队。系统共有 {idle_count} 名空闲冒险者，但现有集结中的小队还需要 {needed_members_for_existing} 人才能满员，请等待他们集结完毕！'
            })

        with db_manager.transaction():
            import random
            from plugins.adventurer.services.adventurer_service import DUNGEONS

            if not dungeon or str(dungeon).strip() == '' or dungeon == '随机地下城':
                dungeon = random.choice(DUNGEONS)['name']

            from plugins.adventurer.models import AdventurerParty
            party = AdventurerParty(
                name=f"远征队_{random.randint(1000, 9999)}",
                target_dungeon=dungeon,
                target_size=size,
                status='forming',
            )
            db_manager.add(party)
            db_manager.flush()
            created_party_id = party.id

        _emit_update(party_ids=[created_party_id], reason='party_created')
        return jsonify({
            'code': 200,
            'message': f'成功创建了前往 {dungeon} 的招募小队，等待冒险者加入'
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': f'创建小队失败：{str(e)}'})

@adventurer_bp.route('/party/<int:party_id>/disband', methods=['POST'])
@login_required
def disband_party(party_id):
    """解散小队（限管理员，仅 forming 状态可解散）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    freed_adventurer_ids = []
    try:
        with db_manager.transaction():
            party = db_manager.adventurer_party.find_one(id=party_id, for_update=True)
            if not party:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '小队不存在'})

            if party.status != 'forming':
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '只能解散集结中的小队'})

            party.status = 'disbanded'

            members = db_manager.party_member.get_by_party(party.id)
            for pm in members:
                adv = db_manager.adventurer.find_one(id=pm.adventurer_id, for_update=True)
                if adv and adv.status == 'in_party':
                    adv.status = 'idle'
                    freed_adventurer_ids.append(adv.id)

        _emit_update(
            adventurer_ids=freed_adventurer_ids,
            party_ids=[party_id],
            reason='party_disbanded'
        )
        return jsonify({'code': 200, 'message': '小队已解散，成员已恢复空闲'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'解散失败：{str(e)}'})

@adventurer_bp.route('/party/<int:party_id>/gather', methods=['POST'])
@login_required
def gather_party(party_id):
    """强制集结冒险者加入小队（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    try:
        with db_manager.transaction():
            party = db_manager.adventurer_party.find_one(id=party_id, for_update=True)
            if not party:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '小队不存在'})

            if party.status != 'forming':
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '只能往集结中的小队拉人'})

            from plugins.adventurer.models import PartyMember
            current_members_count = db_manager.party_member.count_by_party(party.id)

            if current_members_count >= party.target_size:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': f'小队人数已达上限（{party.target_size}人）'})

            idle_adventurers = db_manager.adventurer.get_for_party()

            from plugins.adventurer.services.adventurer_service import DUNGEONS
            dungeon_level_map = {d['name']: d['min_level'] for d in DUNGEONS}
            p_min_lvl = dungeon_level_map.get(party.target_dungeon, 1)

            valid_adventurers = []
            for adv in idle_adventurers:
                if adv.level == 1 and p_min_lvl > 1:
                    continue
                valid_adventurers.append(adv)

            idle_adventurers = valid_adventurers

            if not idle_adventurers:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '当前没有任何符合地牢等级限制的可用冒险者（已排除休息状态和不符合队伍偏好的冒险者）'})

            import random
            random.shuffle(idle_adventurers)
            needed = party.target_size - current_members_count
            if needed <= 0:
                needed = 1

            actual_size = min(needed, len(idle_adventurers))
            selected_members = idle_adventurers[:actual_size]

            for m in selected_members:
                db_manager.adventurer.update_status(m.id, 'in_party', auto_commit=False)
                pm = PartyMember(party_id=party.id, adventurer_id=m.id)
                db_manager.add(pm)

            if current_members_count + actual_size >= party.target_size:
                from datetime import datetime, timedelta
                if not party.departure_time:
                    party.departure_time = _portable_utc_now() + timedelta(minutes=45)

        _emit_update(
            adventurer_ids=[m.id for m in selected_members],
            party_ids=[party.id],
            reason='party_gathered'
        )
        return jsonify({'code': 200, 'message': f'成功将 {actual_size} 名冒险者拉入小队'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'集结失败：{str(e)}'})

@adventurer_bp.route('/party/<int:party_id>/join', methods=['POST'])
@login_required
def join_party(party_id):
    """绑定者主动加入小队（暂时关闭功能）"""
    return jsonify({'code': 403, 'message': '手动加入队伍功能暂未开放'})

@adventurer_bp.route('/party/<int:party_id>/leave', methods=['POST'])
@login_required
def leave_party(party_id):
    """绑定者出发前退队（暂时关闭功能）"""
    return jsonify({'code': 403, 'message': '退队功能暂未开放'})

@adventurer_bp.route('/party/<int:party_id>/depart', methods=['POST'])
@login_required
def depart_party(party_id):
    """强制小队出发探险（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    try:
        from plugins.adventurer.services.adventurer_expedition_runner import send_parties_to_expedition
        import threading
        from flask import current_app

        with db_manager.transaction():
            party = db_manager.adventurer_party.find_one(id=party_id, for_update=True)
            if not party:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '小队不存在'})

            if party.status != 'forming':
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '小队当前状态无法出发'})

        app = current_app._get_current_object()

        def run_in_background(app, p_id):
            with app.app_context():
                send_parties_to_expedition(target_party_id=p_id)

        threading.Thread(target=run_in_background, args=(app, party_id)).start()

        return jsonify({'code': 200, 'message': '小队已出发进行探险！'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'出发失败：{str(e)}'})

@adventurer_bp.route('/party/<int:party_id>/sponsor', methods=['POST'])
@login_required
def sponsor_party(party_id):
    """赞助集结中的小队加速出发"""
    data = request.get_json() or {}
    amount = int(data.get('amount', 0))

    if amount < 1:
        return jsonify({'code': 400, 'message': '最低赞助金额为 1 金币'})

    current_user = request.current_user
    if current_user.balance < amount:
        return jsonify({'code': 400, 'message': '您的金币余额不足'})

    try:
        from services.currency_service import CurrencyService
        with db_manager.transaction():
            party = db_manager.adventurer_party.find_one(id=party_id, for_update=True)
            if not party:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '小队不存在'})

            from services.adventurer_closure_runner import (
                CLOSURE_PARTY_TARGET_DEFEND,
                CLOSURE_PARTY_TARGET_RETIRE,
            )
            if party.target_dungeon in (CLOSURE_PARTY_TARGET_DEFEND, CLOSURE_PARTY_TARGET_RETIRE):
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '闭馆集结队伍不支持赞助加速'})

            if party.status != 'forming':
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '该小队不在集结状态，无法加速'})

            current_members_count = len(party.members)
            if current_members_count < party.target_size:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': f'小队尚未集结完毕（当前 {current_members_count}/{party.target_size} 人），请等待满员后再进行赞助加速！'})

            threshold = current_members_count * 300000
            diff = threshold - party.sponsor_fund

            from datetime import datetime, timedelta
            if party.departure_time and party.departure_time <= _portable_utc_now():
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '小队已准备好出发，无需再赞助！'})

            if getattr(party, 'sponsor_reduced_minutes', 0) >= 30:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '该小队已获得最大加速效果（累计减免30分钟），每个小队最多只能加速一次！'})

            if amount > diff:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': f'该小队只差 {diff} 金币即可完成加速，请修改金额'})

            success, error = CurrencyService.deduct_currency(
                user_id=current_user.id,
                currency_code='gold',
                amount=amount,
                coin_source_type='sponsor_party_acceleration',
                coin_related_id=party.id,
                coin_related_type='adventurer_party',
                coin_description=f'赞助加速小队【{party.name}】',
                auto_commit=False
            )
            if error:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': error})

            party.sponsor_fund += amount

            times_accelerated = 0

            if party.sponsor_fund >= threshold and threshold > 0 and party.sponsor_reduced_minutes < 30:
                party.sponsor_reduced_minutes += 30
                times_accelerated += 1

                if party.departure_time:
                    party.departure_time -= timedelta(minutes=30)

        msg = f"成功赞助 {amount} 金币！"
        if times_accelerated > 0:
            msg += f" 累计赞助达标，小队加速了 {times_accelerated * 30} 分钟！"

        _emit_update(party_ids=[party_id], reason='party_sponsored')
        return jsonify({'code': 200, 'message': msg})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'赞助失败：{str(e)}'})

@adventurer_bp.route('/sponsor', methods=['POST'])
@login_required
def sponsor_adventurer():
    """普通玩家赞助出场（创建自定义冒险者）"""
    from plugins.adventurer.services.adventurer_service import get_max_adventurers
    from services.achievement_service import AchievementService
    from services.adventurer_closure_service import get_closure_state, STATE_PENDING
    current_user = request.current_user

    # 闭馆期间禁止赞助出场
    closure = get_closure_state()
    if closure.get('state') in (STATE_PENDING, 'running_defense', 'closed'):
        return jsonify({'code': 400, 'message': '酒馆已闭馆，暂停赞助出场'}), 400
    current_alive = db_manager.adventurer.get_alive_count()
    max_limit = get_max_adventurers()
    if current_alive >= max_limit:
        if not AchievementService.has_unlocked(current_user.id, 'lottery_special_prize'):
            return jsonify({'code': 400, 'message': f'当前酒馆人满为患（已达{max_limit}人上限），请等待有冒险者阵亡后再来赞助！'})

    data = request.get_json() or {}
    name = data.get('name', '').strip()
    char_class = data.get('character_class')
    personality = data.get('personality')
    gender = data.get('gender', '男')

    if not name:
        return jsonify({'code': 400, 'message': '请输入冒险者名字'})
    if len(name) < 2:
        return jsonify({'code': 400, 'message': '冒险者名字至少需要2个字符'})
    if len(name) > 20:
        return jsonify({'code': 400, 'message': '冒险者名字最多20个字符'})

    if not char_class or not personality:
        return jsonify({'code': 400, 'message': '请选择职业和性格'})

    from plugins.adventurer.services.adventurer_service import CLASSES, PERSONALITIES
    if char_class not in CLASSES:
        return jsonify({'code': 400, 'message': '无效的职业'})

    input_pers = personality.split(',')
    if len(input_pers) > 2:
        return jsonify({'code': 400, 'message': '最多只能选择 2 个性格'})

    for p in input_pers:
        if p.strip() not in PERSONALITIES:
            return jsonify({'code': 400, 'message': f'无效的性格: {p}'})

    existing_alive = db_manager.adventurer.find_one_by_owner(current_user.id, status_ne='dead')
    if existing_alive:
        return jsonify({'code': 400, 'message': f'您在深渊酒馆中已有存活的冒险者（{existing_alive.name}），无法同时拥有多个！'})

    existing = db_manager.adventurer.find_one(name=name)
    if existing:
        return jsonify({'code': 400, 'message': f'名字【{name}】已被使用，请换一个名字！'})

    SPONSOR_FEE = 3000000
    from services.currency_service import CurrencyService

    try:
        import random
        from plugins.adventurer.models import Adventurer

        with db_manager.transaction():
            current_balance, err_msg = CurrencyService.deduct_currency(
                current_user.id,
                'gold',
                SPONSOR_FEE,
                coin_source_type='sponsor_adventurer',
                coin_related_id=0,
                coin_related_type='adventurer',
                auto_commit=False
            )
            if err_msg:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': f'金币不足，赞助出场需要 {SPONSOR_FEE} 金币（{err_msg}）'})

            hp_val = random.randint(80, 150)

            adv = Adventurer(
                name=name,
                gender=gender,
                character_class=char_class,
                level=1,
                exp=0,
                hp=hp_val,
                max_hp=hp_val,
                strength=random.randint(5, 20),
                agility=random.randint(5, 20),
                intelligence=random.randint(5, 20),
                luck=random.randint(1, 10),
                personality=personality,
                equipment={"铁剑": {"type": "weapon", "desc": "普通的铁制长剑"}, "布甲": {"type": "armor", "desc": "基础的布制防具"}},
                gold=random.uniform(50000, 200000),
                owner_id=current_user.id,
                party_preference='auto'
            )

            db_manager.add(adv)

        _emit_update(adventurer_ids=[adv.id], reason='adventurer_sponsored')
        return jsonify({'code': 200, 'message': f'赞助成功（花费 3,000,000 金币）！冒险者 {name} 带着您的期望踏入了深渊酒馆！'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'赞助出场失败：{str(e)}'})

@adventurer_bp.route('/trigger-loop', methods=['POST'])
def trigger_loop():
    """手动触发一轮游戏循环（供测试或管理员使用）"""
    from plugins.adventurer.services.adventurer_expedition_runner import auto_game_loop
    auto_game_loop()
    return jsonify({
        'code': 200,
        'message': '游戏循环已触发'
    })


@adventurer_bp.route('/trigger-restock', methods=['POST'])
@login_required
def trigger_restock():
    """手动触发AI为商店进货（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    try:
        data = request.get_json(silent=True) or {}
        rarity = data.get('rarity') or 'all'
        kinds = int(data.get('kinds') or 0)
        stock = int(data.get('stock') or 0)

        from plugins.adventurer.services.adventurer_shop_service import ai_restock_items, initialize_shop_items, restock_basic_consumables
        initialize_shop_items()
        restock_basic_consumables()

        custom_params = None
        if rarity != 'all' and kinds > 0 and stock > 0:
            custom_params = {
                'rarity': rarity,
                'kinds': kinds,
                'stock': stock
            }

        ai_restock_items(force=True, custom_params=custom_params)
        return jsonify({
            'code': 200,
            'message': 'AI 进货指令已发送，请稍后刷新商店页面查看全新商品！'
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': f'进货失败：{str(e)}'})

@adventurer_bp.route('/<int:adv_id>/invest', methods=['POST'])
@login_required
def invest_adventurer(adv_id):
    """玩家投资给冒险者"""
    data = request.get_json(silent=True) or {}
    amount = float(data.get('amount') or 0)

    if amount < 1:
        return jsonify({'code': 400, 'message': '最低投资1金币'})

    current_user = request.current_user
    if current_user.balance < amount:
        return jsonify({'code': 400, 'message': '余额不足'})

    try:
        current_balance, err_msg = CurrencyService.deduct_currency(
            user_id=current_user.id,
            currency_code='gold',
            amount=amount,
            coin_description=f'投资深渊酒馆冒险者',
            coin_source_type='invest_adventurer',
            auto_commit=False
        )
        if err_msg:
            return jsonify({'code': 400, 'message': err_msg})

        with db_manager.transaction():
            adv_for_update = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv_for_update:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '未找到该冒险者'})

            if adv_for_update.hp <= 0:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '无法投资已阵亡的冒险者'})

            adv_for_update.gold = (adv_for_update.gold or 0) + amount

            current_investors = dict(adv_for_update.investors) if adv_for_update.investors else {}
            investor_name = current_user.nickname or current_user.username
            current_investors[investor_name] = current_investors.get(investor_name, 0) + amount

            adv_for_update.investors = current_investors

        _emit_update(adventurer_ids=[adv_id], reason='adventurer_invested')
        return jsonify({
            'code': 200,
            'message': f'成功投资了 {amount} 金币！',
            'new_balance': current_balance
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': f'投资失败：{str(e)}'})

@adventurer_bp.route('/<int:adv_id>/fund-icu', methods=['POST'])
@login_required
def fund_icu(adv_id):
    """玩家向冒险者ICU基金存入金币"""
    data = request.get_json()
    amount = float(data.get('amount', 0))

    if amount < 1:
        return jsonify({'code': 400, 'message': '最低赞助1金币'})

    current_user = request.current_user
    if current_user.balance < amount:
        return jsonify({'code': 400, 'message': '余额不足'})

    try:
        with db_manager.transaction():
            adv_for_update = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv_for_update:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '未找到该冒险者'})

            if adv_for_update.hp <= 0:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '无法救治已阵亡的冒险者'})

            if adv_for_update.icu_fund >= 10000000:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '该冒险者的ICU治疗基金已达上限（1000w）'})

            actual_amount = min(amount, 10000000 - adv_for_update.icu_fund)

            current_balance, err_msg = CurrencyService.deduct_currency(
                user_id=current_user.id,
                currency_code='gold',
                amount=actual_amount,
                coin_description=f'存入冒险者ICU治疗基金',
                coin_source_type='fund_adventurer_icu',
                auto_commit=False
            )
            if err_msg:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': err_msg})

            adv_for_update.icu_fund += actual_amount

        _emit_update(adventurer_ids=[adv_id], reason='adventurer_icu_funded')
        return jsonify({
            'code': 200,
            'message': f'成功存入 {actual_amount} 金币到ICU治疗基金！',
            'new_balance': current_balance
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': f'存入失败：{str(e)}'})

@adventurer_bp.route('/log/<int:log_id>/force-complete', methods=['POST'])
@login_required
def force_complete_expedition(log_id):
    """强制立即结算探险日志（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    try:
        with db_manager.transaction():
            log = db_manager.expedition_log.find_one(id=log_id, for_update=True)
            if not log:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '日志不存在'})

            full_log = dict(log.full_log)
            if full_log.get('status') != 'in_progress':
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '该探险不处于进行中状态'})

            events = full_log.get('events', [])
            full_log['current_event_index'] = len(events)
            log.full_log = full_log

        from plugins.adventurer.services.adventurer_expedition_runner import progress_expeditions
        progress_expeditions()

        return jsonify({'code': 200, 'message': '探险已强制结算完成'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'强制结算失败：{str(e)}'})

@adventurer_bp.route('/log/<int:log_id>/step', methods=['POST'])
@login_required
def step_expedition(log_id):
    """手动推进一条探险日志进度（限管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'})

    try:
        party_id = None
        is_closure = False
        with db_manager.transaction():
            log = db_manager.expedition_log.find_one(id=log_id)
            if not log:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '日志不存在'})

            full_log = dict(log.full_log)
            if full_log.get('status') != 'in_progress':
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '该探险已结束，无法推进'})

            party_id = log.party_id
            # closure 占位日志走专门的推进入口：普通 progress_expeditions 会过滤掉
            # closure 队伍（target_dungeon ∈ 退休归隐 / 酒馆守卫战），点了没反应。
            is_closure = full_log.get('closure_log_type') in {'closure_retire', 'closure_defend'}

        if is_closure:
            from services.adventurer_closure_runner import step_closure_party_log
            ok, err = step_closure_party_log(log_id)
            if not ok:
                return jsonify({'code': 400, 'message': err or '推进失败'})
            return jsonify({'code': 200, 'message': '已成功推进一段闭馆日志'})

        if party_id:
            from plugins.adventurer.services.adventurer_expedition_runner import progress_expeditions
            progress_expeditions(target_party_id=party_id)

        return jsonify({'code': 200, 'message': '已成功推进一条事件'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'推进失败：{str(e)}'})


@adventurer_bp.route('/<int:adv_id>/follow', methods=['POST'])
@login_required
def follow_adventurer(adv_id):
    """关注冒险者"""
    current_user = request.current_user
    try:
        follow, error = db_manager.adventurer_follow.follow(current_user.id, adv_id)
        if error:
            return jsonify({'code': 400, 'message': error})
        return jsonify({'code': 200, 'message': '关注成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'关注失败：{str(e)}'})


@adventurer_bp.route('/<int:adv_id>/unfollow', methods=['POST'])
@login_required
def unfollow_adventurer(adv_id):
    """取消关注冒险者"""
    current_user = request.current_user
    try:
        result, error = db_manager.adventurer_follow.unfollow(current_user.id, adv_id)
        if error:
            return jsonify({'code': 400, 'message': error})
        return jsonify({'code': 200, 'message': '取消关注成功'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'取消关注失败：{str(e)}'})


@adventurer_bp.route('/<int:adv_id>/party-preference', methods=['POST'])
@login_required
def set_party_preference(adv_id):
    """设置冒险者的队伍偏好"""
    current_user = request.current_user
    data = request.get_json() or {}
    preference = data.get('preference', 'auto')

    valid_preferences = ['auto', 'idle', 'rest']
    if preference not in valid_preferences:
        return jsonify({'code': 400, 'message': f'无效的偏好设置，可选值：{valid_preferences}'})

    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '冒险者不存在'})

            if adv.owner_id != current_user.id:
                db_manager.rollback()
                return jsonify({'code': 403, 'message': '只有绑定的玩家才能修改队伍偏好'})

            if adv.status not in ['idle', 'resting']:
                db_manager.rollback()
                status_names = {'idle': '空闲', 'resting': '休息'}
                return jsonify({'code': 400, 'message': f'只有{status_names["idle"]}或{status_names["resting"]}状态才能修改队伍偏好'})

            if preference == 'rest':
                if adv.rest_start_time:
                    from datetime import datetime, timedelta
                    if _portable_utc_now() >= adv.rest_start_time + timedelta(hours=1):
                        db_manager.rollback()
                        return jsonify({'code': 400, 'message': '满血休息时间已达1小时上限，强制出战中。下一次副本结束前不允许再休息！'})
                if adv.hp >= adv.max_hp and adv.party_preference != 'rest':
                    from datetime import datetime
                    adv.rest_start_time = _portable_utc_now()
            else:
                pass

            adv.party_preference = preference
            preference_names = {'auto': '自动', 'idle': '空闲', 'rest': '休息'}
            msg = f'已将冒险者【{adv.name}】的队伍偏好设置为【{preference_names[preference]}】'

        _emit_update(adventurer_ids=[adv.id], reason='party_preference_changed')
        return jsonify({'code': 200, 'message': msg})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败：{str(e)}'})


@adventurer_bp.route('/<int:adv_id>/warehouse/withdraw', methods=['POST'])
@login_required
def withdraw_from_warehouse(adv_id):
    """从收藏室取出物品到装备栏（管理员可操作任何人）"""
    current_user = request.current_user
    data = request.get_json() or {}
    item_index = data.get('index')

    if item_index is None:
        return jsonify({'code': 400, 'message': '缺少物品索引'})

    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '冒险者不存在'})

            is_admin = getattr(current_user, 'is_admin', False)

            if adv.owner_id != current_user.id and not is_admin:
                db_manager.rollback()
                return jsonify({'code': 403, 'message': '只有绑定的玩家才能操作收藏室'})

            if not is_admin and adv.status not in ['in_party', 'idle', 'resting']:
                status_names = {'in_party': '组队中', 'idle': '空闲', 'resting': '休息', 'exploring': '探险中', 'dead': '已阵亡'}
                return jsonify({'code': 400, 'message': f'当前状态为【{status_names.get(adv.status, adv.status)}】，无法取出物品'})

            warehouse = list(adv.warehouse or [])
            if item_index < 0 or item_index >= len(warehouse):
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '物品索引无效'})

            # 背包硬上限检查：取出 1 件后总数不能超过 ADVENTURER_INVENTORY_HARD_LIMIT
            from flask import current_app
            inventory_limit = current_app.config.get('ADVENTURER_INVENTORY_HARD_LIMIT', 30)
            current_equipment_count = len(adv.equipment or {})
            if current_equipment_count + 1 > inventory_limit:
                db_manager.rollback()
                return jsonify({
                    'code': 400,
                    'message': f'背包已有 {current_equipment_count} 件，取出后将超过 {inventory_limit} 件上限，请先把背包里的物品存进收藏室'
                })

            item = warehouse.pop(item_index)
            adv.warehouse = warehouse
            from sqlalchemy import orm
            orm.attributes.flag_modified(adv, 'warehouse')

            equipment = dict(adv.equipment or {})
            item_name = item.get('name')
            if item_name:
                item_type = item.get('type')
                item_rarity = item.get('rarity')
                item_desc = item.get('desc', '')

                if not item_type or not item_rarity:
                    from utils.tavern_cache import get_cached_item_dict
                    global_item_dict = get_cached_item_dict()
                    if item_name in global_item_dict:
                        item_model = db_manager.adventurer_item.get_by_name(item_name)
                        if item_model:
                            if not item_type: item_type = item_model.type
                            if not item_rarity: item_rarity = item_model.rarity
                            if not item_desc: item_desc = item_model.description

                equipment[item_name] = {
                    'type': item_type or 'consumable',
                    'desc': item_desc,
                    'uses_left': item.get('uses_left'),
                    'rarity': item_rarity or 'common',
                    'locked': False
                }
                adv.equipment = equipment
                orm.attributes.flag_modified(adv, 'equipment')

        _emit_update(adventurer_ids=[adv.id], reason='warehouse_withdrawn')
        action = '管理员从' if is_admin and adv.owner_id != current_user.id else ''
        return jsonify({'code': 200, 'message': f'{action}收藏室取出了【{item_name}】'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败：{str(e)}'})


@adventurer_bp.route('/<int:adv_id>/warehouse/withdraw-batch', methods=['POST'])
@login_required
def withdraw_from_warehouse_batch(adv_id):
    """批量从收藏室取出物品到装备栏。
    body: {item_names: ['名字1', '名字2', ...]}
    取出后背包总数若 > ADVENTURER_INVENTORY_HARD_LIMIT 整批拒绝。
    """
    current_user = request.current_user
    data = request.get_json() or {}
    item_names = data.get('item_names') or []
    if not isinstance(item_names, list) or not item_names:
        return jsonify({'code': 400, 'message': '请勾选要取出的物品'})
    # 去重保序
    seen = set()
    unique_names = []
    for n in item_names:
        if isinstance(n, str) and n and n not in seen:
            seen.add(n)
            unique_names.append(n)
    if not unique_names:
        return jsonify({'code': 400, 'message': '请勾选要取出的物品'})

    try:
        from flask import current_app
        inventory_limit = current_app.config.get('ADVENTURER_INVENTORY_HARD_LIMIT', 30)
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '冒险者不存在'})

            is_admin = getattr(current_user, 'is_admin', False)
            if adv.owner_id != current_user.id and not is_admin:
                db_manager.rollback()
                return jsonify({'code': 403, 'message': '只有绑定的玩家才能操作收藏室'})

            if not is_admin and adv.status not in ['in_party', 'idle', 'resting']:
                status_names = {'in_party': '组队中', 'idle': '空闲', 'resting': '休息',
                                'exploring': '探险中', 'dead': '已阵亡'}
                return jsonify({'code': 400,
                                'message': f'当前状态为【{status_names.get(adv.status, adv.status)}】，无法取出物品'})

            warehouse = list(adv.warehouse or [])
            equipment = dict(adv.equipment or {})
            current_equipment_count = len(equipment)

            # 整批超限拒绝
            if current_equipment_count + len(unique_names) > inventory_limit:
                db_manager.rollback()
                return jsonify({
                    'code': 400,
                    'message': f'背包已有 {current_equipment_count} 件，本次将再取 {len(unique_names)} 件超过 {inventory_limit} 件上限，请先存仓再取出或减少勾选'
                })

            # 收集匹配的物品（按名字，从仓库前往后第一次匹配）
            picked = []
            remaining_warehouse = []
            wanted = list(unique_names)
            for w_item in warehouse:
                if not isinstance(w_item, dict):
                    remaining_warehouse.append(w_item)
                    continue
                w_name = w_item.get('name')
                if w_name in wanted and w_name not in equipment:
                    picked.append(w_item)
                    wanted.remove(w_name)
                else:
                    remaining_warehouse.append(w_item)

            if not picked:
                db_manager.rollback()
                return jsonify({'code': 400, 'message': '勾选的物品都不在收藏室里，或同名装备已经在背包里'})

            from sqlalchemy import orm
            from utils.tavern_cache import get_cached_item_dict
            global_item_dict = get_cached_item_dict()
            for item in picked:
                item_name = item.get('name')
                item_type = item.get('type')
                item_rarity = item.get('rarity')
                item_desc = item.get('desc', '')
                if not item_type or not item_rarity:
                    if item_name in global_item_dict:
                        item_model = db_manager.adventurer_item.get_by_name(item_name)
                        if item_model:
                            if not item_type:
                                item_type = item_model.type
                            if not item_rarity:
                                item_rarity = item_model.rarity
                            if not item_desc:
                                item_desc = item_model.description
                equipment[item_name] = {
                    'type': item_type or 'consumable',
                    'desc': item_desc,
                    'uses_left': item.get('uses_left'),
                    'rarity': item_rarity or 'common',
                    'locked': False,
                }

            adv.warehouse = remaining_warehouse
            adv.equipment = equipment
            orm.attributes.flag_modified(adv, 'warehouse')
            orm.attributes.flag_modified(adv, 'equipment')

        _emit_update(adventurer_ids=[adv.id], reason='warehouse_withdrawn')
        picked_names = [p.get('name') for p in picked]
        missing = [n for n in unique_names if n not in picked_names]
        msg_parts = [f'已取出 {len(picked_names)} 件：{", ".join(picked_names)}']
        if missing:
            msg_parts.append(f'未找到 {len(missing)} 件：{", ".join(missing)}')
        return jsonify({
            'code': 200,
            'message': '；'.join(msg_parts),
            'data': {'picked': picked_names, 'missing': missing},
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败：{str(e)}'})


@adventurer_bp.route('/<int:adv_id>/warehouse/deposit-batch', methods=['POST'])
@login_required
def deposit_to_warehouse_batch(adv_id):
    """批量把装备栏物品存入收藏室。
    body: {item_names: ['名字1', '名字2', ...]}
    锁定的装备会被跳过、不会被存入；返回里告知跳过名单。
    """
    current_user = request.current_user
    data = request.get_json() or {}
    item_names = data.get('item_names') or []
    if not isinstance(item_names, list) or not item_names:
        return jsonify({'code': 400, 'message': '请勾选要存入的物品'})
    seen = set()
    unique_names = []
    for n in item_names:
        if isinstance(n, str) and n and n not in seen:
            seen.add(n)
            unique_names.append(n)
    if not unique_names:
        return jsonify({'code': 400, 'message': '请勾选要存入的物品'})

    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '冒险者不存在'})

            is_admin = getattr(current_user, 'is_admin', False)
            if adv.owner_id != current_user.id and not is_admin:
                db_manager.rollback()
                return jsonify({'code': 403, 'message': '只有绑定的玩家才能操作收藏室'})

            if not is_admin and adv.status not in ['in_party', 'idle', 'resting']:
                status_names = {'in_party': '组队中', 'idle': '空闲', 'resting': '休息',
                                'exploring': '探险中', 'dead': '已阵亡'}
                return jsonify({'code': 400,
                                'message': f'当前状态为【{status_names.get(adv.status, adv.status)}】，无法存入物品'})

            equipment = dict(adv.equipment or {})
            warehouse = list(adv.warehouse or [])

            deposited = []
            skipped_locked = []
            missing = []
            for name in unique_names:
                eq_data = equipment.get(name)
                if not isinstance(eq_data, dict):
                    missing.append(name)
                    continue
                if eq_data.get('locked'):
                    skipped_locked.append(name)
                    continue
                warehouse.append({
                    'name': name,
                    'type': eq_data.get('type', 'consumable'),
                    'desc': eq_data.get('desc', ''),
                    'rarity': eq_data.get('rarity', 'common'),
                    'uses_left': eq_data.get('uses_left'),
                })
                del equipment[name]
                deposited.append(name)

            if not deposited:
                db_manager.rollback()
                hint = []
                if skipped_locked:
                    hint.append(f'{len(skipped_locked)} 件被锁定无法存入')
                if missing:
                    hint.append(f'{len(missing)} 件不在背包里')
                return jsonify({'code': 400,
                                'message': '没有可存入的物品；' + '、'.join(hint) if hint else '没有可存入的物品'})

            from sqlalchemy import orm
            adv.warehouse = warehouse
            adv.equipment = equipment
            orm.attributes.flag_modified(adv, 'warehouse')
            orm.attributes.flag_modified(adv, 'equipment')

        _emit_update(adventurer_ids=[adv.id], reason='warehouse_deposited')
        msg_parts = [f'已存入 {len(deposited)} 件：{", ".join(deposited)}']
        if skipped_locked:
            msg_parts.append(f'锁定跳过 {len(skipped_locked)} 件：{", ".join(skipped_locked)}')
        if missing:
            msg_parts.append(f'背包里没找到 {len(missing)} 件：{", ".join(missing)}')
        return jsonify({
            'code': 200,
            'message': '；'.join(msg_parts),
            'data': {'deposited': deposited, 'skipped_locked': skipped_locked, 'missing': missing},
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败：{str(e)}'})


@adventurer_bp.route('/<int:adv_id>/equipment/lock', methods=['POST'])
@login_required
def toggle_equipment_lock(adv_id):
    """锁定或解锁装备"""
    current_user = request.current_user
    data = request.get_json() or {}
    item_name = data.get('item_name')
    lock = data.get('lock', True)

    if not item_name:
        return jsonify({'code': 400, 'message': '缺少物品名称'})

    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '冒险者不存在'})

            if adv.owner_id != current_user.id:
                db_manager.rollback()
                return jsonify({'code': 403, 'message': '只有绑定的玩家才能操作装备'})

            equipment = adv.equipment or {}
            if item_name not in equipment:
                db_manager.rollback()
                return jsonify({'code': 404, 'message': '装备不存在'})

            if lock:
                locked_count = sum(1 for e in equipment.values() if e.get('locked'))
                if locked_count >= 10:
                    return jsonify({'code': 400, 'message': '最多只能锁定10件装备'})

            equipment[item_name]['locked'] = lock
            adv.equipment = equipment
            from sqlalchemy import orm
            orm.attributes.flag_modified(adv, 'equipment')

        _emit_update(adventurer_ids=[adv.id], reason='equipment_lock_changed')
        action = '锁定' if lock else '解锁'
        return jsonify({'code': 200, 'message': f'已{action}装备【{item_name}】'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败：{str(e)}'})


@adventurer_bp.route('/chat', methods=['GET'])
@login_required
def get_adventurer_chat_messages():
    page, per_page = get_pagination_params(default_per_page=30)
    total_limit = 100
    offset = (page - 1) * per_page
    if offset >= total_limit:
        return jsonify({
            'code': 200,
            'message': 'success',
            'data': {
                'messages': [],
                'total': total_limit, 'page': page, 'per_page': per_page,
                'has_next': False
            }
        })

    base_query = AdventurerChatMessage.query.order_by(AdventurerChatMessage.created_at.desc()).limit(total_limit)
    all_recent = base_query.all()
    total = len(all_recent)
    page_items = all_recent[offset:offset + per_page]

    user_ids = list(set(m.user_id for m in page_items if m.user_id))
    users_map = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

    result = []
    for m in page_items:
        user = users_map.get(m.user_id)
        result.append({
            'id': m.id,
            'user_id': m.user_id,
            'nickname': (user.nickname or user.username) if user else '未知',
            'active_title': user.active_title if user else None,
            'content': m.content,
            'created_at': to_utc_iso(m.created_at),
        })
    has_next = (offset + per_page) < total
    return jsonify({
        'code': 200,
        'message': 'success',
        'data': {
            'messages': result,
            'total': total, 'page': page, 'per_page': per_page,
            'has_next': has_next
        }
    })


@adventurer_bp.route('/chat', methods=['POST'])
@login_required
def post_adventurer_chat_message():
    user = request.current_user
    data = request.get_json()
    content = data.get('content', '').strip()

    if not content:
        return jsonify({'code': 400, 'message': '内容不能为空'})
    if len(content) > 300:
        return jsonify({'code': 400, 'message': '内容不能超过300字'})

    last_msg = AdventurerChatMessage.query.filter_by(user_id=user.id).order_by(AdventurerChatMessage.created_at.desc()).first()
    if last_msg and last_msg.created_at:
        elapsed = (_portable_utc_now() - last_msg.created_at).total_seconds()
        if elapsed < 300:
            remaining = int(300 - elapsed)
            return jsonify({
                'code': 429,
                'message': f'发言冷却中，请{remaining}秒后再试',
                'data': {'retry_after': remaining}
            })

    msg = AdventurerChatMessage(user_id=user.id, content=content)
    db_manager.add(msg)
    try:
        db_manager.commit()
    except Exception as e:
        db_manager.rollback()
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f'发送深渊酒馆聊天室消息失败: {e}')
        return jsonify({'code': 500, 'message': '操作失败，请重试'})

    _emit_update(chat_message_ids=[msg.id], reason='chat_message_created')

    return jsonify({
        'code': 200,
        'message': '发言成功',
        'data': {
            'id': msg.id,
            'content': msg.content,
            'created_at': to_utc_iso(msg.created_at)
        }
    })


@adventurer_bp.route('/<int:adv_id>/claim', methods=['POST'])
@login_required
def claim_adventurer(adv_id):
    """玩家申请认领冒险者"""
    from plugins.adventurer.models import AdventurerClaim

    current_user = request.current_user

    existing_owned = db_manager.adventurer.find_one_by_owner(current_user.id, status_ne='dead')
    if existing_owned:
        return jsonify({'code': 400, 'message': f'您已有绑定的冒险者（{existing_owned.name}），无法认领更多'})

    existing_claim = AdventurerClaim.query.filter_by(
        user_id=current_user.id,
        status='pending'
    ).first()
    if existing_claim:
        return jsonify({'code': 400, 'message': '您已有待审核的认领申请，请等待管理员处理'})

    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id)
            if not adv:
                return jsonify({'code': 404, 'message': '冒险者不存在'}), 404

            if adv.owner_id:
                return jsonify({'code': 400, 'message': '该冒险者已被绑定'}), 400

            existing = AdventurerClaim.query.filter_by(
                user_id=current_user.id,
                adventurer_id=adv_id
            ).first()
            if existing:
                if existing.status == 'pending':
                    return jsonify({'code': 400, 'message': '您已有该冒险者的待审核申请'})
                elif existing.status == 'approved':
                    return jsonify({'code': 400, 'message': '您已成功绑定该冒险者'})
                elif existing.status == 'rejected':
                    existing.status = 'pending'
                    existing.created_at = _portable_utc_now()
                    existing.reject_reason = None
                else:
                    return jsonify({'code': 400, 'message': '申请状态异常'}), 400
            else:
                claim = AdventurerClaim(
                    user_id=current_user.id,
                    adventurer_id=adv_id,
                    status='pending'
                )
                db_manager.add(claim)

        return jsonify({'code': 200, 'message': '认领申请已提交，请等待管理员审核'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'申请失败：{str(e)}'}), 500


@adventurer_bp.route('/claims', methods=['GET'])
@login_required
def list_claims():
    """获取认领申请列表（管理员）"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可查看'}), 403

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    status_filter = request.args.get('status')

    from plugins.adventurer.models import AdventurerClaim
    query = AdventurerClaim.query

    if status_filter:
        query = query.filter_by(status=status_filter)

    pagination = query.order_by(AdventurerClaim.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'code': 200,
        'data': {
            'items': [claim.to_dict() for claim in pagination.items],
            'total': pagination.total,
            'page': page,
            'pages': pagination.pages
        }
    })


@adventurer_bp.route('/claims/<int:claim_id>/approve', methods=['POST'])
@login_required
def approve_claim(claim_id):
    """管理员批准认领申请"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'}), 403

    from plugins.adventurer.models import AdventurerClaim

    try:
        with db_manager.transaction():
            claim = AdventurerClaim.query.session.get(AdventurerClaim, claim_id)
            if not claim:
                return jsonify({'code': 404, 'message': '申请不存在'}), 404

            if claim.status != 'pending':
                return jsonify({'code': 400, 'message': f'申请状态为{claim.status}，无法审核'}), 400

            adv = db_manager.adventurer.find_one(id=claim.adventurer_id, for_update=True)
            if not adv:
                return jsonify({'code': 404, 'message': '冒险者不存在'}), 404

            if adv.owner_id:
                return jsonify({'code': 400, 'message': '该冒险者已被绑定'}), 400

            existing_alive = db_manager.adventurer.find_one_by_owner(claim.user_id, status_ne='dead')
            if existing_alive:
                return jsonify({'code': 400, 'message': f'该用户已有存活冒险者（{existing_alive.name}）'}), 400

            adv.owner_id = claim.user_id
            claim.status = 'approved'
            claim.reviewed_at = _portable_utc_now()
            claim.reviewed_by = request.current_user.id

        return jsonify({'code': 200, 'message': f'已批准认领申请，冒险者【{adv.name}】已绑定到用户'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败：{str(e)}'}), 500


@adventurer_bp.route('/claims/<int:claim_id>/reject', methods=['POST'])
@login_required
def reject_claim(claim_id):
    """管理员拒绝认领申请"""
    if not getattr(request.current_user, 'is_admin', False):
        return jsonify({'code': 403, 'message': '仅管理员可执行'}), 403

    from plugins.adventurer.models import AdventurerClaim
    data = request.get_json() or {}
    reason = data.get('reason', '')

    try:
        claim = AdventurerClaim.query.session.get(AdventurerClaim, claim_id)
        if not claim:
            return jsonify({'code': 404, 'message': '申请不存在'}), 404

        if claim.status != 'pending':
            return jsonify({'code': 400, 'message': f'申请状态为{claim.status}，无法审核'}), 400

        claim.status = 'rejected'
        claim.reviewed_at = _portable_utc_now()
        claim.reviewed_by = request.current_user.id
        claim.reject_reason = reason

        return jsonify({'code': 200, 'message': '已拒绝认领申请'})
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败：{str(e)}'}), 500


@adventurer_bp.route('/my-claim', methods=['GET'])
@login_required
def get_my_claim():
    """获取当前用户的认领申请状态"""
    from plugins.adventurer.models import AdventurerClaim

    current_user = request.current_user
    claim = AdventurerClaim.query.filter_by(user_id=current_user.id).order_by(AdventurerClaim.created_at.desc()).first()

    if not claim:
        return jsonify({'code': 200, 'data': None})

    return jsonify({'code': 200, 'data': claim.to_dict()})


# ── 深渊酒馆关闭流程：玩家端接口（Phase 2） ─────────────────────────────────
# GM 接口在 routes/admin/game.py，状态机说明见
# services/adventurer_closure_service.py 头部注释与 docs/adventurer_design.md §9。

@adventurer_bp.route('/closure/status', methods=['GET'])
def closure_status():
    """读取当前 closure 全局状态。公开可访问，便于未登录用户看到 banner。"""
    from services.adventurer_closure_service import get_closure_state
    return jsonify({'code': 200, 'data': get_closure_state()})


@adventurer_bp.route('/closure/summary', methods=['GET'])
def closure_summary():
    """选择窗口期统计：归隐 / 守护 / 未决 / 强制归隐数。"""
    from services.adventurer_closure_service import get_choice_summary
    return jsonify({'code': 200, 'data': get_choice_summary()})


@adventurer_bp.route('/closure/list', methods=['GET'])
def closure_list():
    """列出所有未归档冒险者的选择，便于「看到别人的选择」面板。"""
    from services.adventurer_closure_service import list_choices
    return jsonify({'code': 200, 'data': list_choices()})


@adventurer_bp.route('/closure/choose', methods=['POST'])
@login_required
def closure_choose():
    """玩家为名下绑定冒险者选择「归隐」或「守护」。仅 pending 状态可用。"""
    from services.adventurer_closure_service import choose_for_adventurer
    data = request.get_json(silent=True) or {}
    adventurer_id = data.get('adventurer_id')
    choice = data.get('choice')

    if not isinstance(adventurer_id, int):
        try:
            adventurer_id = int(adventurer_id) if adventurer_id is not None else None
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'message': 'adventurer_id 必须是整数'}), 400
    if not adventurer_id or adventurer_id <= 0:
        return jsonify({'code': 400, 'message': '请提供合法的 adventurer_id'}), 400

    result, error = choose_for_adventurer(
        user_id=request.current_user.id,
        adventurer_id=adventurer_id,
        choice=choice,
    )
    if error:
        return jsonify({'code': 400, 'message': error, 'data': None}), 400
    return jsonify({'code': 200, 'message': '已记录选择', 'data': result})
# ═══════════════════════════════════════════════════════════════════════════════
# Admin 路由 — 从 routes/admin/adventurer_admin.py 迁移到插件内部
# ═══════════════════════════════════════════════════════════════════════════════

from utils.decorators import admin_required


# ── 系统开关 ──────────────────────────────────────────────────────────────────

@admin_adventurer_bp.route('/auto-party/status', methods=['GET'])
@login_required
@admin_required
def admin_get_auto_party_status():
    from plugins.adventurer.services.adventurer_config_service import get_auto_party_settings
    settings = get_auto_party_settings()
    return jsonify({'code': 200, 'message': 'success', 'data': {'enabled': settings['auto_party_enabled']}}), 200


@admin_adventurer_bp.route('/auto-party/toggle', methods=['POST'])
@login_required
@admin_required
def admin_toggle_auto_party():
    from models.system_config import SystemConfig
    data = request.get_json() or {}
    enabled = bool(data.get('enabled', True))

    config = db_manager.system_config.find_one(config_key='adventurer_auto_party')
    if not config:
        config = SystemConfig(config_key='adventurer_auto_party', config_value='true' if enabled else 'false')
        db_manager.add(config)
    else:
        config.config_value = 'true' if enabled else 'false'

    ok, err = db_manager.commit()
    if not ok:
        return jsonify({'code': 500, 'message': err}), 500

    return jsonify({'code': 200, 'message': '设置成功', 'data': {'enabled': enabled}}), 200


@admin_adventurer_bp.route('/adventurer-berserk/status', methods=['GET'])
@login_required
@admin_required
def admin_get_adventurer_berserk_status():
    from plugins.adventurer.services.adventurer_config_service import is_berserk_mode_enabled
    enabled = is_berserk_mode_enabled()
    return jsonify({'code': 200, 'message': 'success', 'data': {'enabled': enabled}}), 200


@admin_adventurer_bp.route('/adventurer-berserk/toggle', methods=['POST'])
@login_required
@admin_required
def admin_toggle_adventurer_berserk():
    from plugins.adventurer.services.adventurer_config_service import set_berserk_mode_enabled
    data = request.get_json() or {}
    enabled = bool(data.get('enabled', False))
    result, err = set_berserk_mode_enabled(enabled)
    if err:
        return jsonify({'code': 500, 'message': err}), 500
    msg = '狂暴模式已开启（赛季末配置生效）' if enabled else '狂暴模式已关闭'
    return jsonify({'code': 200, 'message': msg, 'data': result}), 200


@admin_adventurer_bp.route('/adventurer-notice', methods=['GET'])
@login_required
@admin_required
def admin_get_adventurer_notice():
    from models.system_config import SystemConfig
    config = db_manager.system_config.find_one(config_key='adventurer_tavern_notice')
    notice = config.config_value if config else ''
    return jsonify({'code': 200, 'message': 'success', 'data': {'notice': notice}}), 200


@admin_adventurer_bp.route('/adventurer-notice', methods=['POST'])
@login_required
@admin_required
def admin_update_adventurer_notice():
    from models.system_config import SystemConfig
    data = request.get_json() or {}
    notice = data.get('notice', '')

    config = db_manager.system_config.find_one(config_key='adventurer_tavern_notice')
    if not config:
        config = SystemConfig(
            config_key='adventurer_tavern_notice',
            config_value=notice,
            description='深渊酒馆公告栏内容'
        )
        db_manager.add(config)
    else:
        config.config_value = notice

    ok, err = db_manager.commit()
    if not ok:
        return jsonify({'code': 500, 'message': err}), 500
    return jsonify({'code': 200, 'message': '公告已更新'}), 200


# ── 冒险者管理 ────────────────────────────────────────────────────────────────

@admin_adventurer_bp.route('/adventurer/max-limit', methods=['GET'])
@login_required
@admin_required
def admin_get_adventurer_max_limit():
    from plugins.adventurer.services.adventurer_service import get_max_adventurers
    max_limit = get_max_adventurers()
    return jsonify({'code': 200, 'message': 'success', 'data': {'max_adventurers': max_limit}}), 200


@admin_adventurer_bp.route('/adventurer/max-limit', methods=['POST'])
@login_required
@admin_required
def admin_set_adventurer_max_limit():
    from plugins.adventurer.services.adventurer_service import set_max_adventurers
    data = request.get_json() or {}
    value = data.get('value')
    if value is None:
        return jsonify({'code': 400, 'message': '请提供 value', 'data': None}), 400
    result, error = set_max_adventurers(value)
    if error:
        return jsonify({'code': 400, 'message': error, 'data': None}), 400
    return jsonify({'code': 200, 'message': f'已设置最大冒险者数量为{result["max_adventurers"]}', 'data': result}), 200


@admin_adventurer_bp.route('/adventurers', methods=['GET'])
@login_required
@admin_required
def admin_list_adventurers():
    from utils.tavern_cache import get_cached_trait_dict
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 24, type=int)
    status_filter = request.args.get('status')
    show_dead = request.args.get('show_dead', 'false') == 'true'
    search = request.args.get('search', '').strip()
    trait_dict = get_cached_trait_dict()

    adventurers, total = db_manager.adventurer.list_paginated(
        page=page, per_page=per_page,
        status_filter=status_filter, show_dead=show_dead, search=search
    )

    return jsonify({
        'code': 200,
        'message': 'success',
        'data': {
            'items': [adv.to_dict(is_admin=True, trait_dict=trait_dict) for adv in adventurers],
            'total': total,
            'page': page,
            'pages': (total + per_page - 1) // per_page
        }
    }), 200


@admin_adventurer_bp.route('/adventurers/<int:adv_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_adventurer(adv_id):
    from models.adventurer import split_adventurer_traits
    data = request.get_json() or {}

    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id)
            if not adv:
                return jsonify({'code': 404, 'message': '冒险者不存在'}), 404

            if 'skills' in data:
                skills = data.get('skills', [])
                if not isinstance(skills, list):
                    return jsonify({'code': 400, 'message': '技能必须是一个列表'}), 400
                adv.skills = skills

            if 'traits' in data:
                traits_dict = data.get('traits', {})
                if not isinstance(traits_dict, dict):
                    return jsonify({'code': 400, 'message': '特质必须是一个对象'}), 400

                new_traits = []
                for trait_name, trait_desc in traits_dict.items():
                    new_traits.append({"name": trait_name, "desc": trait_desc})

                _visible_traits, archived_traits = split_adventurer_traits(adv.traits or [])
                adv.traits = new_traits + archived_traits

            if 'history_story' in data:
                adv.history_story = data.get('history_story', '')

            if 'dm_instruction' in data:
                adv.dm_instruction = data.get('dm_instruction', '')

            if 'personality' in data:
                adv.personality = data.get('personality', '')

            msg = f"已更新冒险者【{adv.name}】的信息"

        return jsonify({'code': 200, 'message': msg}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer/create', methods=['POST'])
@login_required
@admin_required
def admin_create_adventurer():
    from plugins.adventurer.models import Adventurer
    from plugins.adventurer.services.adventurer_service import CLASSES, PERSONALITIES
    import random

    data = request.get_json() or {}
    name = data.get('name', '').strip()
    char_class = data.get('character_class')
    personality = data.get('personality')
    gender = data.get('gender', '男')
    target_user_id = data.get('user_id')

    if not name:
        return jsonify({'code': 400, 'message': '请输入冒险者名字'}), 400
    if len(name) < 2:
        return jsonify({'code': 400, 'message': '冒险者名字至少需要2个字符'}), 400
    if len(name) > 20:
        return jsonify({'code': 400, 'message': '冒险者名字最多20个字符'}), 400
    if not char_class or not personality:
        return jsonify({'code': 400, 'message': '请选择职业和性格'}), 400
    if char_class not in CLASSES:
        return jsonify({'code': 400, 'message': '无效的职业'}), 400

    personalities = [p.strip() for p in personality.split(',') if p.strip()]
    if not personalities:
        return jsonify({'code': 400, 'message': '无效的性格'}), 400
    for p in personalities:
        if p not in PERSONALITIES:
            return jsonify({'code': 400, 'message': f'无效的性格：{p}'}), 400

    try:
        with db_manager.transaction():
            hp_val = random.randint(80, 150)
            adv = Adventurer(
                name=name,
                gender=gender,
                character_class=char_class,
                level=1,
                exp=0,
                hp=hp_val,
                max_hp=hp_val,
                strength=random.randint(5, 20),
                agility=random.randint(5, 20),
                intelligence=random.randint(5, 20),
                luck=random.randint(1, 10),
                personality=personality,
                equipment={"铁剑": {"type": "weapon", "desc": "普通的铁制长剑"}, "布甲": {"type": "armor", "desc": "基础的布制防具"}},
                gold=random.uniform(50000, 200000),
                owner_id=target_user_id if target_user_id else None,
                party_preference='auto'
            )
            db_manager.add(adv)

        _emit_update(reason='adventurer_generated')
        return jsonify({'code': 200, 'message': f'管理员强制创建成功！冒险者{name}已降临深渊酒馆！'}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'创建失败：{str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer/create-force', methods=['POST'])
@login_required
@admin_required
def admin_create_adventurer_force():
    """强制创建冒险者（与 /adventurer/create 逻辑相同，前端兼容路由）"""
    return admin_create_adventurer()


@admin_adventurer_bp.route('/adventurer/logs/<int:log_id>/retry-ai', methods=['POST'])
@login_required
@admin_required
def admin_retry_ai_expedition(log_id):
    """管理员手动重试卡住的 AI 探险生成"""
    from services.async_task_service import AsyncTaskService

    try:
        log = db_manager.expedition_log.find_one(id=log_id)
        if not log:
            return jsonify({'code': 404, 'message': '日志不存在'}), 404

        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        if full_log.get('status') != 'generating':
            return jsonify({'code': 400, 'message': f'该日志状态为 {full_log.get("status", "未知")}，仅 generating 状态可重试'}), 400

        submitted = AsyncTaskService.submit_task('ai_expedition', {
            'party_id': log.party_id,
            'adventurer_shopping_logs': None,
            'departure_msg': None,
        })

        if not submitted:
            return jsonify({'code': 409, 'message': '该探险的 AI 任务正在运行中，请稍后再试'}), 409

        new_full_log = dict(full_log)
        retry_count = new_full_log.get('auto_retry_count', 0)
        new_full_log['auto_retry_count'] = retry_count + 1
        new_full_log['auto_retried_at'] = to_utc_iso(_portable_utc_now())
        log.full_log = new_full_log
        ok, err = db_manager.commit()
        if not ok:
            return jsonify({'code': 500, 'message': f'更新日志失败: {err}'}), 500

        return jsonify({'code': 200, 'message': '已重新提交 AI 生成任务，请稍后查看结果'}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'重试失败: {str(e)}'}), 500


# ── 冒险者绑定/解绑 ──────────────────────────────────────────────────────────

@admin_adventurer_bp.route('/adventurer/<int:adv_id>/bind', methods=['POST'])
@login_required
@admin_required
def admin_bind_adventurer(adv_id):
    from models.user import User
    data = request.get_json() or {}
    user_identifier = data.get('user_identifier', '').strip()

    if not user_identifier:
        return jsonify({'code': 400, 'message': '请提供用户ID或用户名'}), 400

    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv:
                return jsonify({'code': 404, 'message': '冒险者不存在'}), 404

            user = None
            try:
                user_id = int(user_identifier)
                user = db_manager.user.get_by_id(user_id)
            except ValueError:
                pass

            if not user:
                user = db_manager.user.get_by_username(user_identifier)

            if not user:
                return jsonify({'code': 404, 'message': '用户不存在'}), 404

            existing_alive = db_manager.adventurer.find_one_by_owner(user.id, status_ne='dead')
            if existing_alive and existing_alive.id != adv_id:
                return jsonify({'code': 400, 'message': f'该用户已有存活的冒险者（{existing_alive.name}），无法同时绑定多个'}), 400

            if adv.owner_id and adv.owner_id != user.id:
                existing_owner = db_manager.user.get_by_id(adv.owner_id)
                owner_name = existing_owner.username if existing_owner else '未知用户'
                return jsonify({'code': 400, 'message': f'该冒险者已被用户【{owner_name}】绑定'}), 400

            adv.owner_id = user.id
            msg = f'已将冒险者【{adv.name}】绑定到用户【{user.username}】'

        return jsonify({'code': 200, 'message': msg}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer/<int:adv_id>/unbind', methods=['POST'])
@login_required
@admin_required
def admin_unbind_adventurer(adv_id):
    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv:
                return jsonify({'code': 404, 'message': '冒险者不存在'}), 404

            owner_name = '未绑定'
            if adv.owner_id:
                owner = db_manager.user.get_by_id(adv.owner_id)
                owner_name = owner.username if owner else '未知用户'

            adv.owner_id = None
            msg = f'已解除冒险者【{adv.name}】与用户【{owner_name}】的绑定'

        return jsonify({'code': 200, 'message': msg}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer/<int:adv_id>/party-preference', methods=['POST'])
@login_required
@admin_required
def admin_set_party_preference(adv_id):
    data = request.get_json() or {}
    preference = data.get('preference', 'auto')

    valid_preferences = ['auto', 'idle', 'rest']
    if preference not in valid_preferences:
        return jsonify({'code': 400, 'message': f'无效的偏好设置，可选值：{valid_preferences}'}), 400

    try:
        with db_manager.transaction():
            adv = db_manager.adventurer.find_one(id=adv_id, for_update=True)
            if not adv:
                return jsonify({'code': 404, 'message': '冒险者不存在'}), 404

            adv.party_preference = preference
            preference_names = {'auto': '自动', 'idle': '空闲', 'rest': '休息'}
            msg = f'已将冒险者【{adv.name}】的队伍偏好设置为【{preference_names[preference]}】'

        return jsonify({'code': 200, 'message': msg}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer/auto-bind-by-name', methods=['POST'])
@login_required
@admin_required
def admin_auto_bind_adventurers():
    from models.user import User
    from collections import defaultdict

    try:
        with db_manager.transaction():
            unbound_advs = db_manager.adventurer.find_all(owner_id=None)

            if not unbound_advs:
                return jsonify({'code': 200, 'message': '没有需要绑定的冒险者', 'data': {'bound_count': 0, 'skipped_count': 0}}), 200

            nickname_to_advs = defaultdict(list)
            for adv in unbound_advs:
                nickname_to_advs[adv.name].append(adv)

            all_users = db_manager.user.find_all()
            nickname_to_users = defaultdict(list)
            for user in all_users:
                nickname = user.nickname or user.username
                nickname_to_users[nickname].append(user)

            bound_count = 0
            skipped_count = 0
            details = []

            for nickname, advs in nickname_to_advs.items():
                users_with_nickname = nickname_to_users.get(nickname, [])

                if len(users_with_nickname) == 1 and len(advs) == 1:
                    adv = advs[0]
                    user = users_with_nickname[0]

                    existing_alive = db_manager.adventurer.find_one_by_owner(user.id, status_ne='dead')
                    if existing_alive:
                        skipped_count += 1
                        details.append(f'跳过【{adv.name}】：用户【{user.username}】已有存活冒险者【{existing_alive.name}】')
                        continue

                    adv.owner_id = user.id
                    bound_count += 1
                    details.append(f'已绑定：冒险者【{adv.name}】→ 用户【{user.username}】')
                elif len(users_with_nickname) == 0:
                    skipped_count += len(advs)
                    details.append(f'跳过：没有找到名为【{nickname}】的玩家（{len(advs)}个冒险者）')
                elif len(users_with_nickname) > 1:
                    skipped_count += len(advs)
                    details.append(f'跳过：【{nickname}】有{len(users_with_nickname)}个同名玩家，无法确定绑定目标（{len(advs)}个冒险者）')
                elif len(advs) > 1:
                    skipped_count += len(advs)
                    details.append(f'跳过：【{nickname}】有{len(advs)}个同名冒险者，无法确定绑定目标')

        return jsonify({
            'code': 200,
            'message': f'批量绑定完成：成功绑定 {bound_count} 个，跳过 {skipped_count} 个',
            'data': {
                'bound_count': bound_count,
                'skipped_count': skipped_count,
                'details': details[:50]
            }
        }), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败: {str(e)}'}), 500


# ── 冒险者商店管理 ──────────────────────────────────────────────────────────

@admin_adventurer_bp.route('/adventurer-shop/items', methods=['GET'])
@login_required
@admin_required
def admin_get_adv_shop_items():
    from sqlalchemy.orm import joinedload
    from sqlalchemy import or_
    from models.adventurer import AdventurerItem, AdventurerShopItem

    filter_type = request.args.get('filter', 'all')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search_q = request.args.get('q', '').strip()

    query = db_manager.query(AdventurerItem).options(joinedload(AdventurerItem.shop_item))

    if filter_type == 'shop':
        query = query.join(AdventurerItem.shop_item).filter(AdventurerShopItem.stock > 0)

    if search_q:
        query = query.filter(
            or_(
                AdventurerItem.name.ilike(f"%{search_q}%"),
                AdventurerItem.description.ilike(f"%{search_q}%")
            )
        )

    total = query.count()

    query = query.order_by(AdventurerItem.id.desc())
    if per_page > 0:
        query = query.offset((page - 1) * per_page).limit(per_page)

    items = query.all()

    result = []
    for item in items:
        shop_info = item.shop_item
        stock = shop_info.stock if shop_info else 0
        price = shop_info.price if shop_info else 0

        result.append({
            "id": item.id,
            "name": item.name,
            "type": item.type,
            "description": item.description,
            "rarity": item.rarity,
            "price": price,
            "stock": stock
        })

    return jsonify({
        'code': 200,
        'message': 'success',
        'data': {
            'items': result,
            'total': total,
            'page': page,
            'pages': (total + per_page - 1) // per_page if per_page > 0 else 1
        }
    })


@admin_adventurer_bp.route('/adventurer-shop/items', methods=['POST'])
@login_required
@admin_required
def admin_add_adv_shop_item():
    from models.adventurer import AdventurerItem, AdventurerShopItem
    from utils.tavern_cache import invalidate_item_cache

    data = request.get_json() or {}
    name = data.get('name', '').strip()
    price = int(data.get('price', 0))
    stock = int(data.get('stock', 0))

    if not name:
        return jsonify({'code': 400, 'message': '物品名称不能为空'}), 400

    try:
        with db_manager.transaction():
            existing = db_manager.adventurer_item.find_one(name=name)
            if existing:
                if existing.shop_item:
                    existing.shop_item.price = price
                    existing.shop_item.stock = stock
                else:
                    new_shop_item = AdventurerShopItem(item_id=existing.id, price=price, stock=stock)
                    db_manager.add(new_shop_item)

                msg = f"已更新现有物品【{name}】的库存和价格"
                item_dict = {
                    "id": existing.id,
                    "name": existing.name,
                    "type": existing.type,
                    "description": existing.description,
                    "rarity": existing.rarity,
                    "price": existing.shop_item.price if existing.shop_item else price,
                    "stock": existing.shop_item.stock if existing.shop_item else stock
                }
            else:
                item_type = data.get('type', 'consumable')
                description = data.get('description', '')
                rarity = data.get('rarity', 'common')

                new_item = AdventurerItem(
                    name=name,
                    type=item_type,
                    description=description,
                    rarity=rarity
                )
                db_manager.add(new_item)
                db_manager.flush()

                new_shop_item = AdventurerShopItem(item_id=new_item.id, price=price, stock=stock)
                db_manager.add(new_shop_item)

                msg = f"已新增物品【{name}】"
                item_dict = {
                    "id": new_item.id,
                    "name": new_item.name,
                    "type": new_item.type,
                    "description": new_item.description,
                    "rarity": new_item.rarity,
                    "price": price,
                    "stock": stock
                }

        invalidate_item_cache()
        return jsonify({'code': 200, 'message': msg, 'data': item_dict}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'操作失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer-shop/items/<int:item_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_adv_shop_item(item_id):
    from models.adventurer import AdventurerShopItem
    from plugins.adventurer.services.adventurer_service import sync_adventurer_item_snapshots
    from utils.tavern_cache import invalidate_item_cache

    data = request.get_json() or {}
    try:
        with db_manager.transaction():
            item = db_manager.adventurer_item.find_one(id=item_id)
            if not item:
                return jsonify({'code': 404, 'message': '物品不存在'}), 404

            old_name = item.name
            if 'name' in data and data['name'].strip():
                item.name = data['name'].strip()
            if 'type' in data:
                item.type = data['type']
            if 'description' in data:
                item.description = data['description']
            if 'rarity' in data:
                item.rarity = data['rarity']

            if 'price' in data or 'stock' in data:
                price = int(data.get('price', item.shop_item.price if item.shop_item else 0))
                stock = int(data.get('stock', item.shop_item.stock if item.shop_item else 0))

                if item.shop_item:
                    item.shop_item.price = price
                    item.shop_item.stock = stock
                else:
                    new_shop_item = AdventurerShopItem(item_id=item.id, price=price, stock=stock)
                    db_manager.add(new_shop_item)

            sync_result, sync_error = sync_adventurer_item_snapshots(item, old_name=old_name)
            if sync_error:
                raise ValueError(sync_error)

            invalidate_item_cache()

            updated_adventurers = sync_result.get('updated_adventurers', 0) if sync_result else 0
            msg = f"更新成功，已同步 {updated_adventurers} 名冒险者的物品快照"
            item_dict = {
                "id": item.id,
                "name": item.name,
                "type": item.type,
                "description": item.description,
                "rarity": item.rarity,
                "price": item.shop_item.price if item.shop_item else 0,
                "stock": item.shop_item.stock if item.shop_item else 0
            }
        return jsonify({'code': 200, 'message': msg, 'data': item_dict}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'更新失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer-shop/items/<int:item_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_adv_shop_item(item_id):
    from utils.tavern_cache import invalidate_item_cache
    try:
        with db_manager.transaction():
            item = db_manager.adventurer_item.find_one(id=item_id)
            if not item:
                return jsonify({'code': 404, 'message': '物品不存在'}), 404

            db_manager.delete_instance(item)
        invalidate_item_cache()
        return jsonify({'code': 200, 'message': '删除成功'}), 200
    except Exception as e:
        return jsonify({'code': 500, 'message': f'删除失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer-shop/items/auto-add-missing', methods=['POST'])
@login_required
@admin_required
def admin_auto_add_missing_items():
    from models.adventurer import AdventurerItem, AdventurerShopItem
    try:
        adventurers = db_manager.adventurer.find_all()
        existing_items = {item.name for item in db_manager.adventurer_item.find_all()}

        added_count = 0

        for adv in adventurers:
            if adv.equipment:
                for eq_name, eq_data in adv.equipment.items():
                    if eq_name not in existing_items:
                        rarity = eq_data.get('rarity', 'common')
                        price_map = {"common": 50000, "rare": 200000, "epic": 1000000, "legendary": 5000000}
                        new_item = AdventurerItem(
                            name=eq_name,
                            type=eq_data.get('type', 'consumable'),
                            description=eq_data.get('desc', ''),
                            rarity=rarity
                        )
                        db_manager.add(new_item)
                        db_manager.flush()
                        new_shop_item = AdventurerShopItem(
                            item_id=new_item.id,
                            price=price_map.get(rarity, 50000),
                            stock=0
                        )
                        db_manager.add(new_shop_item)
                        existing_items.add(eq_name)
                        added_count += 1

            if adv.warehouse:
                for w_item in adv.warehouse:
                    w_name = w_item.get('name')
                    if w_name and w_name not in existing_items:
                        rarity = w_item.get('rarity', 'common')
                        price_map = {"common": 50000, "rare": 200000, "epic": 1000000, "legendary": 5000000}
                        new_item = AdventurerItem(
                            name=w_name,
                            type=w_item.get('type', 'consumable'),
                            description=w_item.get('desc', ''),
                            rarity=rarity
                        )
                        db_manager.add(new_item)
                        db_manager.flush()
                        new_shop_item = AdventurerShopItem(
                            item_id=new_item.id,
                            price=price_map.get(rarity, 50000),
                            stock=0
                        )
                        db_manager.add(new_shop_item)
                        existing_items.add(w_name)
                        added_count += 1

        ok, err = db_manager.commit()
        if not ok:
            return jsonify({'code': 500, 'message': f'操作失败: {err}'}), 500
        from utils.tavern_cache import invalidate_item_cache
        invalidate_item_cache()

        return jsonify({'code': 200, 'message': f'扫描补缺完成，共自动补录 {added_count} 个物品'}), 200
    except Exception as e:
        db_manager.rollback()
        return jsonify({'code': 500, 'message': f'操作失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer-shop/items/auto-cleanup', methods=['POST'])
@login_required
@admin_required
def admin_auto_cleanup_items():
    from models.adventurer import AdventurerItem
    from plugins.adventurer.services.adventurer_service import BASIC_CONSUMABLES, INITIAL_EQUIPMENT_SEED
    try:
        adventurers = db_manager.adventurer.find_all()
        in_use_names = set()

        for adv in adventurers:
            if adv.equipment:
                in_use_names.update(adv.equipment.keys())
            if adv.warehouse:
                for w_item in adv.warehouse:
                    if w_item.get('name'):
                        in_use_names.add(w_item.get('name'))

        whitelist_names = {item['name'] for item in BASIC_CONSUMABLES}
        whitelist_names.update(item['name'] for item in INITIAL_EQUIPMENT_SEED)

        all_items = db_manager.adventurer_item.find_all()
        deleted_count = 0

        for item in all_items:
            if item.name not in in_use_names and item.name not in whitelist_names:
                stock = item.shop_item.stock if item.shop_item else 0
                if stock <= 0:
                    db_manager.delete_instance(item)
                    deleted_count += 1

        ok, err = db_manager.commit()
        if not ok:
            return jsonify({'code': 500, 'message': f'操作失败: {err}'}), 500
        from utils.tavern_cache import invalidate_item_cache
        invalidate_item_cache()

        return jsonify({'code': 200, 'message': f'自动清理完成，共删除了 {deleted_count} 个孤儿物品记录'}), 200
    except Exception as e:
        db_manager.rollback()
        return jsonify({'code': 500, 'message': f'操作失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer-shop/items/<int:item_id>/send', methods=['POST'])
@login_required
@admin_required
def admin_send_adv_shop_item(item_id):
    from plugins.adventurer.services.adventurer_service import resolve_delivery_target

    data = request.get_json() or {}
    target_id_str = str(data.get('target_id', '')).strip().lower()

    if not target_id_str:
        return jsonify({'code': 400, 'message': '请提供玩家ID或冒险者ID'}), 400

    try:
        with db_manager.transaction():
            item = db_manager.adventurer_item.find_one(id=item_id)
            if not item:
                return jsonify({'code': 404, 'message': '物品不存在'}), 404

            target_result, target_error = resolve_delivery_target(target_id_str, lock_for_update=True)
            if target_error:
                if '找不到对应' in target_error:
                    return jsonify({'code': 404, 'message': target_error}), 404
                return jsonify({'code': 400, 'message': target_error}), 400
            adv = target_result['adventurer']

            import copy
            current_equipment = copy.deepcopy(adv.equipment) if adv.equipment else {}

            eq_name = item.name
            if eq_name in current_equipment:
                if 'uses_left' in current_equipment[eq_name]:
                    current_equipment[eq_name]['uses_left'] += 1
            else:
                current_equipment[eq_name] = {
                    "type": item.type,
                    "desc": item.description,
                    "rarity": item.rarity
                }
                if item.type == 'consumable':
                    current_equipment[eq_name]['uses_left'] = 1

            adv.equipment = current_equipment
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(adv, 'equipment')

        return jsonify({'code': 200, 'message': f'已将【{item.name}】发送给冒险者【{adv.name}】'}), 200
    except ValueError:
        return jsonify({'code': 400, 'message': 'ID必须是数字，可加前缀u(玩家)或a(冒险者)'}), 400
    except Exception as e:
        return jsonify({'code': 500, 'message': f'发送失败: {str(e)}'}), 500


@admin_adventurer_bp.route('/adventurer-shop/resolve-target', methods=['GET'])
@login_required
@admin_required
def admin_resolve_adv_delivery_target():
    from plugins.adventurer.services.adventurer_service import resolve_delivery_target

    target_id_str = request.args.get('target_id', '').strip().lower()
    if not target_id_str:
        return jsonify({'code': 400, 'message': '请提供玩家ID或冒险者ID'}), 400

    result, error = resolve_delivery_target(target_id_str, lock_for_update=False)
    if error:
        status_code = 404 if '找不到对应' in error else 400
        return jsonify({'code': status_code, 'message': error}), status_code

    return jsonify({
        'code': 200,
        'message': '解析成功',
        'data': {
            'adventurer_id': result['adventurer_id'],
            'adventurer_name': result['adventurer_name'],
            'owner_id': result['owner_id'],
            'status': result['status'],
            'resolved_by': result['resolved_by']
        }
    }), 200
