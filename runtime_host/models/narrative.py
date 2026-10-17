from runtime_core.time import utc_now
from datetime import datetime

from extensions import db
from utils.time_utils import to_utc_iso


class _NarrativeTimestampMixin:
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now,
        index=True,
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    def _timestamps(self):
        return {
            "created_at": to_utc_iso(self.created_at),
            "updated_at": to_utc_iso(self.updated_at),
        }


class RuntimeWorldEntity(_NarrativeTimestampMixin, db.Model):
    __tablename__ = "runtime_world_entities"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    external_key = db.Column(db.String(160), nullable=False, unique=True)
    kind = db.Column(db.String(64), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    aliases = db.Column(db.JSON, nullable=False, default=list)
    summary = db.Column(db.Text, nullable=False, default="")
    status = db.Column(
        db.String(24),
        nullable=False,
        default="active",
        index=True,
    )
    visibility = db.Column(
        db.String(24),
        nullable=False,
        default="members",
        index=True,
    )
    scope_keys = db.Column(db.JSON, nullable=False, default=list)
    attributes = db.Column(db.JSON, nullable=False, default=dict)
    importance = db.Column(db.Integer, nullable=False, default=50, index=True)
    revision = db.Column(db.Integer, nullable=False, default=1)
    source_ref = db.Column(db.String(200), nullable=True, index=True)
    contract_version = db.Column(
        db.String(32),
        nullable=False,
        default="0.1",
    )

    def to_narrative_dict(self):
        return {
            "record_type": "entity",
            "source": "shared",
            "id": self.id,
            "external_key": self.external_key,
            "kind": self.kind,
            "name": self.name,
            "aliases": self.aliases or [],
            "summary": self.summary,
            "status": self.status,
            "visibility": self.visibility,
            "scope_keys": self.scope_keys or [],
            "attributes": self.attributes or {},
            "importance": self.importance,
            "revision": self.revision,
            "source_ref": self.source_ref,
            "contract_version": self.contract_version,
            **self._timestamps(),
        }


class RuntimeWorldFact(_NarrativeTimestampMixin, db.Model):
    __tablename__ = "runtime_world_facts"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    fact_key = db.Column(db.String(180), nullable=False, unique=True)
    kind = db.Column(db.String(64), nullable=False, index=True)
    subject_key = db.Column(db.String(160), nullable=True, index=True)
    predicate = db.Column(db.String(120), nullable=False)
    value = db.Column(db.JSON, nullable=False)
    summary = db.Column(db.Text, nullable=False, default="")
    status = db.Column(
        db.String(24),
        nullable=False,
        default="active",
        index=True,
    )
    visibility = db.Column(
        db.String(24),
        nullable=False,
        default="members",
        index=True,
    )
    scope_keys = db.Column(db.JSON, nullable=False, default=list)
    revision = db.Column(db.Integer, nullable=False, default=1)
    source_ref = db.Column(db.String(200), nullable=True, index=True)
    contract_version = db.Column(
        db.String(32),
        nullable=False,
        default="0.1",
    )

    def to_narrative_dict(self):
        return {
            "record_type": "fact",
            "source": "shared",
            "id": self.id,
            "key": self.fact_key,
            "kind": self.kind,
            "subject_key": self.subject_key,
            "predicate": self.predicate,
            "value": self.value,
            "summary": self.summary,
            "status": self.status,
            "visibility": self.visibility,
            "scope_keys": self.scope_keys or [],
            "revision": self.revision,
            "source_ref": self.source_ref,
            "contract_version": self.contract_version,
            **self._timestamps(),
        }


class RuntimeStoryThread(_NarrativeTimestampMixin, db.Model):
    __tablename__ = "runtime_story_threads"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    external_key = db.Column(db.String(180), nullable=False, unique=True)
    kind = db.Column(db.String(64), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False, index=True)
    summary = db.Column(db.Text, nullable=False, default="")
    next_hook = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(24),
        nullable=False,
        default="active",
        index=True,
    )
    visibility = db.Column(
        db.String(24),
        nullable=False,
        default="members",
        index=True,
    )
    scope_keys = db.Column(db.JSON, nullable=False, default=list)
    entity_refs = db.Column(db.JSON, nullable=False, default=list)
    attributes = db.Column(db.JSON, nullable=False, default=dict)
    priority = db.Column(db.Integer, nullable=False, default=50, index=True)
    revision = db.Column(db.Integer, nullable=False, default=1)
    injection_count = db.Column(db.Integer, nullable=False, default=0)
    last_injected_at = db.Column(db.DateTime, nullable=True, index=True)
    cooldown_until = db.Column(db.DateTime, nullable=True, index=True)
    source_ref = db.Column(db.String(200), nullable=True, index=True)
    contract_version = db.Column(
        db.String(32),
        nullable=False,
        default="0.1",
    )

    def to_narrative_dict(self):
        return {
            "record_type": "story_thread",
            "source": "shared",
            "id": self.id,
            "external_key": self.external_key,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "next_hook": self.next_hook,
            "status": self.status,
            "visibility": self.visibility,
            "scope_keys": self.scope_keys or [],
            "entity_refs": self.entity_refs or [],
            "attributes": self.attributes or {},
            "priority": self.priority,
            "revision": self.revision,
            "injection_count": self.injection_count,
            "last_injected_at": to_utc_iso(self.last_injected_at),
            "cooldown_until": to_utc_iso(self.cooldown_until),
            "source_ref": self.source_ref,
            "contract_version": self.contract_version,
            **self._timestamps(),
        }


