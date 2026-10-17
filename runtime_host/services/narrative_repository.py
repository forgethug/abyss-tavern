from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from extensions import db
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from models.narrative import (
    RuntimeNarrativeCandidate,
    RuntimeNarrativeLease,
    RuntimeNarrativeRevision,
    RuntimeStoryThread,
    RuntimeWorldEntity,
    RuntimeWorldFact,
)
from runtime_core.narrative import (
    NarrativeConflictError,
    NarrativeContractError,
    RECORD_STATUSES,
    VISIBILITY_LEVELS,
)


class NarrativeRepository:
    _MODEL_BY_TYPE = {
        "entity": RuntimeWorldEntity,
        "fact": RuntimeWorldFact,
        "story_thread": RuntimeStoryThread,
    }

    @staticmethod
    def _required_text(
        payload: Mapping[str, Any],
        key: str,
        *,
        max_length: int,
    ) -> str:
        value = str(payload.get(key) or "").strip()
        if not value or len(value) > max_length:
            raise NarrativeContractError(
                f"{key} must be a non-empty string up to {max_length} characters"
            )
        return value

    @staticmethod
    def _optional_text(
        payload: Mapping[str, Any],
        key: str,
        *,
        max_length: int | None = None,
    ) -> str | None:
        raw = payload.get(key)
        if raw is None:
            return None
        value = str(raw).strip()
        if max_length is not None and len(value) > max_length:
            raise NarrativeContractError(
                f"{key} must be up to {max_length} characters"
            )
        return value or None

    @staticmethod
    def _string_list(
        payload: Mapping[str, Any],
        key: str,
        *,
        max_items: int = 50,
    ) -> list[str]:
        raw = payload.get(key, [])
        if not isinstance(raw, list):
            raise NarrativeContractError(f"{key} must be an array")
        values = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise NarrativeContractError(
                    f"{key} must contain non-empty strings"
                )
            value = item.strip()
            if value not in values:
                values.append(value)
        if len(values) > max_items:
            raise NarrativeContractError(
                f"{key} may contain at most {max_items} values"
            )
        return values

    @staticmethod
    def _status_visibility(payload):
        status = str(payload.get("status") or "active").strip()
        visibility = str(
            payload.get("visibility") or "members"
        ).strip()
        if status not in RECORD_STATUSES:
            raise NarrativeContractError(f"Unsupported status: {status}")
        if visibility not in VISIBILITY_LEVELS:
            raise NarrativeContractError(
                f"Unsupported visibility: {visibility}"
            )
        return status, visibility

    @staticmethod
    def _bounded_int(
        payload,
        key,
        default,
        *,
        minimum=0,
        maximum=100,
    ):
        try:
            value = int(payload.get(key, default))
        except (TypeError, ValueError) as exc:
            raise NarrativeContractError(f"{key} must be an integer") from exc
        if value < minimum or value > maximum:
            raise NarrativeContractError(
                f"{key} must be between {minimum} and {maximum}"
            )
        return value

    def list_entities(self):
        return (
            db.session.query(RuntimeWorldEntity)
            .order_by(
                RuntimeWorldEntity.importance.desc(),
                RuntimeWorldEntity.id.asc(),
            )
            .limit(500)
            .all()
        )

    def list_facts(self):
        return (
            db.session.query(RuntimeWorldFact)
            .order_by(RuntimeWorldFact.id.asc())
            .limit(500)
            .all()
        )

    def list_story_threads(self):
        return (
            db.session.query(RuntimeStoryThread)
            .order_by(
                RuntimeStoryThread.priority.desc(),
                RuntimeStoryThread.id.asc(),
            )
            .limit(500)
            .all()
        )

    def list_candidates(self, *, status=None):
        query = db.session.query(RuntimeNarrativeCandidate)
        if status:
            query = query.filter(
                RuntimeNarrativeCandidate.status == status
            )
        return query.order_by(
            RuntimeNarrativeCandidate.id.desc()
        ).limit(500).all()

    def get_candidate_for_update(self, candidate_id: int):
        return (
            db.session.query(RuntimeNarrativeCandidate)
            .filter(RuntimeNarrativeCandidate.id == candidate_id)
            .with_for_update()
            .one_or_none()
        )

    @staticmethod
    def claim_pending_candidate(
        candidate_id: int,
        *,
        transient_status: str,
    ) -> bool:
        updated = (
            db.session.query(RuntimeNarrativeCandidate)
            .filter(
                RuntimeNarrativeCandidate.id == candidate_id,
                RuntimeNarrativeCandidate.status == "pending",
            )
            .update(
                {
                    RuntimeNarrativeCandidate.status: transient_status,
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @staticmethod
    def get_candidate(candidate_id: int):
        return db.session.get(RuntimeNarrativeCandidate, candidate_id)

    @staticmethod
    def get_candidate_by_key(idempotency_key: str):
        return (
            db.session.query(RuntimeNarrativeCandidate)
            .filter_by(idempotency_key=idempotency_key)
            .one_or_none()
        )

    def _record_revision(
        self,
        record_type: str,
        record,
        *,
        previous_data,
        source_ref,
    ):
        db.session.flush()
        revision = RuntimeNarrativeRevision(
            record_type=record_type,
            record_id=record.id,
            revision=record.revision,
            previous_data=previous_data,
            new_data=record.to_narrative_dict(),
            source_ref=source_ref,
        )
        db.session.add(revision)
        db.session.flush()

    @staticmethod
    def _require_expected_revision(record, expected_revision):
        if expected_revision is None:
            raise NarrativeConflictError(
                "expected_revision is required when updating a record"
            )
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise NarrativeContractError(
                "expected_revision must be an integer"
            ) from exc
        if record.revision != expected:
            raise NarrativeConflictError(
                f"revision conflict: expected {expected}, "
                f"found {record.revision}"
            )

    @staticmethod
    def _require_create_revision(expected_revision):
        if expected_revision is None:
            return
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise NarrativeContractError(
                "expected_revision must be an integer"
            ) from exc
        if expected != 0:
            raise NarrativeConflictError(
                "revision conflict: a new record expects revision 0"
            )

    def upsert_entity(self, payload, *, source_ref=None):
        if not isinstance(payload, Mapping):
            raise NarrativeContractError("entity payload must be an object")
        allowed = {
            "external_key",
            "kind",
            "name",
            "aliases",
            "summary",
            "status",
            "visibility",
            "scope_keys",
            "attributes",
            "importance",
            "source_ref",
            "expected_revision",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise NarrativeContractError(
                f"Unsupported entity fields: {sorted(unknown)}"
            )
        external_key = self._required_text(
            payload,
            "external_key",
            max_length=160,
        )
        record = (
            db.session.query(RuntimeWorldEntity)
            .filter_by(external_key=external_key)
            .with_for_update()
            .one_or_none()
        )
        previous = None
        if record is None:
            self._require_create_revision(
                payload.get("expected_revision")
            )
            record = RuntimeWorldEntity(external_key=external_key)
            db.session.add(record)
        else:
            self._require_expected_revision(
                record,
                payload.get("expected_revision"),
            )
            previous = record.to_narrative_dict()
            record.revision += 1
        status, visibility = self._status_visibility(payload)
        attributes = payload.get("attributes", {})
        if not isinstance(attributes, dict):
            raise NarrativeContractError("attributes must be an object")
        record.kind = self._required_text(payload, "kind", max_length=64)
        record.name = self._required_text(payload, "name", max_length=160)
        record.aliases = self._string_list(payload, "aliases")
        record.summary = str(payload.get("summary") or "").strip()
        record.status = status
        record.visibility = visibility
        record.scope_keys = self._string_list(payload, "scope_keys")
        record.attributes = dict(attributes)
        record.importance = self._bounded_int(
            payload,
            "importance",
            50,
        )
        record.source_ref = (
            source_ref
            or self._optional_text(
                payload,
                "source_ref",
                max_length=200,
            )
        )
        self._record_revision(
            "entity",
            record,
            previous_data=previous,
            source_ref=record.source_ref,
        )
        return record

    def upsert_fact(self, payload, *, source_ref=None):
        if not isinstance(payload, Mapping):
            raise NarrativeContractError("fact payload must be an object")
        allowed = {
            "fact_key",
            "kind",
            "subject_key",
            "predicate",
            "value",
            "summary",
            "status",
            "visibility",
            "scope_keys",
            "source_ref",
            "expected_revision",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise NarrativeContractError(
                f"Unsupported fact fields: {sorted(unknown)}"
            )
        fact_key = self._required_text(
            payload,
            "fact_key",
            max_length=180,
        )
        record = (
            db.session.query(RuntimeWorldFact)
            .filter_by(fact_key=fact_key)
            .with_for_update()
            .one_or_none()
        )
        previous = None
        if record is None:
            self._require_create_revision(
                payload.get("expected_revision")
            )
            record = RuntimeWorldFact(fact_key=fact_key)
            db.session.add(record)
        else:
            self._require_expected_revision(
                record,
                payload.get("expected_revision"),
            )
            previous = record.to_narrative_dict()
            record.revision += 1
        status, visibility = self._status_visibility(payload)
        value = payload.get("value")
        if value is None:
            raise NarrativeContractError("fact value is required")
        record.kind = self._required_text(payload, "kind", max_length=64)
        record.subject_key = self._optional_text(
            payload,
            "subject_key",
            max_length=160,
        )
        record.predicate = self._required_text(
            payload,
            "predicate",
            max_length=120,
        )
        record.value = value
        record.summary = str(payload.get("summary") or "").strip()
        record.status = status
        record.visibility = visibility
        record.scope_keys = self._string_list(payload, "scope_keys")
        record.source_ref = (
            source_ref
            or self._optional_text(
                payload,
                "source_ref",
                max_length=200,
            )
        )
        self._record_revision(
            "fact",
            record,
            previous_data=previous,
            source_ref=record.source_ref,
        )
        return record

    def upsert_story_thread(self, payload, *, source_ref=None):
        if not isinstance(payload, Mapping):
            raise NarrativeContractError(
                "story thread payload must be an object"
            )
        allowed = {
            "external_key",
            "kind",
            "title",
            "summary",
            "next_hook",
            "status",
            "visibility",
            "scope_keys",
            "entity_refs",
            "attributes",
            "priority",
            "cooldown_until",
            "source_ref",
            "expected_revision",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise NarrativeContractError(
                f"Unsupported story thread fields: {sorted(unknown)}"
            )
        external_key = self._required_text(
            payload,
            "external_key",
            max_length=180,
        )
        record = (
            db.session.query(RuntimeStoryThread)
            .filter_by(external_key=external_key)
            .with_for_update()
            .one_or_none()
        )
        previous = None
        if record is None:
            self._require_create_revision(
                payload.get("expected_revision")
            )
            record = RuntimeStoryThread(external_key=external_key)
            db.session.add(record)
        else:
            self._require_expected_revision(
                record,
                payload.get("expected_revision"),
            )
            previous = record.to_narrative_dict()
            record.revision += 1
        status, visibility = self._status_visibility(payload)
        attributes = payload.get("attributes", {})
        if not isinstance(attributes, dict):
            raise NarrativeContractError("attributes must be an object")
        cooldown_until = payload.get("cooldown_until")
        if isinstance(cooldown_until, str) and cooldown_until:
            try:
                cooldown_until = datetime.fromisoformat(
                    cooldown_until.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except ValueError as exc:
                raise NarrativeContractError(
                    "cooldown_until must be ISO-8601"
                ) from exc
        elif cooldown_until is not None and not isinstance(
            cooldown_until,
            datetime,
        ):
            raise NarrativeContractError(
                "cooldown_until must be ISO-8601"
            )
        record.kind = self._required_text(payload, "kind", max_length=64)
        record.title = self._required_text(
            payload,
            "title",
            max_length=180,
        )
        record.summary = str(payload.get("summary") or "").strip()
        record.next_hook = self._optional_text(payload, "next_hook")
        record.status = status
        record.visibility = visibility
        record.scope_keys = self._string_list(payload, "scope_keys")
        record.entity_refs = self._string_list(
            payload,
            "entity_refs",
            max_items=100,
        )
        record.attributes = dict(attributes)
        record.priority = self._bounded_int(
            payload,
            "priority",
            50,
        )
        record.cooldown_until = cooldown_until
        record.source_ref = (
            source_ref
            or self._optional_text(
                payload,
                "source_ref",
                max_length=200,
            )
        )
        self._record_revision(
            "story_thread",
            record,
            previous_data=previous,
            source_ref=record.source_ref,
        )
        return record

    def apply_operations(
        self,
        operations: Iterable[Mapping[str, Any]],
        *,
        source_ref: str,
    ) -> list[dict]:
        applied = []
        for operation in operations:
            op = operation["op"]
            payload = operation["data"]
            if op == "upsert_entity":
                record = self.upsert_entity(
                    payload,
                    source_ref=source_ref,
                )
            elif op == "upsert_fact":
                record = self.upsert_fact(
                    payload,
                    source_ref=source_ref,
                )
            elif op == "upsert_story_thread":
                record = self.upsert_story_thread(
                    payload,
                    source_ref=source_ref,
                )
            else:
                raise NarrativeContractError(
                    f"Unsupported narrative operation: {op}"
                )
            applied.append(
                {
                    "op": op,
                    "record_type": record.to_narrative_dict()[
                        "record_type"
                    ],
                    "record_id": record.id,
                    "revision": record.revision,
                }
            )
        return applied

    @staticmethod
    def ensure_lease_row(lease_key: str):
        lease = (
            db.session.query(RuntimeNarrativeLease)
            .filter_by(lease_key=lease_key)
            .one_or_none()
        )
        if lease is None:
            try:
                with db.session.begin_nested():
                    lease = RuntimeNarrativeLease(lease_key=lease_key)
                    db.session.add(lease)
                    db.session.flush()
            except IntegrityError:
                lease = (
                    db.session.query(RuntimeNarrativeLease)
                    .filter_by(lease_key=lease_key)
                    .one()
                )
        return lease

    def claim_lease(
        self,
        lease_key: str,
        *,
        owner_token: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        self.ensure_lease_row(lease_key)
        updated = (
            db.session.query(RuntimeNarrativeLease)
            .filter(
                RuntimeNarrativeLease.lease_key == lease_key,
                or_(
                    RuntimeNarrativeLease.owner_token.is_(None),
                    RuntimeNarrativeLease.expires_at.is_(None),
                    RuntimeNarrativeLease.expires_at <= now,
                ),
            )
            .update(
                {
                    RuntimeNarrativeLease.owner_token: owner_token,
                    RuntimeNarrativeLease.expires_at: expires_at,
                    RuntimeNarrativeLease.revision: (
                        RuntimeNarrativeLease.revision + 1
                    ),
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    def release_lease(self, lease_key: str, owner_token: str) -> bool:
        lease = self.ensure_lease_row(lease_key)
        if not lease.owner_token:
            return True
        updated = (
            db.session.query(RuntimeNarrativeLease)
            .filter(
                RuntimeNarrativeLease.lease_key == lease_key,
                RuntimeNarrativeLease.owner_token == owner_token,
            )
            .update(
                {
                    RuntimeNarrativeLease.owner_token: None,
                    RuntimeNarrativeLease.expires_at: None,
                    RuntimeNarrativeLease.revision: (
                        RuntimeNarrativeLease.revision + 1
                    ),
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @staticmethod
    def revisions(record_type=None, record_id=None):
        query = db.session.query(RuntimeNarrativeRevision)
        if record_type:
            query = query.filter(
                RuntimeNarrativeRevision.record_type == record_type
            )
        if record_id is not None:
            query = query.filter(
                RuntimeNarrativeRevision.record_id == record_id
            )
        return query.order_by(
            RuntimeNarrativeRevision.id.desc()
        ).all()
