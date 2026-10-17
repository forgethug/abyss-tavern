"""Keep the official server's control plane outside a portable tavern process."""

import importlib
import sys


def install(_app):
    from runtime_core.pack_adapter import load_runtime_extension_factory

    comment_loader = load_runtime_extension_factory(
        __file__, "comment_repository.py", "get_comment_counts"
    )
    comments = sys.modules[comment_loader.__module__]
    repository = importlib.import_module("db_repository")
    sys.modules["db_repository.s1_expedition_comment_repository"] = comments
    repository.s1_expedition_comment_repository = comments
