"""Bounded cross-log sizing preview for S1 expedition snapshot deduplication."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import hashlib
import logging
from datetime import datetime
from typing import Any, Callable

from flask import current_app

from db_repository.s1_expedition_log_dedup_repository import list_preview_logs
from plugins.adventurer_s1.cold_storage import estimate_json_storage_bytes
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_expedition_log_dedup_preview_service import (
    S1ExpeditionLogDedupPreviewService,
)
from utils.time_utils import to_utc_iso


logger = logging.getLogger(__name__)


class _PreviewSnapshotChanged(RuntimeError):
    """Raised when the second read no longer matches the first read."""


class S1ExpeditionLogDedupGlobalPreviewService:
    """Estimate one content dictionary shared by every log in a bounded scan."""

    SNAPSHOT_OPERATION = "s1_expedition_log_dedup_global_preview_v2"
    ASSET_KINDS = S1ExpeditionLogDedupPreviewService.ASSET_KINDS
    MODES = ("whole", "static")

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

    @classmethod
    def _empty_malformed(cls) -> dict[str, int]:
        return {
            "non_object_full_log_count": 0,
            "missing_snapshot_count": 0,
            "non_list_snapshot_count": 0,
            "non_object_member_count": 0,
            "missing_asset_collection_count": 0,
            "non_list_asset_collection_count": 0,
            "non_object_asset_count": 0,
        }

    @classmethod
    def _empty_kind_totals(cls) -> dict[str, dict[str, int]]:
        return {
            kind: {
                "reference_count": 0,
                "payload_raw_bytes": 0,
            }
            for kind in cls.ASSET_KINDS
        }

    @classmethod
    def _add_catalog_item(
        cls,
        *,
        catalog: dict[str, dict],
        kind_totals: dict[str, dict[str, int]],
        kind: str,
        payload: dict,
        log_id: int,
    ) -> None:
        canonical = S1ExpeditionLogDedupPreviewService._canonical_bytes(payload)
        key = S1ExpeditionLogDedupPreviewService._content_key(kind, payload)
        collision_fingerprint = hashlib.sha512(canonical).hexdigest()
        item = catalog.get(key)
        if item is None:
            raw_storage = estimate_json_storage_bytes(payload, cold=False)
            cold_storage = estimate_json_storage_bytes(payload, cold=True)
            item = {
                "kind": kind,
                "collision_fingerprint": collision_fingerprint,
                "occurrences": 0,
                "distinct_log_count": 0,
                "last_log_id": None,
                "payload_raw_bytes": len(canonical),
                "best_payload_storage_bytes": min(raw_storage, cold_storage),
                "preferred_storage": (
                    "cold_zlib_envelope"
                    if cold_storage < raw_storage
                    else "plain_json"
                ),
            }
            catalog[key] = item
        elif (
            item["collision_fingerprint"] != collision_fingerprint
            or item["kind"] != kind
        ):
            raise ValueError("expedition log dedup content digest collision")

        item["occurrences"] += 1
        if item["last_log_id"] != int(log_id):
            item["last_log_id"] = int(log_id)
            item["distinct_log_count"] += 1
        kind_totals[kind]["reference_count"] += 1
        kind_totals[kind]["payload_raw_bytes"] += len(canonical)

    @classmethod
    def _accumulate_catalog_page(
        cls,
        rows: list[dict],
        *,
        catalogs: dict[str, dict[str, dict]],
        kind_totals: dict[str, dict[str, dict[str, int]]],
        malformed: dict[str, int],
    ) -> None:
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
                        for mode in cls.MODES:
                            payload = S1ExpeditionLogDedupPreviewService._asset_payload(
                                kind,
                                asset,
                                mode,
                            )
                            cls._add_catalog_item(
                                catalog=catalogs[mode],
                                kind_totals=kind_totals[mode],
                                kind=kind,
                                payload=payload,
                                log_id=int(row["id"]),
                            )

    @classmethod
    def _finalize_kind_summary(
        cls,
        catalog: dict[str, dict],
        kind_totals: dict[str, dict[str, int]],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for kind in cls.ASSET_KINDS:
            items = [item for item in catalog.values() if item["kind"] == kind]
            reference_count = int(kind_totals[kind]["reference_count"])
            unique_content_count = len(items)
            distinct_log_reference_count = sum(
                int(item["distinct_log_count"])
                for item in items
            )
            reuse_buckets = {
                "one_log": 0,
                "two_to_five_logs": 0,
                "six_to_twenty_logs": 0,
                "over_twenty_logs": 0,
            }
            for item in items:
                log_count = int(item["distinct_log_count"])
                if log_count <= 1:
                    reuse_buckets["one_log"] += 1
                elif log_count <= 5:
                    reuse_buckets["two_to_five_logs"] += 1
                elif log_count <= 20:
                    reuse_buckets["six_to_twenty_logs"] += 1
                else:
                    reuse_buckets["over_twenty_logs"] += 1
            result[kind] = {
                "reference_count": reference_count,
                "unique_content_count": unique_content_count,
                "duplicate_reference_count": reference_count - unique_content_count,
                "payload_raw_bytes": int(kind_totals[kind]["payload_raw_bytes"]),
                "unique_payload_raw_bytes": sum(
                    int(item["payload_raw_bytes"])
                    for item in items
                ),
                "distinct_log_reference_count": distinct_log_reference_count,
                "cross_log_reused_content_count": sum(
                    1 for item in items if int(item["distinct_log_count"]) >= 2
                ),
                "cross_log_reuse_count": sum(
                    max(0, int(item["distinct_log_count"]) - 1)
                    for item in items
                ),
                "log_reuse_buckets": reuse_buckets,
            }
        return result

    @classmethod
    def _scan_pages(
        cls,
        *,
        before_log_id: int | None,
        scan_limit: int,
        page_size: int,
        cold_only: bool,
        consume: Callable[[list[dict]], None],
    ) -> dict[str, Any]:
        cursor = before_log_id
        scanned = 0
        pages = 0
        first_log_id = None
        last_log_id = None
        has_more = False
        while scanned < scan_limit:
            request_limit = min(page_size, scan_limit - scanned)
            rows, page_has_more = list_preview_logs(
                before_log_id=cursor,
                limit=request_limit,
                cold_only=cold_only,
            )
            if not rows:
                has_more = False
                break
            if first_log_id is None:
                first_log_id = int(rows[0]["id"])
            consume(rows)
            pages += 1
            scanned += len(rows)
            last_log_id = int(rows[-1]["id"])
            cursor = last_log_id
            has_more = bool(page_has_more)
            if not page_has_more:
                break
        return {
            "log_count": scanned,
            "pages_scanned": pages,
            "first_log_id": first_log_id,
            "last_log_id": last_log_id,
            "next_before_log_id": last_log_id if has_more else None,
            "has_more": has_more,
        }

    @classmethod
    def _strategy_result(
        cls,
        *,
        name: str,
        mode: str,
        selected_keys: set[str],
        catalog: dict[str, dict],
        by_kind: dict[str, dict[str, Any]],
        rewritten_log_bytes: int,
        current_storage_bytes: int,
        blob_overhead_bytes: int,
        min_saving_ratio: float,
    ) -> dict[str, Any]:
        blob_payload_bytes = sum(
            int(catalog[key]["best_payload_storage_bytes"])
            for key in selected_keys
        )
        estimated_blob_overhead_bytes = len(selected_keys) * int(blob_overhead_bytes)
        projected = (
            int(rewritten_log_bytes)
            + blob_payload_bytes
            + estimated_blob_overhead_bytes
        )
        saving = int(current_storage_bytes) - projected
        saving_ratio = saving / current_storage_bytes if current_storage_bytes else 0.0
        selected_reference_count = sum(
            int(catalog[key]["occurrences"])
            for key in selected_keys
        )
        selected_by_kind = {}
        for kind in cls.ASSET_KINDS:
            keys = [key for key in selected_keys if catalog[key]["kind"] == kind]
            references = sum(int(catalog[key]["occurrences"]) for key in keys)
            distinct_log_references = sum(
                int(catalog[key]["distinct_log_count"])
                for key in keys
            )
            selected_by_kind[kind] = {
                **by_kind[kind],
                "externalized_content_count": len(keys),
                "externalized_reference_count": references,
                "externalized_duplicate_reference_count": references - len(keys),
                "externalized_distinct_log_reference_count": distinct_log_references,
                "externalized_cross_log_reuse_count": sum(
                    max(0, int(catalog[key]["distinct_log_count"]) - 1)
                    for key in keys
                ),
            }
        recommended = saving > 0 and saving_ratio >= min_saving_ratio
        return {
            "name": name,
            "mode": mode,
            "dictionary_scope": "all_logs_in_scan",
            "content_selection": (
                "positive_raw_gain_and_cross_log_reuse"
                if name == "adaptive_static_content"
                else "all"
            ),
            "rewritten_log_storage_bytes": int(rewritten_log_bytes),
            "blob_payload_storage_bytes": blob_payload_bytes,
            "estimated_blob_record_and_index_overhead_bytes": (
                estimated_blob_overhead_bytes
            ),
            "projected_total_storage_bytes": projected,
            "estimated_saving_bytes": saving,
            "estimated_saving_ratio": round(saving_ratio, 6),
            "externalized_content_count": len(selected_keys),
            "externalized_reference_count": selected_reference_count,
            "externalized_duplicate_reference_count": (
                selected_reference_count - len(selected_keys)
            ),
            "recommended_for_scanned_scope": recommended,
            "by_kind": selected_by_kind,
        }

    @classmethod
    def preview(
        cls,
        *,
        admin_user_id: int,
        before_log_id: object = None,
        scan_limit: object = None,
        cold_only: bool = True,
    ) -> tuple[dict | None, str | None]:
        max_logs = cls._config_int(
            "ADVENTURER_S1_LOG_DEDUP_GLOBAL_PREVIEW_MAX_LOGS",
            1000,
            minimum=1,
            maximum=2000,
        )
        default_logs = cls._config_int(
            "ADVENTURER_S1_LOG_DEDUP_GLOBAL_PREVIEW_DEFAULT_LOGS",
            500,
            minimum=1,
            maximum=max_logs,
        )
        page_size = cls._config_int(
            "ADVENTURER_S1_LOG_DEDUP_GLOBAL_PREVIEW_PAGE_SIZE",
            100,
            minimum=10,
            maximum=250,
        )
        try:
            parsed_limit = default_logs if scan_limit in (None, "") else int(scan_limit)
        except (TypeError, ValueError):
            return None, "scan_limit 必须是正整数"
        if parsed_limit <= 0 or parsed_limit > max_logs:
            return None, f"scan_limit 必须在 1 到 {max_logs} 之间"

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
        catalogs = {mode: {} for mode in cls.MODES}
        kind_totals = {
            mode: cls._empty_kind_totals()
            for mode in cls.MODES
        }
        malformed = cls._empty_malformed()
        log_states: list[dict[str, Any]] = []
        current = {
            "log_count": 0,
            "full_log_raw_json_bytes": 0,
            "audit_member_snapshot_raw_json_bytes": 0,
            "current_logical_storage_bytes": 0,
            "cold_log_count": 0,
        }
        created_at_values = []

        def collect_first_pass(rows: list[dict]) -> None:
            cls._accumulate_catalog_page(
                rows,
                catalogs=catalogs,
                kind_totals=kind_totals,
                malformed=malformed,
            )
            for row in rows:
                full_log = row.get("full_log") or {}
                is_cold = row.get("cold_compressed_at") is not None
                current["log_count"] += 1
                current["full_log_raw_json_bytes"] += estimate_json_storage_bytes(
                    full_log,
                    cold=False,
                )
                current["audit_member_snapshot_raw_json_bytes"] += (
                    estimate_json_storage_bytes(
                        (
                            full_log.get("audit_member_snapshot") or []
                            if isinstance(full_log, dict)
                            else []
                        ),
                        cold=False,
                    )
                )
                current["current_logical_storage_bytes"] += (
                    estimate_json_storage_bytes(full_log, cold=is_cold, expedition_log=True)
                )
                current["cold_log_count"] += int(is_cold)
                if row.get("created_at") is not None:
                    created_at_values.append(row["created_at"])
                log_states.append({
                    "id": int(row["id"]),
                    "status": str(row.get("status") or ""),
                    "created_at": (
                        to_utc_iso(row.get("created_at"))
                        if row.get("created_at")
                        else None
                    ),
                    "updated_at": (
                        to_utc_iso(row.get("updated_at"))
                        if row.get("updated_at")
                        else None
                    ),
                    "cold_compressed_at": (
                        to_utc_iso(row.get("cold_compressed_at"))
                        if row.get("cold_compressed_at")
                        else None
                    ),
                    "full_log_digest": (
                        S1ExpeditionLogDedupPreviewService._digest(full_log)
                    ),
                })

        try:
            first_scan = cls._scan_pages(
                before_log_id=parsed_before,
                scan_limit=parsed_limit,
                page_size=page_size,
                cold_only=bool(cold_only),
                consume=collect_first_pass,
            )
            effective_before = parsed_before
            if effective_before is None and first_scan["first_log_id"] is not None:
                effective_before = int(first_scan["first_log_id"]) + 1

            whole_by_kind = cls._finalize_kind_summary(
                catalogs["whole"],
                kind_totals["whole"],
            )
            static_by_kind = cls._finalize_kind_summary(
                catalogs["static"],
                kind_totals["static"],
            )
            selected_keys = {
                "whole_asset_versions": set(catalogs["whole"]),
                "all_static_content": set(catalogs["static"]),
                "adaptive_static_content": {
                    key
                    for key in S1ExpeditionLogDedupPreviewService._adaptive_static_keys(
                        catalogs["static"],
                        blob_overhead_bytes=blob_overhead_bytes,
                    )
                    if int(catalogs["static"][key]["distinct_log_count"]) >= 2
                },
            }
            strategy_modes = {
                "whole_asset_versions": "whole",
                "all_static_content": "static",
                "adaptive_static_content": "static",
            }
            ref_ids = {
                name: {
                    key: index
                    for index, key in enumerate(sorted(keys), start=1)
                }
                for name, keys in selected_keys.items()
            }
            rewritten_log_bytes = {name: 0 for name in selected_keys}
            second_state_index = 0

            def collect_second_pass(rows: list[dict]) -> None:
                nonlocal second_state_index
                for row in rows:
                    if second_state_index >= len(log_states):
                        raise _PreviewSnapshotChanged()
                    expected = log_states[second_state_index]
                    actual_updated_at = (
                        to_utc_iso(row.get("updated_at"))
                        if row.get("updated_at")
                        else None
                    )
                    actual_created_at = (
                        to_utc_iso(row.get("created_at"))
                        if row.get("created_at")
                        else None
                    )
                    actual_cold_compressed_at = (
                        to_utc_iso(row.get("cold_compressed_at"))
                        if row.get("cold_compressed_at")
                        else None
                    )
                    actual_digest = S1ExpeditionLogDedupPreviewService._digest(
                        row.get("full_log") or {},
                    )
                    if (
                        int(row["id"]) != int(expected["id"])
                        or str(row.get("status") or "") != expected["status"]
                        or actual_created_at != expected["created_at"]
                        or actual_updated_at != expected["updated_at"]
                        or actual_cold_compressed_at != expected["cold_compressed_at"]
                        or actual_digest != expected["full_log_digest"]
                    ):
                        raise _PreviewSnapshotChanged()
                    second_state_index += 1

                for name, mode in strategy_modes.items():
                    transformed = S1ExpeditionLogDedupPreviewService._transform_logs(
                        rows,
                        mode=mode,
                        selected_keys=selected_keys[name],
                        ref_ids=ref_ids[name],
                    )
                    rewritten_log_bytes[name] += sum(
                        estimate_json_storage_bytes(
                            full_log,
                            cold=rows[index].get("cold_compressed_at") is not None,
                            expedition_log=True,
                        )
                        for index, full_log in enumerate(transformed)
                    )

            second_scan = cls._scan_pages(
                before_log_id=effective_before,
                scan_limit=len(log_states),
                page_size=page_size,
                cold_only=bool(cold_only),
                consume=collect_second_pass,
            ) if log_states else {
                "log_count": 0,
                "pages_scanned": 0,
                "first_log_id": None,
                "last_log_id": None,
                "next_before_log_id": None,
                "has_more": False,
            }
            if (
                second_state_index != len(log_states)
                or second_scan["log_count"] != len(log_states)
                or second_scan["first_log_id"] != first_scan["first_log_id"]
                or second_scan["last_log_id"] != first_scan["last_log_id"]
            ):
                raise _PreviewSnapshotChanged()
        except _PreviewSnapshotChanged:
            return None, "预览失败：扫描期间日志发生变化，请稍后重新预览"
        except Exception:  # noqa: BLE001
            logger.exception("S1 cross-log dedup global preview failed")
            return None, "预览失败：服务端跨日志估算异常，请查看日志"

        unsupported_shape_count = sum(
            int(value)
            for key, value in malformed.items()
            if key not in {"missing_snapshot_count", "missing_asset_collection_count"}
        )
        strategies = [
            cls._strategy_result(
                name=name,
                mode=strategy_modes[name],
                selected_keys=selected_keys[name],
                catalog=catalogs[strategy_modes[name]],
                by_kind=(whole_by_kind if strategy_modes[name] == "whole" else static_by_kind),
                rewritten_log_bytes=rewritten_log_bytes[name],
                current_storage_bytes=current["current_logical_storage_bytes"],
                blob_overhead_bytes=blob_overhead_bytes,
                min_saving_ratio=min_saving_ratio,
            )
            for name in (
                "whole_asset_versions",
                "all_static_content",
                "adaptive_static_content",
            )
        ]
        if unsupported_shape_count:
            for strategy in strategies:
                strategy["recommended_for_scanned_scope"] = False
        best = max(
            strategies,
            key=lambda item: int(item["estimated_saving_bytes"]),
        ) if strategies else None
        if not log_states:
            decision = "no_data"
            reason_codes = ["empty_scan"]
        elif unsupported_shape_count:
            decision = "blocked"
            reason_codes = ["unsupported_snapshot_shape"]
        elif not best or int(best["estimated_saving_bytes"]) <= 0:
            decision = "do_not_execute"
            reason_codes = ["non_positive_estimated_saving"]
        elif not best["recommended_for_scanned_scope"]:
            decision = "insufficient_margin"
            reason_codes = ["saving_below_safety_ratio"]
        else:
            decision = "candidate"
            reason_codes = ["positive_saving_with_safety_margin"]

        scope = {
            "requested_before_log_id": parsed_before,
            "effective_before_log_id": effective_before,
            "scan_limit": parsed_limit,
            "scanned_log_count": len(log_states),
            "page_size": page_size,
            "pages_scanned_per_pass": first_scan["pages_scanned"],
            "pages_scanned_total": (
                first_scan["pages_scanned"] + second_scan["pages_scanned"]
            ),
            "database_read_passes": 2 if log_states else 1,
            "cold_only": bool(cold_only),
            "statuses": ["completed", "failed"],
            "first_log_id": first_scan["first_log_id"],
            "last_log_id": first_scan["last_log_id"],
            "next_before_log_id": first_scan["next_before_log_id"],
            "has_more": first_scan["has_more"],
            "created_at_from": (
                to_utc_iso(min(created_at_values)) if created_at_values else None
            ),
            "created_at_to": (
                to_utc_iso(max(created_at_values)) if created_at_values else None
            ),
            "consistent_across_read_passes": True,
        }
        result = {
            "preview_version": 2,
            "generated_at": _portable_utc_now().isoformat() + "Z",
            "mode": "read_only_cross_log_global_preview",
            "execution_available": False,
            "scope": scope,
            "estimate_basis": {
                "codec": "zlib_level_9_base64_json_envelope",
                "dictionary_scope": "all_logs_in_scan",
                "content_address": "sha256_canonical_json_with_kind",
                "collision_guard": "sha512_canonical_json",
                "physical_postgresql_bytes_measured": False,
                "blob_record_and_index_overhead_bytes": blob_overhead_bytes,
                "minimum_recommended_saving_ratio": min_saving_ratio,
                "notes": [
                    "内容字典跨越本次扫描内的全部日志，不会在分页边界重置",
                    "两遍读取必须逐条匹配日志 ID、更新时间和 full_log 摘要",
                    "结果仍是逻辑序列化估算，未计量 TOAST、WAL、死元组和关系文件回收",
                    "后续执行必须重新锁定并核对扫描范围内的完整日志摘要",
                ],
            },
            "current": current,
            "strategies": strategies,
            "best_strategy": best["name"] if best else None,
            "best_estimated_saving_bytes": (
                int(best["estimated_saving_bytes"]) if best else 0
            ),
            "best_estimated_saving_ratio": (
                float(best["estimated_saving_ratio"]) if best else 0.0
            ),
            "recommended_for_scanned_scope": bool(
                best
                and best["recommended_for_scanned_scope"]
                and not unsupported_shape_count
            ),
            "decision": decision,
            "reason_codes": reason_codes,
            "unsupported_shape_count": unsupported_shape_count,
            "malformed": malformed,
        }
        scan_state_digest = S1ExpeditionLogDedupPreviewService._digest(log_states)
        result["scan_state_digest"] = scan_state_digest
        snapshot_payload = {
            "preview_version": 2,
            "scope": scope,
            "log_states": log_states,
            "scan_state_digest": scan_state_digest,
            "analysis_digest": S1ExpeditionLogDedupPreviewService._digest({
                "current": current,
                "strategies": strategies,
            }),
        }
        result["snapshot_token"] = S1AdminScanSnapshotService.store(
            cls.SNAPSHOT_OPERATION,
            int(admin_user_id),
            snapshot_payload,
        )
        result["snapshot_token_available"] = bool(result["snapshot_token"])
        return result, None
