"""Player-only team-loot endpoints; services own state and transactions."""
from flask import request

from routes.auth import login_required
from utils.decorators import generic_anti_cheat_check
from utils.pagination import get_pagination_params
from utils.response import error, success
from plugins.adventurer_s1.services.s1_team_loot_service import S1TeamLootService


def register_team_loot_routes(blueprint):
    @blueprint.route('/team-loot/summary', methods=['GET'])
    @login_required
    def team_loot_summary():
        return success(S1TeamLootService.pending_summary(request.current_user.id))

    @blueprint.route('/team-loot', methods=['GET'])
    @login_required
    def team_loot_list():
        page, per_page = get_pagination_params(default_per_page=20, max_per_page=100)
        log_id = request.args.get('expedition_log_id', type=int)
        return success(S1TeamLootService.list_for_user(
            request.current_user.id, page=page, per_page=per_page, log_id=log_id,
            include_history=request.args.get('history') == '1',
        ))

    @blueprint.route('/team-loot/<int:lot_id>/vote', methods=['POST'])
    @generic_anti_cheat_check('trade')
    @login_required
    def team_loot_vote(lot_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error('请选择分配方式')
        result, err = S1TeamLootService.vote(request.current_user.id, lot_id, body.get('mode'), body.get('adventurer_id'))
        return error(err) if err else success(result)

    @blueprint.route('/team-loot/<int:lot_id>/bid', methods=['POST'])
    @generic_anti_cheat_check('trade')
    @login_required
    def team_loot_bid(lot_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error('请填写出价')
        result, err = S1TeamLootService.bid(request.current_user.id, lot_id, body.get('amount'), body.get('adventurer_id'))
        return error(err) if err else success(result)

    @blueprint.route('/team-loot/<int:lot_id>/claim', methods=['POST'])
    @login_required
    def team_loot_claim(lot_id):
        result, err = S1TeamLootService.claim(request.current_user.id, lot_id)
        return error(err) if err else success(result)
