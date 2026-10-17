import json
import os
import sys
from pathlib import Path


HOST_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = HOST_ROOT.parent


def _pack_import_paths() -> list[Path]:
    raw_paths = os.getenv("GAME_RUNTIME_IMPORT_PATHS_JSON", "[]")
    try:
        values = json.loads(raw_paths)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GAME_RUNTIME_IMPORT_PATHS_JSON must be valid JSON"
        ) from exc
    if not isinstance(values, list) or any(
        not isinstance(value, str)
        for value in values
    ):
        raise RuntimeError(
            "GAME_RUNTIME_IMPORT_PATHS_JSON must be an array of paths"
        )
    paths = [Path(value).resolve() for value in values]
    if any(not path.is_dir() for path in paths):
        raise RuntimeError("A configured game pack import path does not exist")
    return paths


def install_runtime_import_paths() -> None:
    ordered_paths = [
        HOST_ROOT,
        REPOSITORY_ROOT,
        *_pack_import_paths(),
    ]
    for index, path in enumerate(ordered_paths):
        value = str(path)
        if value in sys.path:
            sys.path.remove(value)
        sys.path.insert(index, value)
