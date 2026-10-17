import json
import os
import sys
from pathlib import Path

import pytest


HOST_ROOT = Path(__file__).resolve().parents[1]
S1_ADAPTER_PATH = (
    HOST_ROOT.parent
    / "game-packs"
    / "s1-fantasy"
    / "runtime_adapter"
    / "s1_adapter.py"
)
S1_SNAPSHOT_BACKEND = (
    HOST_ROOT.parent
    / "game-packs"
    / "s1-fantasy"
    / "source-snapshot"
    / "backend"
)
os.environ.setdefault(
    "GAME_RUNTIME_IMPORT_PATHS_JSON",
    json.dumps([str(S1_SNAPSHOT_BACKEND)]),
)
os.environ.setdefault("GAME_RUNTIME_PACK_ID", "s1_fantasy")
os.environ.setdefault(
    "GAME_RUNTIME_PACK_RELEASE",
    "0.0.2-runtime-preview",
)
os.environ.setdefault("GAME_RUNTIME_API_VERSION", "0.1")
if str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

os.environ["GAME_RUNTIME_SECRET_KEY"] = "runtime-flask-test-private-secret-32-bytes"
os.environ["JWT_SECRET_KEY"] = "runtime-jwt-test-private-secret-32-bytes"

from app import create_runtime_app


@pytest.fixture
def runtime_app():
    return create_runtime_app(
        database_uri="sqlite:///:memory:",
        runtime_id="test-runtime",
        bootstrap_secret="test-bootstrap-secret-private-32-bytes",
        seed_on_start=False,
        adapter_path=str(S1_ADAPTER_PATH),
    )


@pytest.fixture
def runtime_client(runtime_app):
    return runtime_app.test_client()
