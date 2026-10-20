"""Project per-tavern optional AI stages into the public S1 orchestration."""
from pathlib import PurePosixPath


def replace(source, old, new, count=1):
    if source.count(old) != count:
        raise ValueError(f'Optional AI source drift ({source.count(old)}): {old[:110]!r}')
    return source.replace(old, new)


def replace_method(source, name, old, new):
    marker = '    def ' + name + '('
    head, sep, tail = source.partition(marker)
    if not sep:
        raise ValueError('Optional AI method missing: ' + name)
    body, end, rest = tail.partition('\n    @staticmethod')
    return head + sep + replace(body, old, new) + end + rest


def project_optional_ai(relative, raw):
    name = PurePosixPath(relative).name
    targets = {'adventurer_s1_expedition_runner.py', 's1_fate_destructive_change_contract.py',
        'adventurer_s1_data_appeal_service.py', 'adventurer_s1_data_appeal_ai_service.py',
        's1_appeal_rarity_restore_service.py', 's1_team_loot_service.py', 'AdminTraitCompressPanel.vue'}
    if name not in targets:
        return raw
    s = raw.decode('utf-8-sig').replace('\r\n', '\n')
    if name == 'AdminTraitCompressPanel.vue':
        # Preserve the existing public compression-status UI on snapshot refresh.
        s = replace(s, 'type CompareRow = {', 'type CompareRow = {\n  compression_enabled?: boolean')
        s = replace(s, 'function summaryStatus(row: CompareRow): StatusView {', "function summaryStatus(row: CompareRow): StatusView {\n  if (row.compression_enabled === false) return { label: '压缩已关闭，使用全文', tone: 'info' }")
        s = replace(s, 'function contextStatus(row: CompareRow): StatusView {', "function contextStatus(row: CompareRow): StatusView {\n  if (row.compression_enabled === false) return { label: '分类筛选已关闭', tone: 'info' }")
    elif name == 's1_team_loot_service.py':
        s = replace(s, "        if review.get('verdict_contract_version') != 2:\n            raise TeamLootError('共同实物尚未取得逐项独立复核授权')", '''        from services.tavern_appeal_policy import payload as policy_payload
        policy = policy_payload(review, appeal)
        if not policy and review.get('verdict_contract_version') != 2:
            raise TeamLootError('共同实物尚未取得逐项执行授权')''')
        s = replace(s, "        normalized, errors = normalize_team_loot(rows, events=(log.full_log or {}).get('events') or [])", '''        if policy:
            approved = {(op.get('audit_match') or {}).get('registration_hash'): op['operation_id']
                for op in policy['operations'] if op.get('section') == TEAM_LOOT_FIELD}
        normalized, errors = normalize_team_loot(rows, events=(log.full_log or {}).get('events') or [])''')
    elif name == 'adventurer_s1_data_appeal_ai_service.py':
        s = replace(s, "appeal_category: str='other', token_usage_out:", "appeal_category: str='other', resolved_lookups: dict | None=None, token_usage_out:")
        s = replace(s, "    if recent_activity:\n        payload['recent_activity'] = recent_activity", "    if resolved_lookups:\n        payload['resolved_lookup_evidence'] = resolved_lookups\n    if recent_activity:\n        payload['recent_activity'] = recent_activity")
    elif name == 's1_appeal_rarity_restore_service.py':
        anchor = '    decisions = effective_data_appeal_operation_decisions(review.get(\'verdicts\'))'
        before = "    if not isinstance(review, dict) or review.get('verdict_contract_version') != 2:\n        return False\n"
        s = replace(s, before + anchor, '''    from services.tavern_appeal_policy import payload as policy_payload
    policy = policy_payload(review, appeal)
    if policy:
        return any(row.get('id') == record_id and row.get('rarity') == rarity
            for row in (policy['patch'].get(section) or {}).get('update', []) if isinstance(row, dict))
''' + before + anchor)
    elif name == 'adventurer_s1_data_appeal_service.py':
        s = replace(s, "            appeal.review_ai_summary = '独立复核已完成，但最终写入达到自动重试上限，自动处理已结束'", "            appeal.review_ai_summary = ('初审原稿通过后端校验，但最终写入达到自动重试上限，自动处理已结束' if review_raw.get('skipped_by_admin') else '独立复核已完成，但最终写入达到自动重试上限，自动处理已结束')")
        s = replace(s, '        def _reject(reason: str, *, warn: bool=False) -> None:', '''        policy_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        if policy_raw.get('skipped_by_admin') is True:
            from services.tavern_appeal_policy import finalize
            finalize(appeal)
            return

        def _reject(reason: str, *, warn: bool=False) -> None:''')
        s = replace(s, '                if mechanical_qty_supplement:\n                    normalized =', "                if mechanical_qty_supplement and stage_enabled('appeal_second_review'):\n                    normalized =")
        s = replace(s, '            if not AdventurerS1DataAppealService._patch_has_item_record_benefit(normalized):', "            if stage_enabled('appeal_second_review') and not AdventurerS1DataAppealService._patch_has_item_record_benefit(normalized):")
        s = replace(s, '            if needs_review:\n', '''            from services.tavern_ai_stage_settings import enabled as stage_enabled
            if not stage_enabled('appeal_second_review'):
                from services.tavern_appeal_policy import create_policy
                review_model = None
                review_raw = create_policy(appeal, raw, normalized, patch_err, contract_valid=primary_ai_contract_candidate)
            elif needs_review:
''')
        s = replace(s, "            if resume_stage == 'review_ai':\n                review_raw = AdventurerS1DataAppealService._preserve_completed_review_retry_state", "            if resume_stage == 'review_ai' and not review_raw.get('skipped_by_admin'):\n                review_raw = AdventurerS1DataAppealService._preserve_completed_review_retry_state")
        s = replace(s, '            appeal.review_ai_model = review_model\n', "            if review_raw.get('skipped_by_admin'):\n                appeal.review_supplement_patch = None\n            appeal.review_ai_model = review_model\n")
        s = replace(s, '            if isinstance(review_raw, dict):\n                enforced_review_raw =', "            if isinstance(review_raw, dict) and not review_raw.get('skipped_by_admin'):\n                enforced_review_raw =")
        s = replace(s, "        pending_stage = stage if stage in ('primary_ai', 'review_ai', 'execution') else 'execution'", "        pending_stage = stage if stage in ('primary_ai', 'review_ai', 'execution') else 'execution'")
        s = replace(s, "            pending_stage = 'review_ai'\n        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}", '''            pending_stage = 'review_ai'
        from services.tavern_ai_stage_settings import enabled as stage_enabled
        if pending_stage == 'review_ai' and not stage_enabled('appeal_second_review'):
            pending_stage = 'primary_ai'
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}''')
        s = replace(s, "        request_verdicts = review_raw.get('request_verdicts')\n        review_decision =", '''        if review_raw.get('skipped_by_admin'):
            from services.tavern_appeal_policy import parse_policy
            policy = parse_policy(appeal)
            return policy is not None and policy.get('contract_valid') is True
        request_verdicts = review_raw.get('request_verdicts')
        review_decision =''')
        s = replace(s, "            if resume_stage == 'review_ai':\n                raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal)", '''            from services.tavern_ai_stage_settings import enabled as stage_enabled
            if not stage_enabled('appeal_second_review'):
                previous_primary = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
                ai_kwargs['resolved_lookups'] = {
                    'related_records': AdventurerS1DataAppealService.build_related_records_for_appeal_review(appeal.adventurer_id, snapshot_before=snapshot, focus_refs=previous_primary.get('focus_refs'), patch=previous_primary.get('patch')),
                    'lookup_results': AdventurerS1DataAppealService.resolve_lookup_queries(appeal.adventurer_id, previous_primary.get('lookup_queries')),
                }
            if resume_stage == 'review_ai':
                raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal)''')
        s = replace(s, '            normalized, patch_err = AdventurerS1DataAppealService.normalize_patch(patch_raw, appeal.snapshot_before, all_player_records=all_player_records, narrative_currency_cap=narrative_currency_cap, allow_all_player_record_ids=False,', '''            from services.tavern_ai_stage_settings import enabled as stage_enabled
            from services.tavern_appeal_policy import explicit_clear_ids
            policy_clear_ids = explicit_clear_ids(patch_raw) if not stage_enabled('appeal_second_review') else None
            normalized, patch_err = AdventurerS1DataAppealService.normalize_patch(patch_raw, appeal.snapshot_before, all_player_records=all_player_records, narrative_currency_cap=narrative_currency_cap, allow_all_player_record_ids=False, allow_quantity_increase=not stage_enabled('appeal_second_review'), allow_custom_desc_clear_ids=policy_clear_ids,''')
        # A signed proposal replaces only the independent semantic authority;
        # frozen IDs, payloads, ownership and transaction checks remain below.
        s = replace(s, '        allowed = {section: set() for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS}\n', '''        allowed = {section: set() for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS}
        from services.tavern_appeal_policy import payload as policy_payload
        policy = policy_payload(review_raw)
        if policy:
            return {section: destructive_action_source_ids(policy['patch'], section) for section in allowed}
''')
        s = replace_method(s, '_collect_custom_desc_clear_ids', "        allowed = {'equipment': set(), 'warehouse': set()}\n", '''        allowed = {'equipment': set(), 'warehouse': set()}
        from services.tavern_appeal_policy import payload as policy_payload, explicit_clear_ids
        policy = policy_payload(review_raw)
        if policy:
            return explicit_clear_ids(policy['patch'])
''')
        s = replace(s, "        expected_name = str(item_name or '').strip()", '''        from services.tavern_appeal_policy import payload as policy_payload
        policy = policy_payload(parsed, appeal)
        if policy:
            return any(row.get('name') == item_name for row in policy['patch'].get('create_items', []) if isinstance(row, dict))
        expected_name = str(item_name or '').strip()''')
    elif name == 's1_fate_destructive_change_contract.py':
        s = replace(s, '    projected = {\n        key: value[key]\n', '''    from services.tavern_ai_stage_authority import verified_fate
    policy = verified_fate(value, context)
    if policy is not None and not needs_regeneration:
        return policy, None
    projected = {
        key: value[key]
''')
        s = replace(s, "            'retire_without_successor',\n            'replace_with_successors',\n",
            "            'retire_without_successor',\n            'replace_with_successors',\n            'admin_original_proposal',\n")
        s = replace(s, "                'retire_without_successor',\n                'replace_with_successors',\n",
            "                'retire_without_successor',\n                'replace_with_successors',\n                'admin_original_proposal',\n")
    else:
        s = replace(s, "    REGENERATE_MAIN = 'regenerate_main'", "    SKIP_DISABLED_STAGE = 'skip_disabled_stage'\n    REGENERATE_MAIN = 'regenerate_main'")
        s = replace(s, '    high_risk_adjudication_required = requires_s1_high_risk_adjudication(high_risk_context)', '''    from services.tavern_ai_stage_settings import enabled as stage_enabled
    high_risk_stage_enabled = stage_enabled('high_risk_adjudication')
    high_risk_adjudication_required = requires_s1_high_risk_adjudication(high_risk_context)''')
        s = replace(s, '    event_resolution = restore_compact_draft(frozen_event_resolution, members)',
            '    event_resolution = restore_compact_draft(frozen_event_resolution, members) if high_risk_stage_enabled else None')
        s = replace(s, '    if high_risk_adjudication_required and event_resolution is None:',
            '    if high_risk_stage_enabled and high_risk_adjudication_required and event_resolution is None:')
        s = replace(s, "        observer_review_enabled = _coerce_bool(current_app.config.get('ADVENTURER_S1_EXPEDITION_OBSERVER_REVIEW_ENABLED', False), False)", '''        from services.tavern_ai_stage_settings import enabled as stage_enabled
        from services.tavern_ai_stage_authority import authorize_fate, issue as issue_stage_policy
        observer_stage_enabled = stage_enabled('expedition_observer')
        observer_review_enabled = _coerce_bool(current_app.config.get('ADVENTURER_S1_EXPEDITION_OBSERVER_REVIEW_ENABLED', False), False)''')
        s = replace(s, '            nonlocal history_anchor_repairs\n', '            nonlocal history_anchor_repairs, observer_feedback\n')
        s = replace(s, "            resume_guard_only = resume_stage == 'settlement_guard_ai'", '''            if not observer_stage_enabled and resume_stage == 'review_ai':
                review_retry_candidate = None
                observer_feedback = build_s1_retry_validation_feedback(review_retry_errors, post_member_context)
                resume_stage = 'main_ai'
            resume_guard_only = resume_stage == 'settlement_guard_ai' '''.rstrip())
        s = replace(s, '            pending_story_release = _pending_s1_review_failure_story_release(generation_retry_trace)', '''            if not observer_stage_enabled:
                conflicts = [row for row in _build_s1_same_name_gain_review_context(candidate, audit_member_snapshot) if row.get('conflict_kind') == 'equipment_update_final_name']
                if conflicts:
                    context_digest = _s1_same_name_gain_review_context_digest(conflicts)
                    candidate[_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD] = {
                        'schema_version': 2, 'status': 'skipped_by_admin', 'reviews': [],
                        'context_digest': context_digest,
                        'admin_policy': issue_stage_policy('expedition_observer', 'same_name', context_digest)}
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer',
                    reason_code='observer_disabled_by_admin', action=S1RetryAction.SKIP_DISABLED_STAGE,
                    attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True,
                    detail='管理员关闭独立剧情审核；主剧情提案通过后端校验后执行。')
            pending_story_release = _pending_s1_review_failure_story_release(generation_retry_trace)''')
        s = replace(s, 'original_release = bool(release_unreviewed_ordinary_loot or', 'original_release = bool(not observer_stage_enabled or release_unreviewed_ordinary_loot or')
        s = replace(s, 'authorize_team_loot_original_release(candidate, review_failure_fallback_candidate, roster=', 'authorize_team_loot_original_release(candidate, review_failure_fallback_candidate, roster=')
        s = replace(s, '            if final_fate_destructive_context_errors:', '''            if not observer_stage_enabled and final_fate_destructive_review_context and not final_fate_destructive_context_errors:
                result_data[FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD] = authorize_fate(final_fate_destructive_review_context)
            if final_fate_destructive_context_errors:''')
        s = replace(s, '            if main_result_generated:\n                review_failure_fallback_candidate =',
            '            if main_result_generated or not observer_stage_enabled:\n                review_failure_fallback_candidate =')
        s = replace(s, '            elif observer_review_enabled or retained_physical_loot_review_requested or hard_specialized_observer_review_requested:',
            '            elif observer_stage_enabled and (observer_review_enabled or retained_physical_loot_review_requested or hard_specialized_observer_review_requested):')
        s = replace(s, "'reason': '剧情审核 AI 已关闭，仅执行后端轻量校验',", "'reason': '未派发独立剧情审核，继续后端校验', 'skipped_by_admin': not observer_stage_enabled,")
        s = replace(s, "            if not infra_failed and candidate.get('unassigned_items')", "            if observer_stage_enabled and not infra_failed and candidate.get('unassigned_items')")
        s = replace(s, '            if reviewed_continuity_context and (not infra_failed):',
            '            if observer_stage_enabled and reviewed_continuity_context and (not infra_failed):')
        s = replace(s, '            if post_hard_errors and len(quantity_repair_errors)',
            '            if observer_stage_enabled and post_hard_errors and len(quantity_repair_errors)')
        s = replace(s, 'allow_observer_equipment_repairs=True, frozen_objective_outline=',
            'allow_observer_equipment_repairs=observer_stage_enabled, frozen_objective_outline=')
        s = replace(s, '                if len(observer_fixable_errors) == len(post_hard_errors):',
            '                if observer_stage_enabled and len(observer_fixable_errors) == len(post_hard_errors):')
        s = replace(s, "    unavailable_preserved = bool(isinstance(receipt, dict)", "    from services.tavern_ai_stage_authority import verify as verify_stage_policy\n    admin_preserved = isinstance(receipt, dict) and receipt.get('status') == 'skipped_by_admin' and receipt.get('reviews') == [] and verify_stage_policy(receipt.get('admin_policy'), 'same_name', expected_context_digest)\n    unavailable_preserved = admin_preserved or bool(isinstance(receipt, dict)")
        s = replace(s, "    rebound_receipt = {**deepcopy(receipt), 'context_digest': _s1_same_name_gain_review_context_digest(final_context)}", '''    rebound_receipt = {**deepcopy(receipt), 'context_digest': _s1_same_name_gain_review_context_digest(final_context)}
    if receipt.get('status') == 'skipped_by_admin' and reviewed_context != final_context:
        # Description finalization has verified the only changed fields above.
        from services.tavern_ai_stage_authority import _signature
        policy = rebound_receipt['admin_policy']
        policy['payload'] = rebound_receipt['context_digest']
        policy['signature'] = _signature('same_name', {k: v for k, v in policy.items() if k != 'signature'})''')
    return s.encode('utf-8')
