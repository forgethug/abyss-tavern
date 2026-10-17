"""Resolve the active dependency environment and interrupted-upgrade guard."""
import json
import os
from pathlib import Path


def require_completed_update(root):
    if (Path(root) / '.updates/pending.json').exists():
        raise RuntimeError('上次升级未完成。请运行 tools/update.cmd（或 tools/update.py）恢复后再启动。')


def installed_python(root):
    root = Path(root).resolve()
    receipt = root / '.updates/current.json'
    if receipt.exists():
        relative = json.loads(receipt.read_text(encoding='utf8'))['python']
        # POSIX venv interpreters normally link to the system Python. Validate
        # the environment directory, but retain the venv executable path.
        path = root / relative
        path = path.parent.resolve() / path.name
        if not path.parent.is_relative_to(root / '.updates') or not path.is_file():
            raise RuntimeError('升级环境记录无效，请运行升级工具恢复。')
        return path
    return root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
