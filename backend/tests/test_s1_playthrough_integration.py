"""End-to-end S1 gameplay with HTTP model fixtures, never a paid provider.

Fixtures deliberately omit history entries; the actual Observer repairs them.
They test contracts and persistence, not the quality of a real model's judgment.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import threading
import time

import pytest

from app import create_app
from config import TestConfig
from db_repository import db_manager
from models import Tavern
from services.game_pack_registry import GamePackRegistry
from services.runtime_supervisor_service import RuntimeSupervisorService

FIXTURES = Path(__file__).parent / 'fixtures/s1_playthrough'


def free_ports(count):
    for first in range(45200, 61000 - count):
        probes = []
        try:
            for offset in range(count):
                sock = socket.socket()
                probes.append(sock)
                sock.bind(('127.0.0.1', first + offset))
            return first
        except OSError:
            pass
        finally:
            for sock in probes:
                sock.close()
    raise RuntimeError('No free port range')


class ScriptedProvider:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []
        self.errors = []

    def response(self, payload):
        feature = payload['messages'][0]['content'].split('当前 feature：', 1)[1].split('；', 1)[0]
        self.calls.append(feature)
        if self.mode == 'unauthorized':
            return 401, {'error': {'type': 'authentication_error', 'message': 'Invalid test API key'}}
        if self.mode == 'malformed':
            content = '{invalid-json'
        elif feature == 'expedition_outline':
            content = (FIXTURES / 'expedition_outline.json').read_text(encoding='utf-8')
        elif feature == 'expedition_main':
            members = [json.loads(line) for line in payload['messages'][-1]['content'].splitlines() if line.startswith('{"id":')]
            assert len(members) == 4
            result = json.loads((FIXTURES / 'expedition_main.json').read_text(encoding='utf-8'))
            for member, update, gain_loss in zip(members, result['member_updates'], result['completion_check']['member_gain_loss'], strict=True):
                update['id'] = gain_loss['id'] = member['id']
                update['remaining_hp'] = member['hp']
            content = json.dumps(result, ensure_ascii=False)
        elif feature == 'expedition_observer':
            request = json.loads(payload['messages'][-1]['content'])
            names = request['task_payload']['member_id_to_name']
            updates = [{'id': int(member_id), 'history_story_append': f'{name}在哥布林矿坑与同伴击退守卫，安全回到酒馆，分得二十枚金币。'} for member_id, name in names.items()]
            result = {'needs_regeneration': False, 'override_fields': {'member_updates': updates}, 'reason': '模拟审核回执：补齐履历，保留既有奖励及归属。'}
            content = json.dumps(result, ensure_ascii=False)
        else:
            raise AssertionError(f'Unexpected model capability: {feature}')
        return 200, {'id': f'fixture-{len(self.calls)}', 'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}

    def start(self, port):
        provider = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    status, result = provider.response(payload)
                except Exception as exc:
                    provider.errors.append(str(exc))
                    status, result = 400, {'error': str(exc)}
                body = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.integration
@pytest.mark.parametrize('mode', ['success', 'unauthorized', 'malformed'])
def test_s1_scripted_gameplay_persistence_and_failure_recovery(tmp_path, monkeypatch, mode):
    first = free_ports(3)
    provider = ScriptedProvider(mode)
    provider.start(first + 2)
    original_require = GamePackRegistry.require_release

    def scenario_pack(*args, **kwargs):
        pack = original_require(*args, **kwargs)
        return replace(pack, runtime_entrypoint=Path(__file__).parent / 'support/s1_scenario_runtime.py')

    monkeypatch.setattr(GamePackRegistry, 'require_release', scenario_pack)
    # Even the test subprocess cannot inherit usable provider credentials.
    for key in ('DEEPSEEK_API_KEY', 'MIMO_API_KEY'):
        monkeypatch.setenv(key, '')

    class ScenarioConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = 'sqlite:///' + (tmp_path / 'control.db').as_posix()
        SECRET_KEY = secrets.token_urlsafe(40)
        JWT_SECRET_KEY = secrets.token_urlsafe(40)
        RUNTIME_BOOTSTRAP_SECRET = secrets.token_urlsafe(40)
        RUNTIME_DATA_DIR = str(tmp_path / 'runtimes')
        RUNTIME_DATABASE_URL_TEMPLATE = ''
        RUNTIME_BASE_PORT = first - 1
        RUNTIME_AUTO_START = True
        RUNTIME_SCHEDULER_ENABLED = False
        RUNTIME_START_TIMEOUT_SECONDS = 90
        RUNTIME_MODEL_PROFILES_JSON = json.dumps([{'id': 'default', 'provider': 'openai_compatible', 'model': 'scripted-test-fixture', 'api_key': 'test-only', 'api_base': f'http://127.0.0.1:{first + 2}/v1'}])
        RUNTIME_MODEL_ASSIGNMENTS_JSON = '{}'
        RUNTIME_MODEL_REASONING_JSON = '{}'

    app = create_app(ScenarioConfig)
    client = app.test_client()
    auth = {}

    def call(method, path, data=None, actor='admin', expected=200):
        response = client.open(path, method=method, json=data, headers=auth.get(actor, {}))
        assert response.status_code == expected, (path, response.json)
        return response.json['data']

    def game(method, path, data=None, actor='admin', expected=200, slug='party'):
        return call(method, f'/api/taverns/{slug}/runtime/{path}', data, actor, expected)

    def db_rows(namespace, query):
        with sqlite3.connect(tmp_path / 'runtimes' / f'{namespace}.db') as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query)]

    try:
        password = secrets.token_urlsafe(20)
        cli = app.test_cli_runner().invoke(args=['bootstrap-admin', '--username', 'scenario_admin', '--password', password])
        assert cli.exit_code == 0, cli.output
        login = call('POST', '/api/auth/login', {'username': 'scenario_admin', 'password': password})
        auth['admin'] = {'Authorization': 'Bearer ' + login['token']}
        for i in range(4):
            actor = f'player{i}'
            registration = call('POST', '/api/auth/register', {'username': actor, 'password': secrets.token_urlsafe(20)}, actor=actor, expected=201)
            auth[actor] = {'Authorization': 'Bearer ' + registration['token']}
        taverns = [call('POST', '/api/admin/taverns', {'slug': slug, 'display_name': slug, 'is_public': True, 'allow_self_join': True}, expected=201) for slug in ('party', 'control')]
        namespace = taverns[0]['runtime_namespace']
        for slug in ('party', 'control'):
            call('GET', f'/api/taverns/{slug}/context')
        for i in range(4):
            call('POST', '/api/taverns/party/join', {}, f'player{i}')
        game('POST', 'admin/expedition/config', {'hidden_dungeon_enabled': False})
        for i, (name, skill, items) in enumerate([('星澜', 2, [1, 6]), ('林弦', 1, [2, 6]), ('白芷', 3, [5, 6]), ('烛川', 4, [5, 6])]):
            game('POST', 'reserve', {'name': name, 'gender': '女', 'personality': '勇毅', 'skill_template_id': skill, 'item_ids': items}, f'player{i}', 201)
        drawn = game('POST', 'admin/draw', {'count': 4})
        assert drawn['drawn_count'] == 4
        before = {row['id']: row for row in drawn['created']}
        ticket = game('POST', 'matchmaking/create-party', {'preferred_dungeon': '哥布林矿坑', 'access_mode': 'public', 'allow_solo_fill': False}, 'player0')['ticket']['id']
        for i in range(1, 4):
            game('POST', f'matchmaking/{ticket}/join', {}, f'player{i}')
        game('POST', f'matchmaking/{ticket}/start', {}, 'player0')
        party = game('POST', 'admin/expedition/form-parties', {})['party_ids'][0]
        log_id = game('POST', f'admin/party/{party}/force-dispatch', {})['log_id']
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            rows = db_rows(namespace, 'SELECT status, full_log FROM adventurer_s1_expedition_logs')
            if rows and rows[0]['status'] != 'generating':
                break
            assert not provider.errors, provider.errors
            time.sleep(.2)
        assert rows[0]['status'] == ('in_progress' if mode == 'success' else 'failed'), (rows, provider.calls, provider.errors)
        assert not provider.errors
        if mode == 'success':
            assert provider.calls == ['expedition_outline', 'expedition_main', 'expedition_observer']
            # Reads and partial publication cannot grant rewards early.
            initial = game('GET', f'logs/{log_id}', actor='player0')
            for i in range(4):
                assert game('GET', 'list', actor=f'player{i}')[0]['gold'] == 0
            game('POST', f'admin/expedition/logs/{log_id}/step', {})
            partial = game('GET', f'logs/{log_id}', actor='player0')
            assert len(partial['full_log']['events']) > len(initial['full_log']['events'])
            game('POST', f'admin/expedition/logs/{log_id}/force-complete', {})
            full = json.loads(db_rows(namespace, 'SELECT full_log FROM adventurer_s1_expedition_logs')[0]['full_log'])
            assert full['settlement_receipt']['status'] == 'passed'
            assert full['settlement_receipt']['mismatches'] == []
            assert full['settlement_receipt']['member_count'] == 4
            game('POST', f'admin/expedition/logs/{log_id}/force-complete', {}, expected=400)
            visible = game('GET', f'logs/{log_id}', actor='player0')
            assert visible['status'] == 'completed'
            assert len(visible['full_log']['events']) >= 6
            assert not {'audit_member_snapshot', 'generation_retry_trace', 'objective_outline', 'settlement_receipt'}.intersection(visible['full_log'])
            serialized = json.dumps(visible, ensure_ascii=False)
            assert '【M' not in serialized and '【人物' not in serialized
            assert db_rows(namespace, 'SELECT COUNT(*) AS n FROM adventurer_s1_currency_logs')[0]['n'] == 4
        else:
            assert all(feature in ('expedition_outline', 'json_repair') for feature in provider.calls), provider.calls
            if mode == 'unauthorized':
                assert provider.calls == ['expedition_outline']
            failed = game('GET', f'logs/{log_id}', actor='player0')
            assert failed['status'] == 'failed' and failed['full_log']['events'] == []
            # Failure narration is deliberately selected at random by the game.
            assert isinstance(failed['result_summary'], str) and failed['result_summary'].strip()
            assert db_rows(namespace, 'SELECT COUNT(*) AS n FROM adventurer_s1_currency_logs')[0]['n'] == 0
            assert db_rows(namespace, 'SELECT status FROM adventurer_s1_parties')[0]['status'] == 'disbanded'

        def verify_players():
            for i in range(4):
                summary = game('GET', 'list', actor=f'player{i}')[0]
                detail = game('GET', str(summary['id']), actor=f'player{i}')
                assert detail['gold'] == (20 if mode == 'success' else 0)
                assert detail['hp'] == before[detail['id']]['hp']
                assert detail['status'] == 'resting'
                assert detail['stamina'] >= (20 if mode == 'success' else 100)
                assert detail['history_entry_count'] == (1 if mode == 'success' else 0)
                ore_count = sum(1 for item in detail['equipment'] if item['name'] == '铜矿样品')
                assert ore_count == int(mode == 'success' and i == 0)
        verify_players()
        # The same object IDs in another tavern cannot be read or operated on.
        game('GET', f'logs/{log_id}', actor='player0', expected=403, slug='control')
        game('GET', f'logs/{log_id}', expected=404, slug='control')
        game('POST', f'admin/expedition/logs/{log_id}/force-complete', {}, expected=400, slug='control')
        assert game('GET', 'list', slug='control') == []
        assert db_rows(taverns[1]['runtime_namespace'], 'SELECT COUNT(*) AS n FROM adventurer_s1_token_usages')[0]['n'] == 0
        call('POST', f'/api/admin/taverns/{taverns[0]["id"]}/runtime/restart', {})
        verify_players()
        assert game('GET', f'logs/{log_id}', actor='player1')['status'] == ('completed' if mode == 'success' else 'failed')
        if mode == 'success':
            owner = game('GET', 'list', actor='player0')[0]
            other = game('GET', 'list', actor='player1')[0]
            detail = game('GET', str(owner['id']), actor='player0')
            ore = next(item for item in detail['equipment'] if item['name'] == '铜矿样品')
            # Cross-owner writes fail before inventory mutation.
            game('POST', f'{owner["id"]}/unequip', {'item_id': ore['id']}, 'player1', 400)
            stored = game('POST', f'{owner["id"]}/unequip', {'item_id': ore['id']}, 'player0')
            assert not any(item['name'] == '铜矿样品' for item in stored['equipment'])
            warehouse_ore = next(item for item in stored['warehouse'] if item['name'] == '铜矿样品')
            game('POST', f'{owner["id"]}/equip', {'item_id': warehouse_ore['id']}, 'player0')
            verify_players()
            game('POST', f'logs/{log_id}/like', {'to_adventurer_id': other['id']}, 'player0')
            game('POST', f'logs/{log_id}/like', {'to_adventurer_id': other['id']}, 'player0', 400)
            assert game('GET', str(other['id']), actor='player1')['praise_count'] == 1
            game('PUT', f'logs/{log_id}/favorite', {}, 'player0')
            assert game('GET', f'logs/{log_id}', actor='player0')['is_favorited'] is True
            assert game('GET', f'logs/{log_id}', actor='player1')['is_favorited'] is False
            game('POST', f'logs/{log_id}/comments', {'target_type': 'event', 'target_index': 1, 'content': '这次一起顺利回来了。'}, 'player0', 201)
            comments = game('GET', f'logs/{log_id}/comments?target_type=event&target_index=1', actor='player1')
            assert comments['items'][0]['content'] == '这次一起顺利回来了。'
            assert provider.calls == ['expedition_outline', 'expedition_main', 'expedition_observer']
    finally:
        with app.app_context():
            for tavern in db_manager.query(Tavern).all():
                RuntimeSupervisorService.stop_runtime(tavern)
        provider.close()
