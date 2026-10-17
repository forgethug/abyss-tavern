from __future__ import annotations
from runtime_core.time import utc_now

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from extensions import db
from models.narrative import (
    RuntimeInjectionSnapshot,
    RuntimeNarrativeCandidate,
)
from runtime_core.narrative import (
    NarrativeBundle,
    NarrativeConflictError,
    NarrativeContext,
    NarrativeContractError,
)
from services.narrative_repository import NarrativeRepository
from sqlalchemy.exc import IntegrityError


class WorldNarrativeEngine:
    def __init__(self, *, pack_id: str, policy, repository=None):
        if policy.pack_id != pack_id:
            raise NarrativeContractError(
                f"Narrative policy pack mismatch: {policy.pack_id}"
            )
        self.pack_id = pack_id
        self.policy = policy
        self.repository = repository or NarrativeRepository()

    @staticmethod
    def _canonical_hash(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _normalize_external(
        records: Iterable[Mapping[str, Any]],
        record_type: str,
    ) -> list[dict]:
        normalized = []
        for record in records:
            if not isinstance(record, Mapping):
                raise NarrativeContractError(
                    f"External {record_type} record must be an object"
                )
            row = dict(record)
            row["record_type"] = record_type
            row["source"] = "pack_compat"
            row.setdefault("visibility", "admin")
            row.setdefault("scope_keys", [])
            row.setdefault("status", "active")
            row.setdefault("revision", 1)
            normalized.append(row)
        return normalized

    def descriptor(self):
        raw_descriptor = self.policy.admin_descriptor()
        if not isinstance(raw_descriptor, Mapping):
            raise NarrativeContractError(
                "Narrative policy descriptor must be an object"
            )
        descriptor = dict(raw_descriptor)
        labels = descriptor.get("labels")
        if not isinstance(labels, Mapping):
            raise NarrativeContractError(
                "Narrative policy descriptor labels must be an object"
            )
        for label_key in (
            "entity",
            "fact",
            "story_thread",
            "candidate",
            "snapshot",
        ):
            if not isinstance(labels.get(label_key), str):
                raise NarrativeContractError(
                    f"Narrative policy label is invalid: {label_key}"
                )
        for key in (
            "entity_kinds",
            "fact_kinds",
            "thread_kinds",
            "candidate_types",
            "custom_views",
        ):
            descriptor.setdefault(key, [])
            if not isinstance(descriptor[key], list):
                raise NarrativeContractError(
                    f"Narrative policy descriptor {key} must be an array"
                )
        for key in (
            "entity_kinds",
            "fact_kinds",
            "thread_kinds",
            "candidate_types",
        ):
            if not all(
                isinstance(value, str) and value.strip()
                for value in descriptor[key]
            ):
                raise NarrativeContractError(
                    f"Narrative policy descriptor {key} is invalid"
                )
        for view in descriptor["custom_views"]:
            if (
                not isinstance(view, Mapping)
                or not isinstance(view.get("key"), str)
                or not isinstance(view.get("label"), str)
                or (
                    view.get("path") is not None
                    and not isinstance(view.get("path"), str)
                )
            ):
                raise NarrativeContractError(
                    "Narrative policy custom view is invalid"
                )
        descriptor.setdefault("supports_generic_writes", False)
        if not isinstance(
            descriptor["supports_generic_writes"],
            bool,
        ):
            raise NarrativeContractError(
                "Narrative policy supports_generic_writes must be boolean"
            )
        descriptor.update(
            {
                "pack_id": self.pack_id,
                "contract_version": self.policy.contract_version,
            }
        )
        return descriptor

    def list_entities(self, context=None):
        shared = [
            record.to_narrative_dict()
            for record in self.repository.list_entities()
        ]
        external = self._normalize_external(
            self.policy.external_entities(context),
            "entity",
        )
        return shared + external

    def list_facts(self, context=None):
        shared = [
            record.to_narrative_dict()
            for record in self.repository.list_facts()
        ]
        external = self._normalize_external(
            self.policy.external_facts(context),
            "fact",
        )
        return shared + external

    def list_story_threads(self, context=None):
        shared = [
            record.to_narrative_dict()
            for record in self.repository.list_story_threads()
        ]
        external = self._normalize_external(
            self.policy.external_story_threads(context),
            "story_thread",
        )
        return shared + external

    @staticmethod
    def _eligible(record, context: NarrativeContext):
        if record.get("status", "active") != "active":
            return False
        if record.get("visibility", "members") not in set(
            context.allowed_visibility
        ):
            return False
        scope_keys = {
            str(value)
            for value in record.get("scope_keys", [])
            if str(value)
        }
        if scope_keys and not scope_keys.intersection(context.scope_keys):
            return False
        cooldown = record.get("cooldown_until")
        if isinstance(cooldown, str) and cooldown:
            try:
                if datetime.fromisoformat(
                    cooldown.replace("Z", "+00:00")
                ).replace(tzinfo=None) > utc_now():
                    return False
            except ValueError:
                return False
        return True

    @staticmethod
    def _score(record, context: NarrativeContext):
        base = int(
            record.get("importance")
            or record.get("priority")
            or 50
        )
        terms = {
            term.casefold()
            for term in context.query_terms
            if term
        }
        if not terms:
            return base
        searchable = " ".join(
            str(record.get(key) or "")
            for key in (
                "name",
                "title",
                "summary",
                "kind",
                "external_key",
                "key",
            )
        ).casefold()
        exact_matches = sum(1 for term in terms if term in searchable)
        return base + exact_matches * 10

    def build_bundle(self, context: NarrativeContext) -> NarrativeBundle:
        def select(rows, limit):
            eligible = [
                row
                for row in rows
                if self._eligible(row, context)
            ]
            eligible.sort(
                key=lambda row: (
                    -self._score(row, context),
                    str(
                        row.get("external_key")
                        or row.get("key")
                        or row.get("id")
                        or ""
                    ),
                )
            )
            return tuple(eligible[:limit])

        return NarrativeBundle(
            entities=select(
                self.list_entities(context),
                context.entity_limit,
            ),
            facts=select(
                self.list_facts(context),
                context.fact_limit,
            ),
            story_threads=select(
                self.list_story_threads(context),
                context.thread_limit,
            ),
        )

    def preview_injection(self, context_payload):
        context = NarrativeContext.from_mapping(context_payload)
        bundle = self.build_bundle(context)
        rendered = self.policy.render_injection(context, bundle)
        if not isinstance(rendered, str):
            raise NarrativeContractError(
                "Narrative policy render_injection must return text"
            )
        return {
            "context": context.to_dict(),
            "bundle": bundle.to_dict(),
            "rendered_text": rendered,
            "content_hash": self._canonical_hash(
                {
                    "context": context.to_dict(),
                    "bundle": bundle.to_dict(),
                    "rendered_text": rendered,
                }
            ),
        }

    def freeze_injection(self, context_payload):
        context = NarrativeContext.from_mapping(context_payload)
        snapshot_key = (
            f"{self.pack_id}:{self.policy.contract_version}:"
            f"{self._canonical_hash(context.run_key)[:32]}"
        )
        existing = (
            db.session.query(RuntimeInjectionSnapshot)
            .filter_by(snapshot_key=snapshot_key)
            .one_or_none()
        )
        if existing is not None:
            if existing.context != context.to_dict():
                raise NarrativeConflictError(
                    "Frozen injection run_key was reused with a different context"
                )
            return existing, False
        preview = self.preview_injection(context_payload)
        snapshot = RuntimeInjectionSnapshot(
            snapshot_key=snapshot_key,
            run_key=context.run_key,
            contract_version=self.policy.contract_version,
            context=preview["context"],
            selected_records=preview["bundle"],
            rendered_text=preview["rendered_text"],
            content_hash=preview["content_hash"],
        )
        try:
            with db.session.begin_nested():
                db.session.add(snapshot)
                db.session.flush()
        except IntegrityError:
            existing = (
                db.session.query(RuntimeInjectionSnapshot)
                .filter_by(snapshot_key=snapshot_key)
                .one()
            )
            if existing.context != context.to_dict():
                raise NarrativeConflictError(
                    "Frozen injection run_key was reused with a different context"
                )
            return existing, False
        now = utc_now()
        selected_thread_ids = [
            row.get("id")
            for row in preview["bundle"]["story_threads"]
            if (
                row.get("source") == "shared"
                and isinstance(row.get("id"), int)
            )
        ]
        if selected_thread_ids:
            from models.narrative import RuntimeStoryThread

            (
                db.session.query(RuntimeStoryThread)
                .filter(RuntimeStoryThread.id.in_(selected_thread_ids))
                .update(
                    {
                        RuntimeStoryThread.injection_count: (
                            RuntimeStoryThread.injection_count + 1
                        ),
                        RuntimeStoryThread.last_injected_at: now,
                    },
                    synchronize_session=False,
                )
            )
        return snapshot, True

    def create_candidate(
        self,
        *,
        idempotency_key,
        candidate_type,
        payload,
        source_run_key=None,
        source_stage_key=None,
    ):
        key = str(idempotency_key or "").strip()
        candidate_type = str(candidate_type or "").strip()
        if not key or len(key) > 180:
            raise NarrativeContractError(
                "idempotency_key must be a non-empty string up to 180 characters"
            )
        if not candidate_type or len(candidate_type) > 64:
            raise NarrativeContractError(
                "candidate_type must be a non-empty identifier"
            )
        if not isinstance(payload, Mapping):
            raise NarrativeContractError(
                "candidate payload must be an object"
            )
        source_run_key = str(source_run_key or "").strip() or None
        source_stage_key = str(source_stage_key or "").strip() or None
        if source_run_key is not None and len(source_run_key) > 160:
            raise NarrativeContractError(
                "source_run_key must be up to 160 characters"
            )
        if source_stage_key is not None and len(source_stage_key) > 100:
            raise NarrativeContractError(
                "source_stage_key must be up to 100 characters"
            )
        normalized_payload = self.policy.normalize_candidate(
            candidate_type,
            payload,
        )
        if not isinstance(normalized_payload, Mapping):
            raise NarrativeContractError(
                "Narrative policy candidate projection must be an object"
            )
        normalized = dict(normalized_payload)
        payload_hash = self._canonical_hash(normalized)
        existing = self.repository.get_candidate_by_key(key)
        if existing is not None:
            if (
                existing.candidate_type != candidate_type
                or existing.payload_hash != payload_hash
            ):
                raise NarrativeConflictError(
                    "Candidate idempotency key was reused with different input"
                )
            return existing, False
        candidate = RuntimeNarrativeCandidate(
            idempotency_key=key,
            candidate_type=candidate_type,
            normalized_payload=normalized,
            payload_hash=payload_hash,
            source_run_key=source_run_key,
            source_stage_key=source_stage_key,
            contract_version=self.policy.contract_version,
        )
        try:
            with db.session.begin_nested():
                db.session.add(candidate)
                db.session.flush()
        except IntegrityError:
            existing = self.repository.get_candidate_by_key(key)
            if existing is None:
                raise
            if (
                existing.candidate_type != candidate_type
                or existing.payload_hash != payload_hash
            ):
                raise NarrativeConflictError(
                    "Candidate idempotency key was reused with different input"
                )
            return existing, False
        return candidate, True

    def accept_candidate(self, candidate_id, *, actor_user_id):
        candidate = self.repository.get_candidate_for_update(candidate_id)
        if candidate is None:
            raise NarrativeContractError("Narrative candidate not found")
        if candidate.status == "accepted":
            return candidate, False
        if candidate.status != "pending":
            raise NarrativeConflictError(
                f"Candidate is already {candidate.status}"
            )
        if not self.repository.claim_pending_candidate(
            candidate.id,
            transient_status="applying",
        ):
            db.session.expire_all()
            candidate = self.repository.get_candidate(candidate_id)
            if candidate is not None and candidate.status == "accepted":
                return candidate, False
            raise NarrativeConflictError(
                "Candidate decision is already in progress"
            )
        candidate.status = "applying"
        source_ref = f"candidate:{candidate.id}"
        applied_changes = self.policy.apply_candidate(
            candidate.candidate_type,
            candidate.normalized_payload,
            self.repository,
            source_ref,
        )
        if not isinstance(applied_changes, (list, tuple)):
            raise NarrativeContractError(
                "Narrative policy apply_candidate must return an array"
            )
        changes = []
        for change in applied_changes:
            if not isinstance(change, Mapping):
                raise NarrativeContractError(
                    "Applied narrative changes must be objects"
                )
            changes.append(dict(change))
        candidate.status = "accepted"
        candidate.decided_by_user_id = actor_user_id
        candidate.decided_at = utc_now()
        candidate.applied_changes = changes
        candidate.decision_reason = "accepted"
        db.session.flush()
        return candidate, True

    def reject_candidate(
        self,
        candidate_id,
        *,
        actor_user_id,
        reason,
    ):
        candidate = self.repository.get_candidate_for_update(candidate_id)
        if candidate is None:
            raise NarrativeContractError("Narrative candidate not found")
        if candidate.status == "rejected":
            return candidate, False
        if candidate.status != "pending":
            raise NarrativeConflictError(
                f"Candidate is already {candidate.status}"
            )
        reason = str(reason or "").strip()
        if not reason or len(reason) > 1000:
            raise NarrativeContractError(
                "Rejection reason is required and must be up to 1000 characters"
            )
        if not self.repository.claim_pending_candidate(
            candidate.id,
            transient_status="rejecting",
        ):
            db.session.expire_all()
            candidate = self.repository.get_candidate(candidate_id)
            if candidate is not None and candidate.status == "rejected":
                return candidate, False
            raise NarrativeConflictError(
                "Candidate decision is already in progress"
            )
        candidate.status = "rejecting"
        candidate.status = "rejected"
        candidate.decided_by_user_id = actor_user_id
        candidate.decided_at = utc_now()
        candidate.decision_reason = reason
        db.session.flush()
        return candidate, True

    def acquire_lease(self, lease_key, *, ttl_seconds=120):
        key = str(lease_key or "").strip()
        if not key or len(key) > 180:
            raise NarrativeContractError("Invalid narrative lease key")
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise NarrativeContractError(
                "ttl_seconds must be an integer"
            ) from exc
        if ttl < 1 or ttl > 3600:
            raise NarrativeContractError(
                "ttl_seconds must be between 1 and 3600"
            )
        now = utc_now()
        token = secrets.token_urlsafe(32)
        claimed = self.repository.claim_lease(
            key,
            owner_token=token,
            expires_at=now + timedelta(seconds=ttl),
            now=now,
        )
        return token if claimed else None

    def release_lease(self, lease_key, owner_token):
        key = str(lease_key or "").strip()
        if not key or len(key) > 180:
            raise NarrativeContractError("Invalid narrative lease key")
        token = str(owner_token or "")
        if not token:
            return False
        return self.repository.release_lease(key, token)
