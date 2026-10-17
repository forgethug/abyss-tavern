"""Test-only runtime entry point: select ordinary encounters and forbid WAN AI.

The real app, worker, parsers, reviewers, publication and settlement remain in use.
Encounter RNG and dispatch timing are fixed; responses come from a loopback server.
"""
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'runtime_host'))
original_send = requests.Session.send


def loopback_only(self, request, **kwargs):
    if urlparse(request.url).hostname != '127.0.0.1':
        raise AssertionError('Scenario tests forbid external HTTP requests')
    return original_send(self, request, **kwargs)


requests.Session.send = loopback_only
from run import app
from plugins.adventurer_s1.services import adventurer_s1_expedition_runner
from waitress import serve

app.config['ADVENTURER_S1_AUTO_DISPATCH_ON_FULL'] = False
adventurer_s1_expedition_runner._roll_special_event = lambda _level: (False, False, None, None)
serve(app, host='127.0.0.1', port=int(os.environ['GAME_RUNTIME_PORT']), threads=8)
