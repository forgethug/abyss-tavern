"""Read-only sizing preview for possible S1 expedition-log snapshot deduplication."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import copy
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from flask import current_app

from db_repository.s1_expedition_log_dedup_repository import list_preview_logs
from plugins.adventurer_s1.cold_storage import estimate_json_storage_bytes
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from utils.time_utils import to_utc_iso


logger = logging.getLogger(__name__)


class S1ExpeditionLogDedupPreviewService:
    """Compare lossless reference layouts without changing persisted logs."""

    SNAPSHOT_OPERATION = "s1_expedition_log_dedup_preview_v1"
    ASSET_KINDS = ("traits", "equipment", "skills")
    STATIC_FIELDS = {
        "traits": ("name", "description", "description_ai"),
        "equipment": ("name", "type", "rarity", "custom_desc", "description"),
        "skills": ("name", "description"),
    }

    @staticmethod
    def _config_int(key: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(current_app.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return min(maximum, max(minimum, value))

    @staticmethod
    def _config_ratio(key: str, default: float) -> float:
        try:
            value = float(current_app.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return min(1.0, max(0.0, value))

    @staticmethod
    def _canonical_bytes(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def _digest(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical_bytes(value)).hexdigest()

    @classmethod
    def _asset_payload(cls, kind: str, asset: dict, mode: str) -> dict:
        if mode == "whole":
            return copy.deepcopy(asset)
        return {
            field: copy.deepcopy(asset[field])
            for field in cls.STATIC_FIELDS[kind]
            if field in asset
        }

    @classmethod
    def _content_key(cls, kind: str, payload: dict) -> str:
        return cls._digest({"kind": kind, "payload": payload})

    @classmethod
    def _collect_catalog(cls, rows: list[dict], mode: str) -> tuple[dict, dict, dict]:
        catalog: dict[str, dict] = {}
        by_kind = {
            kind: {
                "reference_count": 0,
                "unique_content_count": 0,
                "duplicate_reference_count": 0,
                "payload_raw_bytes": 0,
                "unique_payload_raw_bytes": 0,
            }
            for kind in cls.ASSET_KINDS
        }
        malformed = {
            "non_object_full_log_count": 0,
            "missing_snapshot_count": 0,
            "non_list_snapshot_count": 0,
            "non_object_member_count": 0,
            "missing_asset_collection_count": 0,
            "non_list_asset_collection_count": 0,
            "non_object_asset_count": 0,
        }

        for row in rows:
            full_log = row.get("full_log")
            if not isinstance(full_log, dict):
                malformed["non_object_full_log_count"] += 1
                continue
            if "audit_member_snapshot" not in full_log:
                malformed["missing_snapshot_count"] += 1
                continue
            members = full_log.get("audit_member_snapshot")
            if not isinstance(members, list):
                malformed["non_list_snapshot_count"] += 1
                continue
            for member in members:
                if not isinstance(member, dict):
                    malformed["non_object_member_count"] += 1
                    continue
                for kind in cls.ASSET_KINDS:
                    if kind not in member:
                        malformed["missing_asset_collection_count"] += 1
                        continue
                    assets = member.get(kind)
                    if not isinstance(assets, list):
                        malformed["non_list_asset_collection_count"] += 1
                        continue
                    for asset in assets:
                        if not isinstance(asset, dict):
                            malformed["non_object_asset_count"] += 1
                            continue
                        payload = cls._asset_payload(kind, asset, mode)
                        canonical = cls._canonical_bytes(payload)
                        key = cls._content_key(kind, payload)
                        current = catalog.get(key)
                        if current is None:
                            raw_storage = estimate_json_storage_bytes(payload, cold=False)
                            cold_storage = estimate_json_storage_bytes(payload, cold=True)
                            current = {
                                "kind": kind,
                                "payload": payload,
                                "canonical": canonical,
                                "occurrences": 0,
                                "payload_raw_bytes": len(canonical),
                                "best_payload_storage_bytes": min(raw_storage, cold_storage),
                                "preferred_storage": (
                                    "cold_zlib_envelope"
                                    if cold_storage < raw_storage
                                    else "plain_json"
                                ),
                            }
                            catalog[key] = current
                        elif current["canonical"] != canonical or current["kind"] != kind:
                            raise ValueError("expedition log dedup content digest collision")
                        current["occurrences"] += 1
                        by_kind[kind]["reference_count"] += 1
                        by_kind[kind]["payload_raw_bytes"] += len(canonical)

        for item in catalog.values():
            kind = item["kind"]
            by_kind[kind]["unique_content_count"] += 1
            by_kind[kind]["unique_payload_raw_bytes"] += item["payload_raw_bytes"]
        for summary in by_kind.values():
            summary["duplicate_reference_count"] = (
                summary["reference_count"] - summary["unique_content_count"]
            )
        return catalog, by_kind, malformed

    @classmethod
    def _transform_logs(
        cls,
        rows: list[dict],
        *,
        mode: str,
        selected_keys: set[str],
        ref_ids: dict[str, int],
    ) -> list[dict]:
        transformed = []
        for row in rows:
            source_full_log = row.get("full_log")
            full_log = copy.deepcopy(source_full_log)
            if not isinstance(full_log, dict):
                transformed.append(full_log)
                continue
            members = full_log.get("audit_member_snapshot")
            if isinstance(members, list):
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    for kind in cls.ASSET_KINDS:
                        assets = member.get(kind)
                        if not isinstance(assets, list):
                            continue
                        rewritten = []
                        for asset in assets:
                            if not isinstance(asset, dict):
                                rewritten.append(asset)
                                continue
                            payload = cls._asset_payload(kind, asset, mode)
                            key = cls._content_key(kind, payload)
                            if key not in selected_keys:
                                rewritten.append(asset)
                                continue
                            if mode == "whole":
                                reference = {"snapshot_ref": ref_ids[key]}
                                if "asset_id" in asset:
                                    reference["asset_id"] = asset.get("asset_id")
                                rewritten.append(reference)
                            else:
                                dynamic = copy.deepcopy(asset)
                                for field in cls.STATIC_FIELDS[kind]:
                                    dynamic.pop(field, None)
                                dynamic["content_ref"] = ref_ids[key]
                                rewritten.append(dynamic)
                        member[kind] = rewritten
            transformed.append(full_log)
        return transformed

    @classmethod
    def _build_strategy(
        cls,
        rows: list[dict],
        *,
        name: str,
        mode: str,
        catalog: dict[str, dict],
        by_kind: dict,
        selected_keys: set[str],
        current_storage_bytes: int,
        blob_overhead_bytes: int,
        min_saving_ratio: float,
    ) -> dict:
        ref_ids = {key: index for index, key in enumerate(sorted(selected_keys), start=1)}
        transformed = cls._transform_logs(
            rows,
            mode=mode,
            selected_keys=selected_keys,
            ref_ids=ref_ids,
        )
        rewritten_log_bytes = sum(
            estimate_json_storage_bytes(
                full_log,
                cold=rows[index].get("cold_compressed_at") is not None,
                expedition_log=True,
            )
            for index, full_log in enumerate(transformed)
        )
        blob_payload_bytes = sum(
            int(catalog[key]["best_payload_storage_bytes"])
            for key in selected_keys
        )
        estimated_blob_overhead_bytes = len(selected_keys) * int(blob_overhead_bytes)
        projected = rewritten_log_bytes + blob_payload_bytes + estimated_blob_overhead_bytes
        saving = current_storage_bytes - projected
        saving_ratio = saving / current_storage_bytes if current_storage_bytes else 0.0
        selected_reference_count = sum(
            int(catalog[key]["occurrences"])
            for key in selected_keys
        )
        selected_by_kind = {}
        for kind in cls.ASSET_KINDS:
            keys = [key for key in selected_keys if catalog[key]["kind"] == kind]
            references = sum(int(catalog[key]["occurrences"]) for key in keys)
            selected_by_kind[kind] = {
                **by_kind[kind],
                "externalized_content_count": len(keys),
                "externalized_reference_count": references,
                "externalized_duplicate_reference_count": references - len(keys),
            }
        return {
            "name": name,
            "mode": mode,
            "content_selection": (
                "all" if name != "adaptive_static_content" else "positive_raw_gain_only"
            ),
            "rewritten_log_storage_bytes": rewritten_log_bytes,
            "blob_payload_storage_bytes": blob_payload_bytes,
            "estimated_blob_record_and_index_overhead_bytes": estimated_blob_overhead_bytes,
            "projected_total_storage_bytes": projected,
            "estimated_saving_bytes": saving,
            "estimated_saving_ratio": round(saving_ratio, 6),
            "externalized_content_count": len(selected_keys),
            "externalized_reference_count": selected_reference_count,
            "externalized_duplicate_reference_count": (
                selected_reference_count - len(selected_keys)
            ),
            "recommended_for_this_batch": (
                saving > 0 and saving_ratio >= min_saving_ratio
            ),
            "by_kind": selected_by_kind,
        }

    @classmethod
    def _adaptive_static_keys(
        cls,
        catalog: dict[str, dict],
        *,
        blob_overhead_bytes: int,
    ) -> set[str]:
        selected = set()
        for key, item in catalog.items():
            occurrences = int(item["occurrences"])
            if occurrences < 2:
                continue
            reference_bytes = estimate_json_storage_bytes(
                {"content_ref": 9_999_999_999},
                cold=False,
            )
            inline_bytes = occurrences * int(item["payload_raw_bytes"])
            externalized_bytes = (
                int(item["best_payload_storage_bytes"])
                + int(blob_overhead_bytes)
                + occurrences * reference_bytes
            )
            if inline_bytes > externalized_bytes:
                selected.add(key)
        return selected

    @classmethod
    def _analyze(
        cls,
        rows: list[dict],
        *,
        blob_overhead_bytes: int,
        min_saving_ratio: float,
    ) -> dict:
        current_storage_bytes = sum(
            estimate_json_storage_bytes(
                row.get("full_log") or {},
                cold=row.get("cold_compressed_at") is not None,
                expedition_log=True,
            )
            for row in rows
        )
        current_raw_bytes = sum(
            estimate_json_storage_bytes(row.get("full_log") or {}, cold=False)
            for row in rows
        )
        audit_snapshot_raw_bytes = sum(
            estimate_json_storage_bytes(
                (
                    row["full_log"].get("audit_member_snapshot") or []
                    if isinstance(row.get("full_log"), dict)
                    else []
                ),
                cold=False,
            )
            for row in rows
        )

        whole_catalog, whole_by_kind, malformed = cls._collect_catalog(rows, "whole")
        static_catalog, static_by_kind, static_malformed = cls._collect_catalog(rows, "static")
        for key, value in static_malformed.items():
            malformed[key] = max(malformed[key], value)

        strategies = [
            cls._build_strategy(
                rows,
                name="whole_asset_versions",
                mode="whole",
                catalog=whole_catalog,
                by_kind=whole_by_kind,
                selected_keys=set(whole_catalog),
                current_storage_bytes=current_storage_bytes,
                blob_overhead_bytes=blob_overhead_bytes,
                min_saving_ratio=min_saving_ratio,
            ),
            cls._build_strategy(
                rows,
                name="all_static_content",
                mode="static",
                catalog=static_catalog,
                by_kind=static_by_kind,
                selected_keys=set(static_catalog),
                current_storage_bytes=current_storage_bytes,
                blob_overhead_bytes=blob_overhead_bytes,
                min_saving_ratio=min_saving_ratio,
            ),
        ]
        adaptive_keys = cls._adaptive_static_keys(
            static_catalog,
            blob_overhead_bytes=blob_overhead_bytes,
        )
        strategies.append(cls._build_strategy(
            rows,
            name="adaptive_static_content",
            mode="static",
            catalog=static_catalog,
            by_kind=static_by_kind,
            selected_keys=adaptive_keys,
            current_storage_bytes=current_storage_bytes,
            blob_overhead_bytes=blob_overhead_bytes,
            min_saving_ratio=min_saving_ratio,
        ))
        unsupported_shape_count = sum(
            int(value)
            for key, value in malformed.items()
            if key not in {"missing_snapshot_count", "missing_asset_collection_count"}
        )
        if unsupported_shape_count:
            for strategy in strategies:
                strategy["recommended_for_this_batch"] = False
        best = (
            max(strategies, key=lambda item: int(item["estimated_saving_bytes"]))
            if strategies else None
        )
        if not rows:
            decision = "no_data"
            reason_codes = ["empty_page"]
        elif unsupported_shape_count:
            decision = "blocked"
            reason_codes = ["unsupported_snapshot_shape"]
        elif not best or int(best["estimated_saving_bytes"]) <= 0:
            decision = "do_not_execute"
            reason_codes = ["non_positive_estimated_saving"]
        elif not best["recommended_for_this_batch"]:
            decision = "insufficient_margin"
            reason_codes = ["saving_below_safety_ratio"]
        else:
            decision = "candidate"
            reason_codes = ["positive_saving_with_safety_margin"]
        return {
            "current": {
                "log_count": len(rows),
                "full_log_raw_json_bytes": current_raw_bytes,
                "audit_member_snapshot_raw_json_bytes": audit_snapshot_raw_bytes,
                "current_logical_storage_bytes": current_storage_bytes,
                "cold_log_count": sum(
                    1 for row in rows if row.get("cold_compressed_at") is not None
                ),
            },
            "strategies": strategies,
            "best_strategy": best["name"] if best else None,
            "best_estimated_saving_bytes": int(best["estimated_saving_bytes"]) if best else 0,
            "best_estimated_saving_ratio": float(best["estimated_saving_ratio"]) if best else 0.0,
            "recommended_for_this_batch": bool(
                best and best["recommended_for_this_batch"] and not unsupported_shape_count
            ),
            "decision": decision,
            "reason_codes": reason_codes,
            "unsupported_shape_count": unsupported_shape_count,
            "malformed": malformed,
        }

    @classmethod
    def preview(
        cls,
        *,
        admin_user_id: int,
        before_log_id: object = None,
        limit: object = None,
        cold_only: bool = True,
    ) -> tuple[dict | None, str | None]:
        max_limit = cls._config_int(
            "ADVENTURER_S1_LOG_DEDUP_PREVIEW_MAX_LOGS",
            100,
            minimum=1,
            maximum=500,
        )
        default_limit = cls._config_int(
            "ADVENTURER_S1_LOG_DEDUP_PREVIEW_DEFAULT_LOGS",
            40,
            minimum=1,
            maximum=max_limit,
        )
        try:
            parsed_limit = default_limit if limit in (None, "") else int(limit)
        except (TypeError, ValueError):
            return None, "limit 必须是正整数"
        if parsed_limit <= 0 or parsed_limit > max_limit:
            return None, f"limit 必须在 1 到 {max_limit} 之间"

        parsed_before = None
        if before_log_id not in (None, ""):
            try:
                parsed_before = int(before_log_id)
            except (TypeError, ValueError):
                return None, "before_log_id 必须是正整数"
            if parsed_before <= 0:
                return None, "before_log_id 必须是正整数"

        blob_overhead_bytes = cls._config_int(
            "ADVENTURER_S1_LOG_DEDUP_ESTIMATED_BLOB_OVERHEAD_BYTES",
            192,
            minimum=0,
            maximum=4096,
        )
        min_saving_ratio = cls._config_ratio(
            "ADVENTURER_S1_LOG_DEDUP_MIN_RECOMMENDED_SAVING_RATIO",
            0.10,
        )
        try:
            rows, has_more = list_preview_logs(
                before_log_id=parsed_before,
                limit=parsed_limit,
                cold_only=bool(cold_only),
            )
            analysis = cls._analyze(
                rows,
                blob_overhead_bytes=blob_overhead_bytes,
                min_saving_ratio=min_saving_ratio,
            )
        except Exception:  # noqa: BLE001
            logger.exception("S1 expedition log dedup preview failed")
            return None, "预览失败：服务端估算异常，请查看日志"

        log_states = [
            {
                "id": int(row["id"]),
                "updated_at": to_utc_iso(row.get("updated_at")) if row.get("updated_at") else None,
                "full_log_digest": cls._digest(row.get("full_log") or {}),
            }
            for row in rows
        ]
        scope = {
            "before_log_id": parsed_before,
            "limit": parsed_limit,
            "cold_only": bool(cold_only),
            "statuses": ["completed", "failed"],
            "first_log_id": int(rows[0]["id"]) if rows else None,
            "last_log_id": int(rows[-1]["id"]) if rows else None,
            "next_before_log_id": int(rows[-1]["id"]) if rows and has_more else None,
            "has_more": bool(has_more),
            "created_at_from": (
                to_utc_iso(min(row["created_at"] for row in rows)) if rows else None
            ),
            "created_at_to": (
                to_utc_iso(max(row["created_at"] for row in rows)) if rows else None
            ),
        }
        result = {
            "preview_version": 1,
            "generated_at": _portable_utc_now().isoformat() + "Z",
            "mode": "read_only_decision_preview",
            "execution_available": False,
            "scope": scope,
            "estimate_basis": {
                "codec": "zlib_level_9_base64_json_envelope",
                "physical_postgresql_bytes_measured": False,
                "blob_record_and_index_overhead_bytes": blob_overhead_bytes,
                "minimum_recommended_saving_ratio": min_saving_ratio,
                "notes": [
                    "结果只覆盖本页日志，不能外推为全库物理节省量",
                    "未计量 WAL、死元组、TOAST 页布局和 VACUUM 后文件回收",
                    "后续执行必须重新锁定并核对 full_log_digest",
                ],
            },
            **analysis,
        }
        snapshot_payload = {
            "preview_version": 1,
            "scope": scope,
            "log_states": log_states,
            "analysis_digest": cls._digest({
                "current": result["current"],
                "strategies": result["strategies"],
            }),
        }
        result["snapshot_token"] = S1AdminScanSnapshotService.store(
            cls.SNAPSHOT_OPERATION,
            int(admin_user_id),
            snapshot_payload,
        )
        result["snapshot_token_available"] = bool(result["snapshot_token"])
        return result, None
