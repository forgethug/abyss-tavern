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

    return blueprint
