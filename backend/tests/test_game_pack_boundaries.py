import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class TestGamePackBoundaries:
    def test_shared_runtime_core_has_no_game_pack_imports(self):
        violations = []
        for path in (REPOSITORY_ROOT / "runtime_core").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for token in (
                "plugins.adventurer",
                "s1_fantasy",
                "framework_smoke",
                "original_anomaly",
                "original-anomaly",
            ):
                if token in source:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}:{token}"
                    )
        assert violations == []

    def test_shared_runtime_host_has_no_pack_business_imports(self):
        forbidden = (
            "plugins.adventurer_s1",
            "plugins.adventurer",
            "s1_fantasy",
            "S1_RUNTIME_",
            "original_anomaly",
            "original-anomaly",
        )
        violations = []
        for path in (REPOSITORY_ROOT / "runtime_host").rglob("*.py"):
            if "tests" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in source:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}:{token}"
                    )
        assert violations == []

    def test_second_pack_does_not_import_s1(self):
        smoke_root = REPOSITORY_ROOT / "runtime_host" / "tests" / "fixtures" / "framework-smoke"
        violations = []
        for path in smoke_root.rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".ts",
                ".vue",
                ".json",
            }:
                continue
            source = path.read_text(encoding="utf-8")
            for token in ("adventurer_s1", "s1_fantasy", "s1-fantasy"):
                if token in source:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}:{token}"
                    )
        assert violations == []


    def test_frontend_runtime_host_has_no_pack_specific_imports(self):
        runtime_root = REPOSITORY_ROOT / "frontend-v2" / "src" / "runtime"
        violations = []
        for path in runtime_root.rglob("*.ts"):
            source = path.read_text(encoding="utf-8")
            for token in (
                "adventurer_s1",
                "framework_smoke",
                "original_anomaly",
                "original-anomaly",
            ):
                if token in source:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}:{token}"
                    )
        assert violations == []

    def test_s1_sync_cannot_write_shared_core_or_other_packs(self):
        sync_wrapper = (
            REPOSITORY_ROOT / "tools" / "sync-s1-source-snapshot.ps1"
        ).read_text(encoding="utf-8")
        assert 'sync_s1_portable_source.py' in sync_wrapper
        sync_script = (
            REPOSITORY_ROOT / "tools" / "sync_s1_portable_source.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(sync_script)
        scopes = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SCOPES"
                    for target in node.targets)
        )
        assert 'backend/plugins/adventurer_s1' in scopes
        assert 'frontend-v2/src/plugins/adventurer_s1' in scopes
        assert all('..' not in scope for scope in scopes)
        assert "runtime_core" not in sync_script
        assert "runtime_host" not in sync_script
        assert "framework-smoke" not in sync_script
        assert "original-anomaly" not in sync_script
        assert "original_anomaly" not in sync_script
