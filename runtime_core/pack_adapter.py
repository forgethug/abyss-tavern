import importlib.util
import hashlib
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

from runtime_core.task_registry import RuntimeTaskRegistry
from runtime_core.narrative import NarrativePolicyRegistry


@runtime_checkable
class RuntimePackAdapter(Protocol):
    pack_id: str
    plugin_name: str
    route_prefix: str

    def configure_app(self, app) -> None: ...

    def model_table_names(self, db) -> tuple[str, ...]: ...

    def register_tasks(self, registry: RuntimeTaskRegistry) -> None: ...

    def register_narrative_policy(
        self,
        registry: NarrativePolicyRegistry,
    ) -> None: ...

    def migrate_database(self, db, table_names: tuple[str, ...]) -> None: ...

    def verify_models(self, db) -> None: ...

    def seed(self) -> None: ...

    def get_scheduler_tasks(self) -> list: ...

    def after_async_task_init(self, app) -> None: ...

    def health_metadata(self, app) -> dict: ...


def load_runtime_pack_adapter(adapter_path: Path) -> RuntimePackAdapter:
    resolved_path = Path(adapter_path).resolve()
    if not resolved_path.is_file() or resolved_path.suffix != ".py":
        raise RuntimeError(
            f"Runtime pack adapter does not exist: {resolved_path}"
        )
    path_digest = hashlib.sha256(str(resolved_path).encode("utf-8")).hexdigest()[:16]
    module_name = f"_game_pack_adapter_{path_digest}"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, resolved_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Unable to load runtime pack adapter: {resolved_path}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    factory = getattr(module, "create_runtime_adapter", None)
    if not callable(factory):
        raise RuntimeError(
            f"Runtime pack adapter has no create_runtime_adapter(): {resolved_path}"
        )
    adapter = factory()
    if not isinstance(adapter, RuntimePackAdapter):
        raise RuntimeError(
            f"Runtime pack adapter does not satisfy the shared contract: "
            f"{resolved_path}"
        )
    return adapter


def load_runtime_extension_factory(
    owner_path: Path,
    sibling_filename: str,
    factory_name: str,
):
    """Load a pack-local extension without adding pack folders to sys.path."""

    resolved_path = Path(owner_path).resolve().with_name(sibling_filename)
    if not resolved_path.is_file() or resolved_path.suffix != ".py":
        raise RuntimeError(
            f"Runtime pack extension does not exist: {resolved_path}"
        )
    path_digest = hashlib.sha256(
        str(resolved_path).encode("utf-8")
    ).hexdigest()[:16]
    module_name = f"_game_pack_extension_{path_digest}"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, resolved_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Unable to load runtime pack extension: {resolved_path}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise RuntimeError(
            f"Runtime pack extension has no {factory_name}(): {resolved_path}"
        )
    return factory
