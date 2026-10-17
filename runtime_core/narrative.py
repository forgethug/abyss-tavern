from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


NARRATIVE_CONTRACT_VERSION = "0.1"
VISIBILITY_LEVELS = frozenset({"public", "members", "admin", "secret"})
RECORD_STATUSES = frozenset(
    {"active", "dormant", "resolved", "archived"}
)
CANDIDATE_STATUSES = frozenset({"pending", "accepted", "rejected"})


class NarrativeContractError(ValueError):
    pass


class NarrativeConflictError(NarrativeContractError):
    pass


@dataclass(frozen=True)
class NarrativeContext:
    run_key: str
    scope_keys: tuple[str, ...] = ()
    query_terms: tuple[str, ...] = ()
    allowed_visibility: tuple[str, ...] = ("public", "members")
    entity_limit: int = 12
    fact_limit: int = 20
    thread_limit: int = 10
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "NarrativeContext":
        if not isinstance(payload, Mapping):
            raise NarrativeContractError("narrative context must be an object")
        run_key = str(payload.get("run_key") or "").strip()
        if not run_key or len(run_key) > 160:
            raise NarrativeContractError(
                "run_key must be a non-empty string up to 160 characters"
            )

        def string_tuple(key: str) -> tuple[str, ...]:
            raw = payload.get(key, [])
            if not isinstance(raw, list):
                raise NarrativeContractError(f"{key} must be an array")
            values = tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in raw
                    if isinstance(value, str) and str(value).strip()
                )
            )
            if len(values) != len(raw):
                raise NarrativeContractError(
                    f"{key} must contain unique non-empty strings"
                )
            return values

        allowed_visibility = string_tuple("allowed_visibility") or (
            "public",
            "members",
        )
        if not set(allowed_visibility).issubset(VISIBILITY_LEVELS):
            raise NarrativeContractError(
                "allowed_visibility contains unsupported values"
            )

        def bounded_limit(key: str, default: int) -> int:
            try:
                value = int(payload.get(key, default))
            except (TypeError, ValueError) as exc:
                raise NarrativeContractError(
                    f"{key} must be an integer"
                ) from exc
            if value < 1 or value > 100:
                raise NarrativeContractError(
                    f"{key} must be between 1 and 100"
                )
            return value

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise NarrativeContractError("metadata must be an object")
        return cls(
            run_key=run_key,
            scope_keys=string_tuple("scope_keys"),
            query_terms=string_tuple("query_terms"),
            allowed_visibility=allowed_visibility,
            entity_limit=bounded_limit("entity_limit", 12),
            fact_limit=bounded_limit("fact_limit", 20),
            thread_limit=bounded_limit("thread_limit", 10),
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict:
        return {
            "run_key": self.run_key,
            "scope_keys": list(self.scope_keys),
            "query_terms": list(self.query_terms),
            "allowed_visibility": list(self.allowed_visibility),
            "entity_limit": self.entity_limit,
            "fact_limit": self.fact_limit,
            "thread_limit": self.thread_limit,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class NarrativeBundle:
    entities: tuple[dict, ...]
    facts: tuple[dict, ...]
    story_threads: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {
            "entities": [dict(row) for row in self.entities],
            "facts": [dict(row) for row in self.facts],
            "story_threads": [
                dict(row)
                for row in self.story_threads
            ],
        }


@runtime_checkable
class NarrativePackPolicy(Protocol):
    pack_id: str
    contract_version: str

    def admin_descriptor(self) -> Mapping[str, Any]: ...

    def external_entities(
        self,
        context: NarrativeContext | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def external_facts(
        self,
        context: NarrativeContext | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def external_story_threads(
        self,
        context: NarrativeContext | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def normalize_candidate(
        self,
        candidate_type: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def apply_candidate(
        self,
        candidate_type: str,
        payload: Mapping[str, Any],
        repository: Any,
        source_ref: str,
    ) -> Sequence[Mapping[str, Any]]: ...

    def render_injection(
        self,
        context: NarrativeContext,
        bundle: NarrativeBundle,
    ) -> str: ...


class BaseNarrativePackPolicy:
    """Small default policy; concrete packs keep all narrative semantics."""

    pack_id = ""
    contract_version = NARRATIVE_CONTRACT_VERSION

    def admin_descriptor(self) -> Mapping[str, Any]:
        return {
            "contract_version": self.contract_version,
            "labels": {
                "entity": "世界实体",
                "fact": "世界事实",
                "story_thread": "故事线",
                "candidate": "维护候选",
                "snapshot": "注入快照",
            },
            "entity_kinds": [],
            "fact_kinds": [],
            "thread_kinds": [],
            "candidate_types": [],
            "supports_generic_writes": False,
            "custom_views": [],
        }

    def external_entities(self, context=None):
        return ()

    def external_facts(self, context=None):
        return ()

    def external_story_threads(self, context=None):
        return ()

    def normalize_candidate(self, candidate_type, payload):
        raise NarrativeContractError(
            f"Pack does not accept narrative candidates: {candidate_type}"
        )

    def apply_candidate(
        self,
        candidate_type,
        payload,
        repository,
        source_ref,
    ):
        raise NarrativeContractError(
            f"Pack does not apply narrative candidates: {candidate_type}"
        )

    def render_injection(
        self,
        context: NarrativeContext,
        bundle: NarrativeBundle,
    ) -> str:
        sections = []
        for label, records in (
            ("Entities", bundle.entities),
            ("Facts", bundle.facts),
            ("Story Threads", bundle.story_threads),
        ):
            if not records:
                continue
            lines = [f"[{label}]"]
            for record in records:
                title = str(
                    record.get("name")
                    or record.get("title")
                    or record.get("key")
                    or record.get("external_key")
                    or ""
                ).strip()
                summary = str(
                    record.get("summary")
                    or record.get("value")
                    or ""
                ).strip()
                lines.append(f"- {title}: {summary}".rstrip(": "))
            sections.append("\n".join(lines))
        return "\n\n".join(sections)


class NarrativePolicyRegistry:
    def __init__(self):
        self._factories = {}

    def register(self, pack_id: str, factory) -> None:
        normalized = str(pack_id or "").strip()
        if not normalized or not callable(factory):
            raise NarrativeContractError(
                "Narrative policy registration is invalid"
            )
        if normalized in self._factories:
            raise NarrativeContractError(
                f"Narrative policy already registered: {normalized}"
            )
        self._factories[normalized] = factory

    def create(self, pack_id: str, app) -> NarrativePackPolicy:
        factory = self._factories.get(str(pack_id or "").strip())
        if factory is None:
            raise NarrativeContractError(
                f"Narrative policy is not registered: {pack_id}"
            )
        policy = factory(app)
        if not isinstance(policy, NarrativePackPolicy):
            raise NarrativeContractError(
                f"Narrative policy does not satisfy the contract: {pack_id}"
            )
        if policy.pack_id != pack_id:
            raise NarrativeContractError(
                f"Narrative policy pack mismatch: {policy.pack_id}"
            )
        return policy
