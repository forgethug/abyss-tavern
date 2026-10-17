"""深渊酒馆 S1 玩家业务数据导出。

导出是只读快照：玩家业务表按 CSV 输出，探险正文按 Markdown 输出，最后
打包为一个 zip。这里不复用管理员数据库备份，避免把后台编辑、修复和 AI
运行凭据带出。
"""

from __future__ import annotations
from runtime_core.time import utc_from_timestamp as _portable_utc_from_timestamp, utc_now as _portable_utc_now

import csv
import io
import json
import os
import re
import tempfile
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Table, inspect, select

from extensions import db
from services.backup_service import BackupService


class S1ExportService:
    """生成管理员可下载的 S1 玩家业务数据归档。"""

    EXPORT_DIR_NAME = BackupService.BACKUP_DIR_NAME
    EXPORT_FILE_PREFIX = 'adventurer-s1-player-export-'

    # 这些表承载管理员修改、审计、AI 运行或补偿过程，不属于玩家业务档案。
    EXCLUDED_TABLES = frozenset({
        'adventurer_s1_settings',
        'adventurer_s1_data_appeals',
        'adventurer_s1_data_appeal_audit_logs',
        'adventurer_s1_data_appeal_quota',
        'adventurer_s1_bug_reclaim_batches',
        'adventurer_s1_bug_reclaim_scan_states',
        'adventurer_s1_bug_reclaim_candidates',
        'adventurer_s1_bug_reclaim_items',
        'adventurer_s1_companion_evolution_scan_states',
        'adventurer_s1_companion_evolution_scan_events',
        'adventurer_s1_bug_qty_shortfall_batches',
        'adventurer_s1_bug_qty_shortfall_items',
        'adventurer_s1_expedition_consumption_repairs',
        'adventurer_s1_gain_anomalies',
        'adventurer_s1_ai_feedback',
        'adventurer_s1_token_usages',
        'adventurer_s1_description_ai_asset_versions',
        'adventurer_s1_description_ai_backlog',
        'adventurer_s1_description_ai_cache',
        'adventurer_s1_building_ai_failure_audits',
        'adventurer_s1_expedition_observer_reviews',
        'adventurer_s1_expedition_audits',
        'adventurer_s1_expedition_audit_voters',
    })
    # 后台覆盖字段/原始 AI 载荷即使落在玩家业务表中也不得导出。
    EXCLUDED_COLUMNS = frozenset({
        'dm_instruction', 'dm_instruction_revision', 'admin_note',
        'admin_user_id', 'admin_id', 'resolved_by_user_id', 'resolution_note',
        'archived_by_user_id', 'raw_response', 'review_raw_response',
        'review_data', 'generation_owner_boot_id', 'maintenance_claim_token',
        'maintenance_claim_expires_at', 'prompt_version', 'last_error',
    })
    EXCLUDED_JSON_KEYS = frozenset({
        'waiting_player_npc_injection', 'cross_realm_mirror_injection',
        'waiting_npc_updates', 'waiting_npc_persistence',
        'waiting_npc_review_quarantine', 'countermeasure_learning',
        'countermeasure_opportunity_persistence',
        'countermeasure_application_persistence',
        'countermeasure_learning_persistence', 'objective_outline',
        'hidden_dungeon_context', 'review_failure_quarantine_receipt',
        'commission_reconciliation_receipt', 'settlement_conflict_repairs',
    })
    # full_log 作为独立 Markdown 导出，避免 CSV 中重复整篇正文。
    LARGE_STORY_COLUMNS = frozenset({'full_log', 'player_name_history'})
    _SAFE_NAME = re.compile(r'[^\w\-\u4e00-\u9fff]+', re.UNICODE)

    @classmethod
    def get_export_dir(cls) -> Path:
        """返回服务器端归档目录，并确保目录存在。"""
        return Path(BackupService.get_backup_dir())

    @classmethod
    def write_zip_archive(cls) -> dict[str, Any]:
        """将归档原子写入服务器目录，避免通过 HTTP 返回大响应体。"""
        payload, counts = cls.build_zip()
        timestamp = _portable_utc_now().strftime('%Y%m%d-%H%M%S-%f')
        filename = f'{cls.EXPORT_FILE_PREFIX}{timestamp}.zip'
        directory = cls.get_export_dir()
        target = directory / filename
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='wb', prefix=f'.{filename}.', suffix='.partial',
                dir=directory, delete=False,
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
        stat = target.stat()
        return {
            'filename': filename,
            'directory': cls.EXPORT_DIR_NAME,
            'relative_path': f'{cls.EXPORT_DIR_NAME}/{filename}',
            'size': stat.st_size,
            'size_mb': round(stat.st_size / 1024 / 1024, 2),
            'created_at': _portable_utc_from_timestamp(stat.st_mtime).isoformat() + 'Z',
            'counts': counts,
        }

    @classmethod
    def list_archives(cls) -> list[dict[str, Any]]:
        """列出已生成的 S1 归档，不返回临时文件或其他数据库备份。"""
        directory = cls.get_export_dir()
        archives: list[dict[str, Any]] = []
        for path in directory.glob(f'{cls.EXPORT_FILE_PREFIX}*.zip'):
            try:
                stat = path.stat()
            except OSError:
                continue
            archives.append({
                'filename': path.name,
                'directory': cls.EXPORT_DIR_NAME,
                'relative_path': f'{cls.EXPORT_DIR_NAME}/{path.name}',
                'size': stat.st_size,
                'size_mb': round(stat.st_size / 1024 / 1024, 2),
                'created_at': _portable_utc_from_timestamp(stat.st_mtime).isoformat() + 'Z',
            })
        archives.sort(key=lambda item: item['created_at'], reverse=True)
        return archives

    @classmethod
    def build_zip(cls) -> tuple[bytes, dict[str, int]]:
        # 使用已注册的 ORM Table，保留 ColdExpeditionLogJSON 等自定义类型的
        # result processor；直接 reflect 会把压缩正文当作原始数据库值读出。
        metadata = db.metadata
        existing_tables = set(inspect(db.engine).get_table_names())
        counts: dict[str, int] = {}
        files: dict[str, bytes] = {}
        for table_name in sorted(
            name for name in metadata.tables
            if (name == 'adventurer_s1' or name.startswith('adventurer_s1_'))
            and name in existing_tables
        ):
            if table_name in cls.EXCLUDED_TABLES:
                continue
            table = metadata.tables[table_name]
            payload, count = cls._export_table(table)
            if count == 0 and table_name not in {
                'adventurer_s1', 'adventurer_s1_expedition_logs',
                'adventurer_s1_chat_messages', 'adventurer_s1_building_instances',
                'adventurer_s1_dorm_rooms',
            }:
                continue
            files[f'csv/{table_name}.csv'] = payload
            counts[table_name] = count

        story_files, story_count = cls._export_stories(metadata.tables.get('adventurer_s1_expedition_logs'))
        files.update({f'stories/{name}': body for name, body in story_files.items()})
        counts['stories'] = story_count

        manifest = {
            'format': 'adventurer_s1_player_export_v1',
            'generated_at': _portable_utc_now().isoformat(timespec='seconds') + 'Z',
            'scope': 'S1 玩家业务数据；不含管理员后台修改、审计、AI 运行凭据与原始响应',
            'files': sorted(files),
            'counts': counts,
        }
        files['README.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files):
                archive.writestr(name, files[name])
        return output.getvalue(), counts

    @classmethod
    def _export_table(cls, table: Table) -> tuple[bytes, int]:
        columns = [
            column for column in table.columns
            if column.name not in cls.EXCLUDED_COLUMNS
            and column.name not in cls.LARGE_STORY_COLUMNS
        ]
        rows = db.session.execute(select(table)).mappings().all()
        stream = io.StringIO(newline='')
        writer = csv.writer(stream, lineterminator='\n')
        headers = [column.name for column in columns]
        senders: dict[int, tuple[str, str]] = {}
        if table.name == 'adventurer_s1_chat_messages':
            from models.user import User
            user_ids = {int(row['user_id']) for row in rows if row.get('user_id') is not None}
            if user_ids:
                senders = {
                    int(user.id): (str(user.username or ''), str(user.nickname or ''))
                    for user in User.query.filter(User.id.in_(user_ids)).all()
                }
            headers.extend(['sender_username', 'sender_nickname'])
        writer.writerow(headers)
        for row in rows:
            values = [cls._cell(row.get(column.name)) for column in columns]
            if table.name == 'adventurer_s1_chat_messages':
                username, nickname = senders.get(int(row['user_id']), ('', '未知'))
                values.extend([username, nickname or username or '未知'])
            writer.writerow(values)
        return ('\ufeff' + stream.getvalue()).encode('utf-8'), len(rows)

    @classmethod
    def _export_stories(cls, table: Table | None) -> tuple[dict[str, bytes], int]:
        if table is None:
            return {}, 0
        member_table = db.metadata.tables.get('adventurer_s1_expedition_log_members')
        adventurer_table = db.metadata.tables.get('adventurer_s1')
        member_names: dict[int, list[str]] = {}
        if member_table is not None and adventurer_table is not None:
            query = (
                select(member_table.c.expedition_log_id, adventurer_table.c.name)
                .select_from(member_table.join(adventurer_table, member_table.c.adventurer_id == adventurer_table.c.id))
                .order_by(member_table.c.expedition_log_id, member_table.c.id)
            )
            for row in db.session.execute(query).all():
                member_names.setdefault(int(row[0]), []).append(str(row[1] or '未知'))

        files: dict[str, bytes] = {}
        used_names: set[str] = set()
        statement = select(table)
        if 'id' in table.c:
            statement = statement.order_by(table.c.id)
        rows = db.session.execute(statement).mappings().all()
        for row in rows:
            story = row.get('full_log')
            location = row.get('hidden_dungeon') or row.get('temporary_location_name') or row.get('target_dungeon') or '未知地点'
            created = row.get('created_at')
            day = created.strftime('%Y%m%d') if hasattr(created, 'strftime') else '未知日期'
            members = member_names.get(int(row['id']), []) or ['未知小队']
            stem = cls._safe_name(f"{row['id']}-{location}-{day}-{'、'.join(members)}")[:180]
            if not stem:
                stem = f"{row['id']}-探险故事"
            filename = f'{stem}.md'
            suffix = 2
            while filename in used_names:
                filename = f'{stem}-{suffix}.md'
                suffix += 1
            used_names.add(filename)
            files[filename] = cls._story_markdown(row, story, location, members).encode('utf-8')
        return files, len(rows)

    @classmethod
    def _story_markdown(cls, row: Any, story: Any, location: str, members: list[str]) -> str:
        if isinstance(story, (dict, list)):
            body = json.dumps(cls._redact_json(story), ensure_ascii=False, indent=2)
        else:
            body = str(story or '')
        return (
            f"# {row['id']} - {location}\n\n"
            f"- 日期：{cls._cell(row.get('created_at'))}\n"
            f"- 地点：{location}\n"
            f"- 小队成员：{'、'.join(members)}\n"
            f"- 状态：{row.get('status') or ''}\n\n"
            f"## 故事正文\n\n{body}\n"
        )

    @classmethod
    def _redact_json(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._redact_json(item)
                for key, item in value.items()
                if key not in cls.EXCLUDED_COLUMNS
                and key not in cls.EXCLUDED_JSON_KEYS
                and not str(key).startswith('_')
            }
        if isinstance(value, list):
            return [cls._redact_json(item) for item in value]
        return value

    @classmethod
    def _cell(cls, value: Any) -> str:
        if value is None:
            return ''
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(cls._redact_json(value), ensure_ascii=False, separators=(',', ':'))
        return str(value)

    @classmethod
    def _safe_name(cls, value: str) -> str:
        return cls._SAFE_NAME.sub('_', value).strip(' ._') or '未命名'
