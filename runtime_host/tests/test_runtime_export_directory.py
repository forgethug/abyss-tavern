from pathlib import Path
import zipfile

from flask import Flask


def test_export_directories_are_runtime_local_and_ids_cannot_escape(tmp_path):
    from services.backup_service import BackupService
    first,second = Flask('first'),Flask('second')
    first.config.update(GAME_RUNTIME_DATA_DIR=str(tmp_path),GAME_RUNTIME_ID='../first')
    second.config.update(GAME_RUNTIME_DATA_DIR=str(tmp_path),GAME_RUNTIME_ID='../second')
    with first.app_context():
        first_path = Path(BackupService.get_backup_dir())
        (first_path/'first.txt').write_text('first',encoding='utf-8')
        assert first_path.is_relative_to(tmp_path)
    with second.app_context():
        second_path = Path(BackupService.get_backup_dir())
        assert second_path.is_relative_to(tmp_path)
        assert second_path != first_path
        assert not (second_path/'first.txt').exists()


def test_s1_export_writes_a_valid_archive_inside_runtime(runtime_app,tmp_path):
    from plugins.adventurer_s1.services.s1_export_service import S1ExportService
    runtime_app.config['GAME_RUNTIME_DATA_DIR'] = str(tmp_path)
    with runtime_app.app_context():
        result = S1ExportService.write_zip_archive()
        archive = S1ExportService.get_export_dir()/result['filename']
        assert archive.is_relative_to(tmp_path)
        assert archive.stat().st_size == result['size']
        with zipfile.ZipFile(archive) as saved:
            assert saved.testzip() is None
            assert saved.namelist()
