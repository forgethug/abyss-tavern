from runtime_core.task_registry import (
    RuntimeTaskRegistry,
    dedupe_by_fields,
    global_dedupe_key,
)


class TestRuntimeTaskRegistry:
    def test_pack_registers_and_dispatches_its_own_task(self):
        registry = RuntimeTaskRegistry()
        consumed = []
        registry.register(
            "example.resolve",
            lambda payload: consumed.append(payload["operation_id"]),
            dedupe_key_builder=dedupe_by_fields(
                "example.resolve",
                "operation_id",
            ),
        )

        assert registry.supported_types() == {"example.resolve"}
        assert (
            registry.build_dedupe_key(
                "example.resolve",
                {"operation_id": 17},
            )
            == "example.resolve:17"
        )
        registry.dispatch("example.resolve", {"operation_id": 17})
        assert consumed == [17]

    def test_duplicate_task_type_is_rejected(self):
        registry = RuntimeTaskRegistry()
        registry.register("example.resolve", lambda _payload: None)

        try:
            registry.register("example.resolve", lambda _payload: None)
        except ValueError as exc:
            assert str(exc) == (
                "Runtime task type already registered: example.resolve"
            )
        else:
            raise AssertionError("duplicate task type must be rejected")

    def test_global_dedupe_key_is_explicit(self):
        registry = RuntimeTaskRegistry()
        registry.register(
            "example.maintenance",
            lambda _payload: None,
            dedupe_key_builder=global_dedupe_key("example.maintenance"),
        )

        assert (
            registry.build_dedupe_key("example.maintenance", {})
            == "example.maintenance:global"
        )
