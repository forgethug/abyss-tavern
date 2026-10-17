"""Runtime-local archive directory interface used by pack data exporters."""
import hashlib
from pathlib import Path

from flask import current_app


class BackupService:
    BACKUP_DIR_NAME = 'backups'

    @staticmethod
    def get_backup_dir() -> str:
        # Freeze the app's data root at startup and namespace archives even when
        # two apps deliberately share a parent directory. IDs never become paths.
        root = Path(current_app.config['GAME_RUNTIME_DATA_DIR']).resolve()
        runtime_id = str(current_app.config['GAME_RUNTIME_ID'])
        namespace = hashlib.sha256(runtime_id.encode('utf-8')).hexdigest()
        directory = root / BackupService.BACKUP_DIR_NAME / namespace
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)
