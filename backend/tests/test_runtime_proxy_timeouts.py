from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.runtime_supervisor_service import RuntimeSupervisorService as Supervisor
from services.game_pack_registry import GamePackRegistry


@pytest.mark.parametrize('path,seconds', [
    ('init', 35), ('logs', 35), ('admin/world-outline', 125),
    ('admin/export/all', 605), ('ambient-npcs/71/actions/talk', 65),
])
def test_proxy_uses_server_owned_budget_and_forwards_exactly_once(app, monkeypatch, path, seconds):
    tavern = SimpleNamespace(game_pack='s1_fantasy', game_pack_release=GamePackRegistry.get('s1_fantasy').pack_version,
                             runtime_namespace='timeout-test', runtime_port=12345)
    response = Mock(status=200, headers={'Content-Type': 'application/json'})
    response.read.return_value = b'{"result":"done"}'
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    transport = Mock(return_value=context)
    monkeypatch.setattr('services.runtime_supervisor_service.urlopen', transport)
    with app.app_context():
        result = Supervisor.proxy_request(tavern, runtime_path=path, query_string=b'timeout=99999',
            method='POST', body=b'{}', headers={'Authorization': 'Bearer example', 'X-Timeout': '99999'})
    assert result[0:2] == (b'{"result":"done"}', 200)
    assert result[2]['Cache-Control'] == 'private, no-store'
    assert transport.call_count == 1
    assert transport.call_args.kwargs['timeout'] == seconds
    request = transport.call_args.args[0]
    assert request.data == b'{}'
    assert not request.has_header('X-timeout')


def test_ambiguous_timeout_never_replays_write(app, monkeypatch):
    tavern = SimpleNamespace(game_pack='s1_fantasy', game_pack_release=GamePackRegistry.get('s1_fantasy').pack_version,
                             runtime_namespace='timeout-test', runtime_port=12345)
    transport = Mock(side_effect=TimeoutError('response timed out'))
    monkeypatch.setattr('services.runtime_supervisor_service.urlopen', transport)
    with app.app_context(), pytest.raises(TimeoutError):
        Supervisor.proxy_request(tavern, runtime_path='admin/repair', query_string=b'',
            method='POST', body=b'{}', headers={})
    assert transport.call_count == 1
