from runtime_core.game_pack_registry import (
    GamePackConfigurationError,
    GamePackDefinition,
    GamePackRegistry as BaseGamePackRegistry,
)


class GamePackRegistry(BaseGamePackRegistry):
    """The games shipped by this application; unrelated local packs stay private."""

    @classmethod
    def all(cls):
        definition = cls._load_manifest(cls.game_packs_root() / "s1-fantasy" / "manifest.json")
        return {definition.pack_id: definition}

__all__ = [
    "GamePackConfigurationError",
    "GamePackDefinition",
    "GamePackRegistry",
]
