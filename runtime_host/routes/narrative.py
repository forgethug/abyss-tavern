from __future__ import annotations

from flask import Blueprint, jsonify, request

from extensions import db
from models.narrative import (
    RuntimeInjectionSnapshot,
)
from routes.auth import login_required
from runtime_core.narrative import (
    NarrativeConflictError,
    NarrativeContractError,
)
from utils.decorators import admin_required


def _response(data=None, *, message="success", status=200):
    return (
        jsonify({"code": status, "message": message, "data": data}),
        status,
    )


def _pagination():
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 50))
    except (TypeError, ValueError) as exc:
        raise NarrativeContractError(
            "page and page_size must be integers"
        ) from exc
    if page < 1 or page_size < 1 or page_size > 100:
        raise NarrativeContractError(
            "page must be positive and page_size must be between 1 and 100"
        )
    return page, page_size


def _paginate_rows(rows):
    page, page_size = _pagination()
    total = len(rows)
    start = (page - 1) * page_size
    return {
        "items": rows[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def create_narrative_blueprint(engine):
    blueprint = Blueprint(
        "runtime_narrative",
        __name__,
        url_prefix="/api/runtime/narrative",
    )

    @blueprint.errorhandler(NarrativeConflictError)
    def handle_conflict(exc):
        db.session.rollback()
        return _response(None, message=str(exc), status=409)

    @blueprint.errorhandler(NarrativeContractError)
    def handle_contract_error(exc):
        db.session.rollback()
        return _response(None, message=str(exc), status=400)

    @blueprint.get("/descriptor")
    @login_required
    @admin_required
    def descriptor():
        return _response(engine.descriptor())

    @blueprint.get("/overview")
    @login_required
    @admin_required
    def overview():
        entities = engine.list_entities()
        facts = engine.list_facts()
        threads = engine.list_story_threads()
        candidates = engine.repository.list_candidates()
        return _response(
            {
                "entity_count": len(entities),
                "fact_count": len(facts),
                "story_thread_count": len(threads),
                "pending_candidate_count": sum(
                    1
                    for candidate in candidates
                    if candidate.status == "pending"
                ),
                "shared_entity_count": sum(
                    1 for row in entities if row.get("source") == "shared"
                ),
                "shared_fact_count": sum(
                    1 for row in facts if row.get("source") == "shared"
                ),
                "shared_story_thread_count": sum(
                    1 for row in threads if row.get("source") == "shared"
                ),
            }
        )

    @blueprint.get("/entities")
    @login_required
    @admin_required
    def entities():
        return _response(_paginate_rows(engine.list_entities()))

    @blueprint.get("/facts")
    @login_required
    @admin_required
    def facts():
        return _response(_paginate_rows(engine.list_facts()))

    @blueprint.get("/story-threads")
    @login_required
    @admin_required
    def story_threads():
        return _response(_paginate_rows(engine.list_story_threads()))

    @blueprint.get("/candidates")
    @login_required
    @admin_required
    def candidates():
        status = str(request.args.get("status") or "").strip() or None
        if status not in {None, "pending", "accepted", "rejected"}:
            raise NarrativeContractError("Unsupported candidate status")
        rows = [
            row.to_narrative_dict()
            for row in engine.repository.list_candidates(status=status)
        ]
        return _response(_paginate_rows(rows))

    @blueprint.get("/snapshots")
    @login_required
    @admin_required
    def snapshots():
        rows = (
            db.session.query(RuntimeInjectionSnapshot)
            .order_by(RuntimeInjectionSnapshot.id.desc())
            .limit(500)
            .all()
        )
        return _response(
            _paginate_rows(
                [
                    row.to_narrative_dict(
                        include_rendered_text=False,
                    )
                    for row in rows
                ]
            )
        )

    @blueprint.get("/revisions")
    @login_required
    @admin_required
    def revisions():
        record_type = (
            str(request.args.get("record_type") or "").strip() or None
        )
        if record_type not in {None, "entity", "fact", "story_thread"}:
            raise NarrativeContractError("Unsupported record_type")
        record_id = request.args.get("record_id")
        if record_id is not None:
            try:
                record_id = int(record_id)
            except (TypeError, ValueError) as exc:
                raise NarrativeContractError(
                    "record_id must be an integer"
                ) from exc
        rows = [
            row.to_narrative_dict()
            for row in engine.repository.revisions(
                record_type=record_type,
                record_id=record_id,
            )[:500]
        ]
        return _response(_paginate_rows(rows))

    @blueprint.post("/preview")
    @login_required
    @admin_required
    def preview():
        payload = request.get_json(silent=True)
        return _response(engine.preview_injection(payload or {}))

    @blueprint.post("/snapshots")
    @login_required
    @admin_required
    def freeze_snapshot():
        try:
            snapshot, created = engine.freeze_injection(
                request.get_json(silent=True) or {}
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return _response(
            {
                "snapshot": snapshot.to_narrative_dict(),
                "created": created,
            },
            status=201 if created else 200,
        )

    @blueprint.post("/candidates")
    @login_required
    @admin_required
    def create_candidate():
        payload = request.get_json(silent=True) or {}
        try:
            candidate, created = engine.create_candidate(
                idempotency_key=payload.get("idempotency_key"),
                candidate_type=payload.get("candidate_type"),
                payload=payload.get("payload"),
                source_run_key=payload.get("source_run_key"),
                source_stage_key=payload.get("source_stage_key"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return _response(
            {
                "candidate": candidate.to_narrative_dict(),
                "created": created,
            },
            status=201 if created else 200,
        )

    @blueprint.post("/candidates/<int:candidate_id>/accept")
    @login_required
    @admin_required
    def accept_candidate(candidate_id):
        try:
            candidate, applied = engine.accept_candidate(
                candidate_id,
                actor_user_id=request.current_user.id,
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return _response(
            {
                "candidate": candidate.to_narrative_dict(),
                "applied": applied,
            }
        )

    @blueprint.post("/candidates/<int:candidate_id>/reject")
    @login_required
    @admin_required
    def reject_candidate(candidate_id):
        payload = request.get_json(silent=True) or {}
        try:
            candidate, changed = engine.reject_candidate(
                candidate_id,
                actor_user_id=request.current_user.id,
                reason=payload.get("reason"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return _response(
            {
                "candidate": candidate.to_narrative_dict(),
                "changed": changed,
            }
        )

    @blueprint.post("/leases/acquire")
    @login_required
    @admin_required
    def acquire_lease():
        payload = request.get_json(silent=True) or {}
        try:
            token = engine.acquire_lease(
                payload.get("lease_key"),
                ttl_seconds=payload.get("ttl_seconds", 120),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        if token is None:
            return _response(
                None,
                message="Narrative lease is already held",
                status=409,
            )
        return _response({"owner_token": token}, status=201)

    @blueprint.post("/leases/release")
    @login_required
    @admin_required
    def release_lease():
        payload = request.get_json(silent=True) or {}
        try:
            released = engine.release_lease(
                payload.get("lease_key"),
                payload.get("owner_token"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        if not released:
            return _response(
                None,
                message="Narrative lease token does not match",
                status=409,
            )
        return _response({"released": True})

    return blueprint
