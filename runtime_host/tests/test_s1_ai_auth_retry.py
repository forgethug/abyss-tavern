"""Credential failures stop same-key retries without disabling route failover."""

import json

import pytest
import requests


@pytest.mark.parametrize('status', [401, 403])
@pytest.mark.parametrize('backup', [False, True])
def test_rejected_credential_is_tried_once_per_route(runtime_app, monkeypatch, status, backup):
    from plugins.adventurer_s1.services import s1_ai_runtime_config_service as service

    profile = {
        'id': 'default', 'api_base': 'https://model.test/v1',
        'model': 'fixture', 'provider': 'openai_compatible',
        'api_key': 'fixture-primary', 'stream_enabled': False,
    }
    if backup:
        profile['backup_api_key'] = 'fixture-backup'
    runtime_app.config['ADVENTURER_S1_AI_PROFILES_JSON'] = json.dumps([profile])
    runtime_app.config['ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON'] = '{}'
    calls = []

    def post(url, **kwargs):
        credential = kwargs['headers']['Authorization']
        calls.append(credential)
        response = requests.Response()
        response.status_code = 200 if credential == 'Bearer fixture-backup' else status
        response.url = url
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps(
            {'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]}
            if response.status_code == 200 else {'error': {'message': 'Invalid API key'}}
        ).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(runtime_app.extensions['model_gateway'], '_post', post)
    monkeypatch.setattr(service, '_wait_before_s1_ai_retry', lambda **kwargs: None)
    with runtime_app.app_context():
        response = service.post_s1_ai_request(
            'https://model.test/v1/chat/completions', feature='expedition_outline',
            json={'messages': [{'role': 'user', 'content': 'Test fixture'}]},
        )
    assert calls == ['Bearer fixture-primary'] + (['Bearer fixture-backup'] if backup else [])
    assert response.status_code == (200 if backup else status)
    if not backup:
        with pytest.raises(requests.HTTPError) as exc:
            response.raise_for_status()
        assert not service.is_s1_ai_retryable_request_exception(exc.value)


@pytest.mark.parametrize('status', [408, 429, 500, 503])
def test_transient_http_failures_still_allow_retries(runtime_app, status):
    from plugins.adventurer_s1.services import s1_ai_runtime_config_service as service

    response = requests.Response()
    response.status_code = status
    assert service._is_retryable_ai_http_response(response)
    assert service.is_s1_ai_retryable_request_exception(requests.HTTPError(response=response))