class RuntimeNarrativeCandidate(_NarrativeTimestampMixin, db.Model):
    __tablename__ = "runtime_narrative_candidates"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    idempotency_key = db.Column(
        db.String(180),
        nullable=False,
        unique=True,
    )
    candidate_type = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(
        db.String(24),
        nullable=False,
        default="pending",
        index=True,
    )
    normalized_payload = db.Column(db.JSON, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False, index=True)
    source_run_key = db.Column(db.String(160), nullable=True, index=True)
    source_stage_key = db.Column(db.String(100), nullable=True)
    contract_version = db.Column(db.String(32), nullable=False)
    decision_reason = db.Column(db.Text, nullable=True)
    decided_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    applied_changes = db.Column(db.JSON, nullable=False, default=list)

    def to_narrative_dict(self):
        return {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "candidate_type": self.candidate_type,
            "status": self.status,
            "normalized_payload": self.normalized_payload,
            "payload_hash": self.payload_hash,
            "source_run_key": self.source_run_key,
            "source_stage_key": self.source_stage_key,
            "contract_version": self.contract_version,
            "decision_reason": self.decision_reason,
            "decided_by_user_id": self.decided_by_user_id,
            "decided_at": to_utc_iso(self.decided_at),
            "applied_changes": self.applied_changes or [],
            **self._timestamps(),
        }


class RuntimeInjectionSnapshot(_NarrativeTimestampMixin, db.Model):
    __tablename__ = "runtime_injection_snapshots"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    snapshot_key = db.Column(db.String(180), nullable=False, unique=True)
    run_key = db.Column(db.String(160), nullable=False, index=True)
    contract_version = db.Column(db.String(32), nullable=False)
    context = db.Column(db.JSON, nullable=False)
    selected_records = db.Column(db.JSON, nullable=False)
    rendered_text = db.Column(db.Text, nullable=False, default="")
    content_hash = db.Column(db.String(64), nullable=False, index=True)

    def to_narrative_dict(self, *, include_rendered_text=True):
        data = {
            "id": self.id,
            "snapshot_key": self.snapshot_key,
            "run_key": self.run_key,
            "contract_version": self.contract_version,
            "context": self.context,
            "selected_records": self.selected_records,
            "content_hash": self.content_hash,
            **self._timestamps(),
        }
        if include_rendered_text:
            data["rendered_text"] = self.rendered_text
        return data


class RuntimeNarrativeRevision(db.Model):
    __tablename__ = "runtime_narrative_revisions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    record_type = db.Column(db.String(32), nullable=False, index=True)
    record_id = db.Column(db.Integer, nullable=False, index=True)
    revision = db.Column(db.Integer, nullable=False)
    previous_data = db.Column(db.JSON, nullable=True)
    new_data = db.Column(db.JSON, nullable=False)
    source_ref = db.Column(db.String(200), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "record_type",
            "record_id",
            "revision",
            name="uq_runtime_narrative_record_revision",
        ),
    )

    def to_narrative_dict(self):
        return {
            "id": self.id,
            "record_type": self.record_type,
            "record_id": self.record_id,
            "revision": self.revision,
            "previous_data": self.previous_data,
            "new_data": self.new_data,
            "source_ref": self.source_ref,
            "created_at": to_utc_iso(self.created_at),
        }


class RuntimeNarrativeLease(_NarrativeTimestampMixin, db.Model):
    __tablename__ = "runtime_narrative_leases"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    lease_key = db.Column(db.String(180), nullable=False, unique=True)
    owner_token = db.Column(db.String(120), nullable=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    revision = db.Column(db.Integer, nullable=False, default=0)

    def to_narrative_dict(self):
        return {
            "lease_key": self.lease_key,
            "held": bool(self.owner_token),
            "expires_at": to_utc_iso(self.expires_at),
            "revision": self.revision,
            **self._timestamps(),
        }
