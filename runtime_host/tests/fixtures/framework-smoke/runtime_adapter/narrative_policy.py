from runtime_core.narrative import (
    BaseNarrativePackPolicy,
    NarrativeContractError,
)


class FrameworkSmokeNarrativePolicy(BaseNarrativePackPolicy):
    pack_id = "framework_smoke"

    def admin_descriptor(self):
        return {
            **super().admin_descriptor(),
            "labels": {
                "entity": "测试实体",
                "fact": "测试事实",
                "story_thread": "测试故事线",
                "candidate": "测试维护候选",
                "snapshot": "测试注入快照",
            },
            "entity_kinds": ["personnel", "anomaly", "department"],
            "fact_kinds": ["observation", "rule", "incident"],
            "thread_kinds": ["investigation", "containment", "rule_chain"],
            "candidate_types": ["world_update"],
            "supports_generic_writes": True,
        }

    @staticmethod
    def _project_operation(operation):
        if not isinstance(operation, dict):
            raise NarrativeContractError(
                "Every narrative operation must be an object"
            )
        op = str(operation.get("op") or "").strip()
        field_map = {
            "upsert_entity": {
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
                "expected_revision",
            },
            "upsert_fact": {
                "fact_key",
                "kind",
                "subject_key",
                "predicate",
                "value",
                "summary",
                "status",
                "visibility",
                "scope_keys",
                "expected_revision",
            },
            "upsert_story_thread": {
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
                "expected_revision",
            },
        }
        if op not in field_map:
            raise NarrativeContractError(
                f"Unsupported narrative operation: {op}"
            )
        data = operation.get("data")
        if not isinstance(data, dict):
            raise NarrativeContractError(
                "Narrative operation data must be an object"
            )
        return {
            "op": op,
            "data": {
                key: data[key]
                for key in field_map[op]
                if key in data
            },
        }

    def normalize_candidate(self, candidate_type, payload):
        if candidate_type != "world_update":
            raise NarrativeContractError(
                f"Unsupported candidate type: {candidate_type}"
            )
        operations = payload.get("operations")
        if not isinstance(operations, list) or not 1 <= len(operations) <= 50:
            raise NarrativeContractError(
                "world_update requires 1 to 50 operations"
            )
        return {
            "operations": [
                self._project_operation(operation)
                for operation in operations
            ]
        }

    @staticmethod
    def apply_candidate(
        candidate_type,
        payload,
        repository,
        source_ref,
    ):
        if candidate_type != "world_update":
            raise NarrativeContractError(
                f"Unsupported candidate type: {candidate_type}"
            )
        return repository.apply_operations(
            payload["operations"],
            source_ref=source_ref,
        )


def create_policy(_app):
    return FrameworkSmokeNarrativePolicy()
