"""Admin-only writes for the model catalog owned by this runtime."""
from flask import Blueprint, jsonify, request, current_app
from sqlalchemy.exc import SQLAlchemyError
from routes.auth import login_required
from utils.decorators import admin_required


def create_model_config_blueprint(prefix):
    blueprint = Blueprint('tavern_model_config', __name__, url_prefix=prefix)

    def reply(result, status_override=None):
        data, error = result
        from services.ai_runtime_config_service import STALE_AI_CONFIG_ERROR, STALE_AI_BALANCE_BLOCK_ERROR
        status = (409 if error in {STALE_AI_CONFIG_ERROR, STALE_AI_BALANCE_BLOCK_ERROR} else 400) if error else 200
        status = status_override or status
        response = jsonify({'code': status, 'message': error or 'success', 'data': data})
        response.headers['Cache-Control'] = 'private, no-store'
        return response, status

    @blueprint.put('/admin/ai/config')
    @login_required
    @admin_required
    def save():
        from services.ai_runtime_config_service import update_ai_provider_config
        try:
            return reply(update_ai_provider_config(request.get_json(silent=True), scope='s1'))
        except SQLAlchemyError as exc:
            # SQL exception strings may contain private bound parameters.
            current_app.logger.error('Model configuration storage failed: %s', type(exc).__name__)
            return reply((None, '模型配置保存失败，请稍后刷新确认后重试'), 503)

    @blueprint.post('/admin/ai/balance/resume')
    @login_required
    @admin_required
    def resume():
        from services.ai_runtime_config_service import resume_ai_credential_after_topup
        return reply(resume_ai_credential_after_topup(request.get_json(silent=True)))

    @blueprint.route('/admin/ai/input-settings', methods=['GET', 'PUT'])
    @login_required
    @admin_required
    def input_settings():
        from services.tavern_ai_input_settings import read_settings, save_settings
        try:
            if request.method == 'GET':
                return reply((read_settings(), None))
            data, error, status = save_settings(request.get_json(silent=True))
            return reply((data, error), status)
        except SQLAlchemyError as exc:
            current_app.logger.error('AI input settings storage failed: %s', type(exc).__name__)
            return reply((None, '设置保存失败，请稍后重试'), 503)

    return blueprint
