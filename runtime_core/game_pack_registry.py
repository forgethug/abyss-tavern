import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


class GamePackConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class GamePackDefinition:
    pack_id: str
    display_name: str
    platform_api_version: str
    runtime_api_version: str
    pack_version: str
    manifest_path: Path
    runtime_host_dir: Path
    runtime_entrypoint: Path
    runtime_adapter: Path
    runtime_api_prefix: str
    frontend_plugin: str
    model_capabilities: Tuple[str, ...]
    runtime_environment_keys: Tuple[str, ...]
    runtime_import_paths: Tuple[Path, ...]

    def to_public_dict(self) -> dict:
        return {
            "pack_id": self.pack_id,
            "display_name": self.display_name,
            "platform_api_version": self.platform_api_version,
            "runtime_api_version": self.runtime_api_version,
            "pack_version": self.pack_version,
            "frontend_plugin": self.frontend_plugin,
            "model_capabilities": list(self.model_capabilities),
        }


class GamePackRegistry:
    PLATFORM_API_VERSION = "0.1"
    RUNTIME_API_VERSION = "0.1"
    _PACK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
    _FRONTEND_PLUGIN_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

    @staticmethod
    def repository_root() -> Path:
        return Path(__file__).resolve().parents[1]

    @classmethod
    def game_packs_root(cls) -> Path:
        return cls.repository_root() / "game-packs"

    @staticmethod
    def _required_string(data: dict, key: str, manifest_path: Path) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise GamePackConfigurationError(
                f"{manifest_path}: {key} must be a non-empty string"
            )
        return value.strip()

    @classmethod
    def _resolve_repository_path(
        cls,
        base_dir: Path,
        relative_path: str,
        *,
        field_name: str,
        manifest_path: Path,
    ) -> Path:
        candidate = (base_dir / relative_path).resolve()
        repository_root = cls.repository_root().resolve()
        try:
            candidate.relative_to(repository_root)
        except ValueError as exc:
            raise GamePackConfigurationError(
                f"{manifest_path}: {field_name} points outside the repository"
            ) from exc
        return candidate

    @classmethod
    def _load_manifest(cls, manifest_path: Path) -> GamePackDefinition:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GamePackConfigurationError(
                f"Unable to read game pack manifest: {manifest_path}"
            ) from exc
        if not isinstance(data, dict):
            raise GamePackConfigurationError(
                f"{manifest_path}: manifest root must be an object"
            )

        pack_id = cls._required_string(data, "pack_id", manifest_path)
        if not cls._PACK_ID_PATTERN.fullmatch(pack_id):
            raise GamePackConfigurationError(
                f"{manifest_path}: invalid pack_id {pack_id!r}"
            )
        display_name = cls._required_string(data, "display_name", manifest_path)
        platform_api_version = cls._required_string(
            data,
            "platform_api_version",
            manifest_path,
        )
        runtime_api_version = cls._required_string(
            data,
            "runtime_api_version",
            manifest_path,
        )
        pack_version = cls._required_string(data, "pack_version", manifest_path)
        if platform_api_version != cls.PLATFORM_API_VERSION:
            raise GamePackConfigurationError(
                f"{manifest_path}: unsupported platform_api_version "
                f"{platform_api_version!r}"
            )
        if runtime_api_version != cls.RUNTIME_API_VERSION:
            raise GamePackConfigurationError(
                f"{manifest_path}: unsupported runtime_api_version "
                f"{runtime_api_version!r}"
            )

        entrypoints = data.get("entrypoints")
        if not isinstance(entrypoints, dict):
            raise GamePackConfigurationError(
                f"{manifest_path}: entrypoints must be an object"
            )
        runtime_host_value = cls._required_string(
            entrypoints,
            "runtime_host",
            manifest_path,
        )
        runtime_entrypoint_value = cls._required_string(
            entrypoints,
            "runtime_entrypoint",
            manifest_path,
        )
        runtime_api_prefix = cls._required_string(
            entrypoints,
            "runtime_api_prefix",
            manifest_path,
        ).rstrip("/")
        frontend_plugin = cls._required_string(
            entrypoints,
            "frontend_plugin",
            manifest_path,
        )
        runtime_adapter_value = cls._required_string(
            entrypoints,
            "runtime_adapter",
            manifest_path,
        )
        if not runtime_api_prefix.startswith("/api/"):
            raise GamePackConfigurationError(
                f"{manifest_path}: runtime_api_prefix must start with /api/"
            )
        if not cls._FRONTEND_PLUGIN_PATTERN.fullmatch(frontend_plugin):
            raise GamePackConfigurationError(
                f"{manifest_path}: invalid frontend_plugin {frontend_plugin!r}"
            )
        raw_environment_keys = data.get("runtime_environment_keys", [])
        if not isinstance(raw_environment_keys, list) or any(
            not isinstance(key, str)
            or not re.fullmatch(r"^[A-Z][A-Z0-9_]{0,127}$", key)
            for key in raw_environment_keys
        ):
            raise GamePackConfigurationError(
                f"{manifest_path}: runtime_environment_keys must contain "
                "environment variable names"
            )
        runtime_environment_keys = tuple(dict.fromkeys(raw_environment_keys))
        raw_model_capabilities = data.get("model_capabilities", [])
        if not isinstance(raw_model_capabilities, list) or any(
            not isinstance(capability, str)
            or not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", capability)
            for capability in raw_model_capabilities
        ):
            raise GamePackConfigurationError(
                f"{manifest_path}: model_capabilities must contain identifiers"
            )
        model_capabilities = tuple(dict.fromkeys(raw_model_capabilities))
        raw_import_paths = data.get("runtime_import_paths", [])
        if not isinstance(raw_import_paths, list) or any(
            not isinstance(path, str) or not path.strip()
            for path in raw_import_paths
        ):
            raise GamePackConfigurationError(
                f"{manifest_path}: runtime_import_paths must contain paths"
            )
        runtime_import_paths = tuple(
            cls._resolve_repository_path(
                manifest_path.parent,
                path,
                field_name="runtime_import_paths",
                manifest_path=manifest_path,
            )
            for path in dict.fromkeys(raw_import_paths)
        )
        if any(not path.is_dir() for path in runtime_import_paths):
            raise GamePackConfigurationError(
                f"{manifest_path}: runtime_import_paths directory does not exist"
            )

        runtime_host_dir = cls._resolve_repository_path(
            manifest_path.parent,
            runtime_host_value,
            field_name="runtime_host",
            manifest_path=manifest_path,
        )
        runtime_entrypoint = cls._resolve_repository_path(
            runtime_host_dir,
            runtime_entrypoint_value,
            field_name="runtime_entrypoint",
            manifest_path=manifest_path,
        )
        runtime_adapter = cls._resolve_repository_path(
            manifest_path.parent,
            runtime_adapter_value,
            field_name="runtime_adapter",
            manifest_path=manifest_path,
        )
        try:
            runtime_entrypoint.relative_to(runtime_host_dir)
        except ValueError as exc:
            raise GamePackConfigurationError(
                f"{manifest_path}: runtime_entrypoint points outside runtime_host"
            ) from exc
        if not runtime_host_dir.is_dir():
            raise GamePackConfigurationError(
                f"{manifest_path}: runtime_host directory does not exist"
            )
        if not runtime_entrypoint.is_file():
            raise GamePackConfigurationError(
                f"{manifest_path}: runtime_entrypoint file does not exist"
            )
        if not runtime_adapter.is_file() or runtime_adapter.suffix != ".py":
            raise GamePackConfigurationError(
                f"{manifest_path}: runtime_adapter file does not exist"
            )

        return GamePackDefinition(
            pack_id=pack_id,
            display_name=display_name,
            platform_api_version=platform_api_version,
            runtime_api_version=runtime_api_version,
            pack_version=pack_version,
            manifest_path=manifest_path,
            runtime_host_dir=runtime_host_dir,
            runtime_entrypoint=runtime_entrypoint,
            runtime_adapter=runtime_adapter,
            runtime_api_prefix=runtime_api_prefix,
            frontend_plugin=frontend_plugin,
            model_capabilities=model_capabilities,
            runtime_environment_keys=runtime_environment_keys,
            runtime_import_paths=runtime_import_paths,
        )

    @classmethod
    def all(cls) -> Dict[str, GamePackDefinition]:
        definitions: Dict[str, GamePackDefinition] = {}
        for manifest_path in sorted(cls.game_packs_root().glob("*/manifest.json")):
            definition = cls._load_manifest(manifest_path)
            if definition.pack_id in definitions:
                raise GamePackConfigurationError(
                    f"Duplicate game pack id: {definition.pack_id}"
                )
            definitions[definition.pack_id] = definition
        if not definitions:
            raise GamePackConfigurationError("No game packs are registered")
        return definitions

    @classmethod
    def get(cls, pack_id: str) -> GamePackDefinition:
        normalized_pack_id = str(pack_id or "").strip()
        definition = cls.all().get(normalized_pack_id)
        if definition is None:
            raise GamePackConfigurationError(
                f"Unknown game pack: {normalized_pack_id or '<empty>'}"
            )
        return definition

    @classmethod
    def require_release(
        cls,
        pack_id: str,
        pack_version: str,
    ) -> GamePackDefinition:
        definition = cls.get(pack_id)
        if definition.pack_version != str(pack_version or "").strip():
            raise GamePackConfigurationError(
                f"Game pack release is not installed: "
                f"{pack_id}@{pack_version or '<empty>'}"
            )
        return definition

    @classmethod
    def public_list(cls) -> List[dict]:
        return [
            definition.to_public_dict()
            for definition in cls.all().values()
        ]
