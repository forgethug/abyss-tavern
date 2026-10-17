from runtime_core.time import utc_now
import json
import os
import subprocess
import sys
import threading
import time
import psutil
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import current_app
from sqlalchemy.orm import lazyload

from db_repository import db_manager
from models import Tavern, User
from services.game_pack_registry import GamePackRegistry


@dataclass
class _ReadyRuntime:
    process: subprocess.Popen
    generation: tuple
    checked_at: float
    identities: OrderedDict = field(default_factory=OrderedDict)


class RuntimeSupervisorService:
    _processes = {}
    _locks = {}
    _locks_guard = threading.Lock()
    _SYSTEM_ENVIRONMENT_KEYS = frozenset(
        {
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "TZ",
            "VIRTUAL_ENV",
            "WINDIR",
        }
    )
    @classmethod
    def _runtime_lock(cls, runtime_namespace: str) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(runtime_namespace, threading.Lock())

    @staticmethod
    def runtime_base_url(tavern: Tavern) -> str:
        return f"http://{current_app.config['RUNTIME_HOST']}:{tavern.runtime_port}"

    @staticmethod
    def _readiness_cache() -> dict:
        # An application must never reuse another application's database or
        # credentials, even if both happen to contain the same namespace.
        return current_app.extensions.setdefault("runtime_readiness_cache", {})

    @staticmethod
    def _invalidate_readiness(tavern: Tavern) -> None:
        RuntimeSupervisorService._readiness_cache().pop(tavern.runtime_namespace, None)

    @staticmethod
    def _generation(tavern: Tavern) -> tuple:
        return (
            tavern.runtime_namespace, tavern.runtime_pid, tavern.runtime_started_at,
            tavern.runtime_port, tavern.game_pack, tavern.game_pack_release,
            current_app.config["RUNTIME_HOST"],
            current_app.config["RUNTIME_BOOTSTRAP_SECRET"],
        )

    @staticmethod
    def _cached_readiness(tavern: Tavern) -> Optional[_ReadyRuntime]:
        state = RuntimeSupervisorService._readiness_cache().get(tavern.runtime_namespace)
        process = RuntimeSupervisorService._processes.get(tavern.runtime_namespace)
        if (
            state is not None and process is state.process
            and process.pid == tavern.runtime_pid and process.poll() is None
            and tavern.runtime_status == "ready"
            and state.generation == RuntimeSupervisorService._generation(tavern)
        ):
            return state
        RuntimeSupervisorService._invalidate_readiness(tavern)
        return None

    @staticmethod
    def _remember_ready(tavern: Tavern) -> Optional[_ReadyRuntime]:
        state = RuntimeSupervisorService._cached_readiness(tavern)
        process = RuntimeSupervisorService._processes.get(tavern.runtime_namespace)
        # Without a retained Popen handle, PID reuse/external restarts cannot be
        # detected reliably. Those runtimes retain the uncached verification path.
        if process is None or process.pid != tavern.runtime_pid or process.poll() is not None:
            return None
        if state is None:
            state = _ReadyRuntime(process, RuntimeSupervisorService._generation(tavern), 0)
            RuntimeSupervisorService._readiness_cache()[tavern.runtime_namespace] = state
        state.checked_at = time.monotonic()
        return state

    @staticmethod
    def _identity_payload(user: User) -> dict:
        return {
            "id": user.id,
            "username": user.username,
            "nickname": user.display_name,
            "is_admin": user.is_admin,
            "is_active": user.is_active,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        }

    @staticmethod
    def _sync_changed_identity(tavern: Tavern, user: User, state: Optional[_ReadyRuntime]):
        fingerprint = tuple(RuntimeSupervisorService._identity_payload(user).items())
        if state is not None and state.identities.get(user.id) == fingerprint:
            state.identities.move_to_end(user.id)
            return True, None
        ok, error = RuntimeSupervisorService.sync_identity(tavern, user)
        if not ok:
            RuntimeSupervisorService._invalidate_readiness(tavern)
            return False, error
        if state is not None:
            state.identities[user.id] = fingerprint
            state.identities.move_to_end(user.id)
            limit = max(0, int(current_app.config["RUNTIME_IDENTITY_CACHE_MAX_USERS"]))
            while len(state.identities) > limit:
                state.identities.popitem(last=False)
        return True, None

    @staticmethod
    def _request_json(
        url: str,
        *,
        method: str = "GET",
        payload: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: float = 1.0,
    ) -> Tuple[Optional[dict], Optional[str], Optional[int]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = {"Accept": "application/json"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")), None, response.status
        except HTTPError as exc:
            raw = exc.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = None
            return data, str(exc), exc.code
        except (URLError, TimeoutError, OSError) as exc:
            return None, str(exc), None

    @staticmethod
    def _health(tavern: Tavern) -> bool:
        pack = GamePackRegistry.get(tavern.game_pack)
        data, _, status = RuntimeSupervisorService._request_json(
            f"{RuntimeSupervisorService.runtime_base_url(tavern)}/api/runtime/health",
            timeout=0.8,
        )
        return bool(
            status == 200
            and data
            and data.get("data", {}).get("runtime_id") == tavern.runtime_namespace
            and data.get("data", {}).get("game_pack") == pack.pack_id
            and data.get("data", {}).get("runtime_api_version")
            == pack.runtime_api_version
            and data.get("data", {}).get("pack_release")
            == tavern.game_pack_release
        )

    @staticmethod
    def _child_environment(tavern: Tavern, runtime_data_dir: Path) -> dict:
        pack = GamePackRegistry.require_release(
            tavern.game_pack,
            tavern.game_pack_release,
        )
        allowed_keys = RuntimeSupervisorService._SYSTEM_ENVIRONMENT_KEYS | frozenset(
            pack.runtime_environment_keys
        )
        environment = {
            key: os.environ[key]
            for key in allowed_keys
            if key in os.environ
        }
        environment.update(
            {
                "GAME_RUNTIME_ID": tavern.runtime_namespace,
                "GAME_RUNTIME_PORT": str(tavern.runtime_port),
                "GAME_RUNTIME_HOST": current_app.config["RUNTIME_HOST"],
                "GAME_RUNTIME_DATA_DIR": str(runtime_data_dir),
                "GAME_RUNTIME_BOOTSTRAP_SECRET": current_app.config[
                    "RUNTIME_BOOTSTRAP_SECRET"
                ],
                "GAME_RUNTIME_PACK_ID": tavern.game_pack,
                "GAME_RUNTIME_PACK_RELEASE": tavern.game_pack_release,
                "GAME_RUNTIME_API_VERSION": pack.runtime_api_version,
                "GAME_RUNTIME_ADAPTER_PATH": str(pack.runtime_adapter),
                "GAME_RUNTIME_IMPORT_PATHS_JSON": json.dumps(
                    [str(path) for path in pack.runtime_import_paths]
                ),
                "GAME_RUNTIME_MODEL_CAPABILITIES_JSON": json.dumps(
                    list(pack.model_capabilities)
                ),
                "GAME_RUNTIME_MODEL_PROFILES_JSON": current_app.config.get(
                    "RUNTIME_MODEL_PROFILES_JSON",
                    "[]",
                ),
                "GAME_RUNTIME_MODEL_ASSIGNMENTS_JSON": current_app.config.get(
                    "RUNTIME_MODEL_ASSIGNMENTS_JSON",
                    "{}",
                ),
                "GAME_RUNTIME_MODEL_REASONING_JSON": current_app.config.get(
                    "RUNTIME_MODEL_REASONING_JSON",
                    "{}",
                ),
                "GAME_RUNTIME_SEED_ON_START": str(
                    current_app.config["RUNTIME_SEED_ON_START"]
                ).lower(),
                "GAME_RUNTIME_SCHEDULER_ENABLED": str(
                    current_app.config["RUNTIME_SCHEDULER_ENABLED"]
                ).lower(),
                "JWT_SECRET_KEY": current_app.config["JWT_SECRET_KEY"],
                "GAME_RUNTIME_SECRET_KEY": current_app.config["SECRET_KEY"],
            }
        )

        database_url_template = current_app.config.get(
            "RUNTIME_DATABASE_URL_TEMPLATE",
            "",
        ).strip()
        if database_url_template:
            try:
                runtime_database_url = database_url_template.format(
                    runtime_id=tavern.runtime_namespace,
                    runtime_key=tavern.runtime_namespace.replace("-", ""),
                    tavern_id=tavern.id,
                    slug=tavern.slug,
                    pack_id=tavern.game_pack,
                )
                environment["GAME_RUNTIME_DATABASE_URL"] = runtime_database_url
            except (IndexError, KeyError, ValueError) as exc:
                raise ValueError(
                    "RUNTIME_DATABASE_URL_TEMPLATE 包含无效占位符"
                ) from exc
        return environment

    @staticmethod
    def _launch(tavern: Tavern) -> subprocess.Popen:
        pack = GamePackRegistry.require_release(
            tavern.game_pack,
            tavern.game_pack_release,
        )
        runtime_host_dir = pack.runtime_host_dir
        runtime_data_dir = Path(current_app.config["RUNTIME_DATA_DIR"]).resolve()
        runtime_data_dir.mkdir(parents=True, exist_ok=True)
        environment = RuntimeSupervisorService._child_environment(
            tavern,
            runtime_data_dir,
        )
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW
        log_path = runtime_data_dir / f"{tavern.runtime_namespace}.log"
        log_handle = log_path.open("ab")
        try:
            return subprocess.Popen(
                [sys.executable, str(pack.runtime_entrypoint)],
                cwd=str(runtime_host_dir),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
        finally:
            log_handle.close()

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> bool:
        if process.poll() is not None:
            return True
        # Windows venv python.exe can be a launcher with a serving child.
        # Keep process handles (including creation times), not bare reusable PIDs.
        try:
            root = psutil.Process(process.pid)
            targets = root.children(recursive=True) + [root]
            stopped = RuntimeSupervisorService._terminate_targets(targets)
            process.wait(timeout=5)
            return stopped
        except psutil.NoSuchProcess:
            return process.poll() is not None
        except (psutil.AccessDenied, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _terminate_targets(targets) -> bool:
        try:
            for target in targets:
                try:
                    target.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(targets, timeout=5)
            for target in alive:
                try:
                    target.kill()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(alive, timeout=5)
            return not alive
        except psutil.AccessDenied:
            return False

    @staticmethod
    def _owned_runtime_processes(tavern: Tavern):
        """Recover ownership after a control-plane restart without trusting a PID."""
        pack = GamePackRegistry.require_release(tavern.game_pack, tavern.game_pack_release)
        entrypoint = Path(pack.runtime_entrypoint).resolve()
        data_dir = Path(current_app.config['RUNTIME_DATA_DIR']).resolve()
        owned = []
        for process in psutil.process_iter(['cmdline']):
            try:
                args = process.info.get('cmdline') or []
                if not any(Path(arg).is_absolute() and Path(arg).resolve() == entrypoint for arg in args[1:]):
                    continue
                environment = process.environ()
                if (
                    environment.get('GAME_RUNTIME_ID') == tavern.runtime_namespace
                    and Path(environment.get('GAME_RUNTIME_DATA_DIR', '')).resolve() == data_dir
                ):
                    owned.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
                continue
        return owned

    @staticmethod
    def sync_identity(tavern: Tavern, user: User) -> Tuple[bool, Optional[str]]:
        _, error, status = RuntimeSupervisorService._request_json(
            f"{RuntimeSupervisorService.runtime_base_url(tavern)}/api/runtime/identity/sync",
            method="POST",
            payload=RuntimeSupervisorService._identity_payload(user),
            headers={
                "X-Runtime-Bootstrap-Secret": current_app.config[
                    "RUNTIME_BOOTSTRAP_SECRET"
                ]
            },
            timeout=2.0,
        )
        if status != 200:
            return False, error or "Runtime identity sync failed"
        return True, None

    @staticmethod
    def proxy_request(
        tavern: Tavern,
        *,
        runtime_path: str,
        query_string: bytes,
        method: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> Tuple[bytes, int, dict]:
        pack = GamePackRegistry.require_release(
            tavern.game_pack,
            tavern.game_pack_release,
        )
        clean_path = runtime_path.lstrip("/")
        return RuntimeSupervisorService._proxy_to_runtime_path(
            tavern,
            target_path=f"{pack.runtime_api_prefix}/{clean_path}",
            query_string=query_string,
            method=method,
            body=body,
            headers=headers,
        )

    @staticmethod
    def proxy_narrative_request(
        tavern: Tavern,
        *,
        narrative_path: str,
        query_string: bytes,
        method: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> Tuple[bytes, int, dict]:
        clean_path = str(narrative_path or "").strip().strip("/")
        if any(part in {".", ".."} for part in clean_path.split("/")):
            raise ValueError("Invalid narrative runtime path")
        suffix = f"/{clean_path}" if clean_path else ""
        return RuntimeSupervisorService._proxy_to_runtime_path(
            tavern,
            target_path=f"/api/runtime/narrative{suffix}",
            query_string=query_string,
            method=method,
            body=body,
            headers=headers,
        )

    @staticmethod
    def _proxy_timeout(tavern: Tavern, target_path: str) -> float:
        # A caller cannot raise the timeout through a header or query parameter.
        # Keep the proxy budget outside the UI's 30/60/120/600 second budgets.
        pack = GamePackRegistry.require_release(tavern.game_pack, tavern.game_pack_release)
        suffix = target_path.removeprefix(pack.runtime_api_prefix).strip('/')
        key = 'RUNTIME_PROXY_TIMEOUT_SECONDS'
        if tavern.game_pack == 's1_fantasy':
            if suffix == 'admin/export/all':
                key = 'RUNTIME_PROXY_EXPORT_TIMEOUT_SECONDS'
            elif suffix.startswith('admin/'):
                key = 'RUNTIME_PROXY_ADMIN_TIMEOUT_SECONDS'
            elif suffix.startswith('ambient-npcs/') and '/actions/' in suffix:
                key = 'RUNTIME_PROXY_ACTION_TIMEOUT_SECONDS'
        return float(current_app.config[key])

    @staticmethod
    def _proxy_to_runtime_path(
        tavern: Tavern,
        *,
        target_path: str,
        query_string: bytes,
        method: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> Tuple[bytes, int, dict]:
        url = (
            f"{RuntimeSupervisorService.runtime_base_url(tavern)}"
            f"{target_path}"
        )
        if query_string:
            url = f"{url}?{query_string.decode('latin-1')}"

        forwarded_headers = {"Accept": headers.get("Accept", "application/json")}
        for name in ("Authorization", "Content-Type"):
            if headers.get(name):
                forwarded_headers[name] = headers[name]

        proxy_request = Request(
            url,
            data=body if body else None,
            headers=forwarded_headers,
            method=method,
        )
        try:
            with urlopen(proxy_request, timeout=RuntimeSupervisorService._proxy_timeout(tavern, target_path)) as response:
                return (
                    response.read(),
                    response.status,
                    {
                        "Content-Type": response.headers.get(
                            "Content-Type",
                            "application/json",
                        ),
                        "Cache-Control": "private, no-store",
                    },
                )
        except HTTPError as exc:
            return (
                exc.read(),
                exc.code,
                {
                    "Content-Type": exc.headers.get(
                        "Content-Type",
                        "application/json",
                    ),
                    "Cache-Control": "private, no-store",
                },
            )
        except (URLError, TimeoutError, OSError):
            # The request may already have committed. Invalidate for the next
            # request, never automatically replay an ambiguous business write.
            with RuntimeSupervisorService._runtime_lock(tavern.runtime_namespace):
                RuntimeSupervisorService._invalidate_readiness(tavern)
            raise

    @staticmethod
    def _ensure_ready_locked(
        tavern: Tavern,
        user: User,
    ) -> Tuple[bool, Optional[str]]:
        # Requests may have waited behind startup or another identity sync.
        # Re-read the control-plane records after acquiring the runtime lock.
        tavern = db_manager.query(Tavern).options(lazyload("*")).populate_existing().filter_by(id=tavern.id).first()
        user = db_manager.query(User).populate_existing().filter_by(id=user.id).first()
        if tavern is None or tavern.status != "active":
            return False, "酒馆不存在或未开放"
        if user is None or not user.is_active:
            return False, "用户不存在或已停用"
        try:
            GamePackRegistry.require_release(
                tavern.game_pack,
                tavern.game_pack_release,
            )
        except ValueError:
            return False, "酒馆固定的游戏包版本未安装或与当前平台不兼容"
        port_missing = not tavern.runtime_port
        if port_missing:
            tavern.runtime_port = current_app.config["RUNTIME_BASE_PORT"] + tavern.id
        state = RuntimeSupervisorService._cached_readiness(tavern)
        if state is not None and time.monotonic() - state.checked_at < max(
            0, float(current_app.config["RUNTIME_HEALTH_CACHE_SECONDS"])
        ):
            return RuntimeSupervisorService._sync_changed_identity(tavern, user, state)
        if RuntimeSupervisorService._health(tavern):
            changed = port_missing or tavern.runtime_status != "ready" or tavern.runtime_error is not None
            tavern.runtime_status = "ready"
            tavern.runtime_error = None
            if changed:
                ok, error = db_manager.commit()
                if not ok:
                    RuntimeSupervisorService._invalidate_readiness(tavern)
                    return False, error
            state = RuntimeSupervisorService._remember_ready(tavern)
            return RuntimeSupervisorService._sync_changed_identity(tavern, user, state)

        RuntimeSupervisorService._invalidate_readiness(tavern)
        process = RuntimeSupervisorService._processes.get(tavern.runtime_namespace)
        if process is not None and process.poll() is None:
            return False, "Runtime 进程仍在运行，健康检查暂时失败，请稍后重试"
        RuntimeSupervisorService._processes.pop(tavern.runtime_namespace, None)
        tavern.runtime_status = "starting"
        tavern.runtime_error = None
        try:
            process = RuntimeSupervisorService._launch(tavern)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            tavern.runtime_pid = None
            tavern.runtime_status = "failed"
            tavern.runtime_error = f"Runtime 进程启动失败: {exc}"
            ok, error = db_manager.commit()
            return False, error or tavern.runtime_error
        RuntimeSupervisorService._processes[tavern.runtime_namespace] = process
        tavern.runtime_pid = process.pid
        ok, error = db_manager.commit()
        if not ok:
            RuntimeSupervisorService._processes.pop(
                tavern.runtime_namespace,
                None,
            )
            RuntimeSupervisorService._terminate_process(process)
            return False, error

        deadline = time.monotonic() + current_app.config[
            "RUNTIME_START_TIMEOUT_SECONDS"
        ]
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if RuntimeSupervisorService._health(tavern):
                tavern.runtime_status = "ready"
                tavern.runtime_started_at = utc_now()
                tavern.runtime_error = None
                ok, error = db_manager.commit()
                if not ok:
                    return False, error
                state = RuntimeSupervisorService._remember_ready(tavern)
                return RuntimeSupervisorService._sync_changed_identity(tavern, user, state)
            time.sleep(0.2)

        exit_code = process.poll()
        failure_message = (
            f"Runtime process exited with code {exit_code}"
            if exit_code is not None
            else "Runtime health check timed out"
        )
        RuntimeSupervisorService._processes.pop(tavern.runtime_namespace, None)
        RuntimeSupervisorService._terminate_process(process)
        tavern.runtime_pid = None
        tavern.runtime_status = "failed"
        tavern.runtime_error = failure_message
        ok, error = db_manager.commit()
        return False, error or tavern.runtime_error

    @staticmethod
    def ensure_ready(tavern: Tavern, user: User) -> Tuple[bool, Optional[str]]:
        lock = RuntimeSupervisorService._runtime_lock(tavern.runtime_namespace)
        with lock:
            return RuntimeSupervisorService._ensure_ready_locked(tavern, user)

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        # os.kill(pid, 0) is not a harmless existence probe on Windows.
        return psutil.pid_exists(pid)

    @staticmethod
    def _mark_stopped(tavern: Tavern) -> Tuple[bool, Optional[str]]:
        tavern.runtime_pid = None
        tavern.runtime_status = "stopped"
        tavern.runtime_error = None
        return db_manager.commit()

    @staticmethod
    def _stop_runtime_locked(
        tavern: Tavern,
    ) -> Tuple[bool, Optional[str]]:
        RuntimeSupervisorService._invalidate_readiness(tavern)
        process = RuntimeSupervisorService._processes.get(
            tavern.runtime_namespace,
            None,
        )
        if process is not None:
            if not RuntimeSupervisorService._terminate_process(process):
                return False, "Runtime 进程未能停止"
            RuntimeSupervisorService._processes.pop(tavern.runtime_namespace, None)
            if RuntimeSupervisorService._health(tavern):
                return False, "Runtime 子进程仍在运行，未完成停止"
            return RuntimeSupervisorService._mark_stopped(tavern)

        targets = RuntimeSupervisorService._owned_runtime_processes(tavern)
        if targets:
            if not RuntimeSupervisorService._terminate_targets(targets):
                return False, "Runtime 进程未能停止"
        elif tavern.runtime_pid and RuntimeSupervisorService._pid_exists(tavern.runtime_pid):
            return False, "Runtime PID 仍存在，但无法确认它属于当前酒馆"
        if RuntimeSupervisorService._health(tavern):
            return False, "Runtime 仍在运行，但无法确认进程归属，未执行停止"
        return RuntimeSupervisorService._mark_stopped(tavern)

    @staticmethod
    def stop_runtime(
        tavern: Tavern,
    ) -> Tuple[bool, Optional[str]]:
        lock = RuntimeSupervisorService._runtime_lock(
            tavern.runtime_namespace,
        )
        with lock:
            return RuntimeSupervisorService._stop_runtime_locked(tavern)

    @staticmethod
    def restart_runtime(
        tavern: Tavern,
        user: User,
    ) -> Tuple[bool, Optional[str]]:
        lock = RuntimeSupervisorService._runtime_lock(
            tavern.runtime_namespace,
        )
        with lock:
            stopped, error = RuntimeSupervisorService._stop_runtime_locked(
                tavern,
            )
            if not stopped:
                return False, error
            return RuntimeSupervisorService._ensure_ready_locked(tavern, user)
