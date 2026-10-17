"""Durable source discovery; AI ownership stays in the existing cache claims."""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime, timedelta
from sqlalchemy import case

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1DescriptionAiBacklog as Backlog
from plugins.adventurer_s1.models import AdventurerS1DescriptionAiAssetVersion as Version


def remember_description_sources(
    realm_code: str, items: list[dict], *, observed_at: datetime | None = None,
    completed_hashes: set[str] | None = None,
) -> None:
    if not items:
        return
    now = _portable_utc_now()
    observed_at = observed_at or now
    completed_hashes = completed_hashes or set()
    by_asset = {item['asset_key']: item for item in items if item.get('asset_key')}
    with db_manager.transaction():
        if db.engine.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        elif db.engine.dialect.name == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise RuntimeError('Unsupported description backlog database')
        if by_asset:
            db.session.execute(insert(Version).values([
                dict(realm_code=realm_code, asset_key=key,
                     source_hash=by_asset[key]['source_hash'], observed_at=observed_at)
                for key in sorted(by_asset)
            ]).on_conflict_do_nothing(index_elements=['realm_code', 'asset_key']))
        versions = (Version.query.filter(
            Version.realm_code == realm_code, Version.asset_key.in_(sorted(by_asset)),
        ).order_by(Version.asset_key).populate_existing().with_for_update().all()) if by_asset else []
        accepted = [item for item in items if not item.get('asset_key')]
        replacements = {}
        for row in versions:
            if row.observed_at > observed_at:
                continue  # 较早的读取晚到，不得把指针退回旧内容。
            item = by_asset[row.asset_key]
            if row.source_hash != item['source_hash']:
                replacements.setdefault(item['source_hash'], set()).add(row.source_hash)
                row.source_hash = item['source_hash']
            row.observed_at = observed_at
            accepted.append(item)
        old_hashes = set().union(*replacements.values()) if replacements else set()
        unique = {item['source_hash']: item for item in accepted}
        tracked_hashes = {item['source_hash'] for item in accepted if item.get('asset_key')}
        pending = {
            row.source_hash: row for row in Backlog.query.filter(
                Backlog.realm_code == realm_code,
                Backlog.source_hash.in_(sorted(old_hashes | set(unique))),
            ).order_by(Backlog.source_hash).populate_existing().with_for_update().all()
        }
        values = []
        for key in sorted(unique):
            if key in completed_hashes:
                continue
            inherited = [pending[h] for h in replacements.get(key, ()) if h in pending]
            created = min([r.created_at for r in inherited] + [now])
            next_check = min([r.next_check_at for r in inherited] + [now])
            source = {k: unique[key][k] for k in ('source_hash', 'kind', 'name', 'description')}
            if key in tracked_hashes:
                source['asset_version_tracked'] = True
            if key in pending:
                # 同版重复进入不改队列位置。换版且已有共享任务时继承更早位置。
                if inherited:
                    pending[key].created_at = min(pending[key].created_at, created)
                    pending[key].next_check_at = min(pending[key].next_check_at, next_check)
                if source.get('asset_version_tracked') and not pending[key].source_item.get('asset_version_tracked'):
                    pending[key].source_item = source
                continue
            values.append(dict(realm_code=realm_code, source_hash=key, source_item=source,
                               created_at=created, next_check_at=next_check))
        if values:
            statement = insert(Backlog).values(values)
            db.session.execute(statement.on_conflict_do_update(
                index_elements=['realm_code', 'source_hash'],
                set_={
                    'created_at': case(
                        (Backlog.created_at > statement.excluded.created_at, statement.excluded.created_at),
                        else_=Backlog.created_at,
                    ),
                    'next_check_at': case(
                        (Backlog.next_check_at > statement.excluded.next_check_at, statement.excluded.next_check_at),
                        else_=Backlog.next_check_at,
                    ),
                },
            ))
        db.session.flush()
        if old_hashes:
            referenced = Version.query.filter(
                Version.realm_code == Backlog.realm_code,
                Version.source_hash == Backlog.source_hash,
            ).exists()
            Backlog.query.filter(
                Backlog.realm_code == realm_code, Backlog.source_hash.in_(old_hashes),
                ~referenced,
            ).delete(synchronize_session=False)


def current_description_source_hashes(realm_code: str, source_hashes: set[str]) -> set[str]:
    """只读校验已登记资产的当前版本；不把历史请求视为当前需求。"""
    if not source_hashes:
        return set()
    from sqlalchemy import select
    with db.engine.connect() as connection:
        rows = connection.execute(select(Version.source_hash).where(
            Version.realm_code == realm_code, Version.source_hash.in_(sorted(source_hashes)),
        ).distinct()).all()
    return {row[0] for row in rows}


def take_description_sources(realm_code: str, *, limit: int, revisit_seconds: int) -> list[dict]:
    """Move one bounded page to the tail before any Redis/AI work; never erase it."""
    now = _portable_utc_now()
    with db_manager.transaction():
        rows = (Backlog.query.filter(
            Backlog.realm_code == realm_code, Backlog.next_check_at <= now,
        ).order_by(Backlog.next_check_at, Backlog.created_at, Backlog.source_hash)
            .limit(limit).populate_existing().with_for_update(skip_locked=True).all())
        items = [dict(row.source_item) for row in rows]
        for row in rows:
            row.next_check_at = now + timedelta(seconds=revisit_seconds)
    db_manager.remove()
    return items


def forget_completed_description_sources(realm_code: str, source_hashes: set[str]) -> None:
    if not source_hashes:
        return
    with db_manager.transaction():
        Backlog.query.filter(
            Backlog.realm_code == realm_code, Backlog.source_hash.in_(sorted(source_hashes)),
        ).delete(synchronize_session=False)
