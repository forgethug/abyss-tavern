"""Squad invitations attached to existing campaign world-line evolution."""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import random
import math
from datetime import datetime

from flask import current_app
from utils.time_utils import to_utc_iso

from db_repository import db_manager
from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository as Repo
from db_repository.s1_campaign_stage_repository import S1CampaignStageRepository as Stages
from plugins.adventurer_s1.services.s1_campaign_stage_contract import (
    stage_definition, eligible_branches, available_stage_maps, apply_node_report,
    completion_node_keys,
)
from plugins.adventurer_s1.services.s1_campaign_dispatch_contract import (
    DISPATCH_LOG_FIELD, extract_dispatch_report, project_dispatch_context, project_dispatch_facts,
)
from plugins.adventurer_s1.services.s1_campaign_revelation_contract import (
    revelation_enabled, public_stage_title, freeze_revelation_context,
)
from plugins.adventurer_s1.services.s1_campaign_challenge_contract import freeze_challenge_context, public_challenge


def _config(key):
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    return current_app.config.get(key, AdventurerS1Plugin.default_config[key])


def _node_with_challenge(node, world=None):
    """A future task may shrink; its map retains the fixed operator baseline."""
    if node.get('challenge') is not None or not (world or {}).get('challenge'):
        return node
    return {**node, 'challenge': world['challenge']}


def _public_challenge_fields(node, world=None):
    challenge = public_challenge(_node_with_challenge(node, world))
    return {'challenge': challenge} if challenge else {}


class S1CampaignDispatchService:
    @staticmethod
    def entry_maps(row, stage, worlds, published=()):
        """Public map access is independent from exclusive core-event ownership."""
        owned = {name for name, world in worlds.items()
                 if world['campaign_root_id'] == row.campaign_root_id}
        if not owned or row.ended_at or (stage and stage.status == 'ended'):
            return []
        known = (set(owned) if not stage else {
            node['dungeon'] for node in stage_definition(stage.plan, stage.stage_key)['nodes']
            if stage.progress[node['key']]['knowledge'] != 'hidden'})
        return sorted(owned & (known | set(published)))

    @staticmethod
    def enable(root_id):
        with db_manager.transaction():
            row, error = Repo.enable(root_id)
            return ({'id': row.id} if row else None), error

    @staticmethod
    def public_row(row, stage=None, worlds=None, busy_dungeons=None, published=()):
        busy_dungeons = list(busy_dungeons or [])
        active_count = len(busy_dungeons) or int(row.active_log_id is not None)
        definition = stage_definition(stage.plan, stage.stage_key) if stage else None
        visible_maps = ({n['dungeon'] for n in definition['nodes']
                         if stage.progress[n['key']]['knowledge'] != 'hidden'} if stage else
                        set(row.target_dungeons or []))
        busy_dungeons = [name for name in busy_dungeons if name in visible_maps]
        result = {'id': row.id, 'revision': row.revision, 'title': row.public_title,
                'summary': row.public_summary, 'objective': row.objective,
                'target_dungeons': list(row.target_dungeons or []),
                'status': 'ended' if row.ended_at else 'underway' if row.active_log_id else 'open',
                'ended_at': to_utc_iso(row.ended_at)}
        result.update(max_active_teams=row.max_active_teams, active_team_count=active_count,
                      busy_dungeons=busy_dungeons)
        if worlds is not None:
            activity_maps = [world for world in worlds.values()
                             if world['campaign_root_id'] == row.campaign_root_id]
            if activity_maps:
                result['isolated_world'] = True
                entries = S1CampaignDispatchService.entry_maps(row, stage, worlds, published)
                result['core_task_dungeons'] = (available_stage_maps(definition, stage.progress)
                    if stage and stage.status == 'active' else list(row.target_dungeons or []) if not stage else [])
                result['target_dungeons'] = entries
                from .adventurer_s1_config_service import S1_DUNGEON_UNIQUE_TIERS
                result['dungeon_levels'] = {
                    entry['name']: {'min_level': entry['min_level'], 'max_level': entry['max_level'],
                                    'rank': S1_DUNGEON_UNIQUE_TIERS.index(entry['min_level'])}
                    for entry in activity_maps if entry['name'] in entries}
        if stage:
            result['stage'] = {
                'title': public_stage_title(stage.plan, definition), 'status': stage.status,
                'nodes': [{'dungeon': n['dungeon'], 'title': n['title'], 'objective': n['objective'],
                           'result': stage.progress[n['key']]['result'],
                           'required_for_success': n['key'] in completion_node_keys(definition),
                           'evidence_log_id': stage.progress[n['key']].get('evidence_log_id'),
                           **_public_challenge_fields(n, (worlds or {}).get(n['dungeon']))}
                          for n in definition['nodes'] if stage.progress[n['key']]['knowledge'] != 'hidden'],
            }
        return result

    @staticmethod
    def board(page=1, per_page=12):
        total, rows = Repo.public_page(page, per_page)
        stages = Stages.states([row.id for row in rows])
        occupancy = Repo.occupancy([row.id for row in rows])
        published = Repo.published_dungeons([row.id for row in rows])
        from db_repository.s1_campaign_evolution_repository import S1CampaignEvolutionRepository
        notices = S1CampaignEvolutionRepository.public_notices([row.id for row in rows])
        from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository
        worlds = S1CampaignWorldRepository.worlds_for_roots({row.campaign_root_id for row in rows})
        return {'items': [dict(S1CampaignDispatchService.public_row(
                    row, stages.get(row.id), worlds, occupancy[row.id], published[row.id]),
                    **({'world_notice': {**notices[row.id],
                        'occurred_at': to_utc_iso(notices[row.id]['occurred_at'])}} if row.id in notices else {})) for row in rows],
                'page': page, 'per_page': per_page, 'total': total,
                'has_next': page * per_page < total, 'hall_built': Repo.hall_built()}

    @staticmethod
    def history(dispatch_id, page=1, per_page=12):
        row = Repo.get(dispatch_id)
        if not row or not row.discovered_at:
            return None, '事件尚未公开'
        total, reports = Repo.report_page(dispatch_id, page, per_page, chronological=bool(row.ended_at))
        return {'items': [{'party_name': r.party_name, 'revision': r.revision,
                           'expedition_log_id': r.expedition_log_id,
                           'title': r.report['public_title'], 'summary': r.report['public_summary'],
                           'outcome': r.report['outcome']} for r in reports],
                'page': page, 'per_page': per_page, 'total': total, 'has_next': page * per_page < total}, None

    @staticmethod
    def bind_ticket(ticket, dispatch_id, revision):
        """Called inside ticket creation's transaction, after playable owner validation."""
        if not Repo.hall_built():
            return '战役议事厅尚未建成'
        row = Repo.get(dispatch_id, lock=True)
        if not row or not row.discovered_at or row.ended_at:
            return '当前事件不可接取'
        if row.revision != revision:
            return '事件已有后续，请刷新任务'
        from .s1_campaign_world_service import campaign_world
        world = campaign_world(ticket.preferred_dungeon)
        maps = row.target_dungeons or []
        if world:
            stage = Stages.states([row.id]).get(row.id)
            maps = S1CampaignDispatchService.entry_maps(row, stage,
                {ticket.preferred_dungeon: world}, Repo.published_dungeons([row.id])[row.id])
        if ticket.queue_type != 'group' or ticket.preferred_dungeon not in maps:
            return '请选择本任务对应的地图并组队接取'
        ticket.campaign_dispatch_id = row.id
        ticket.campaign_dispatch_revision = row.revision
        ticket.campaign_task_title = row.public_title
        return None

    @staticmethod
    def freeze_departure(log, party, locus_scope=None):
        """Run in dispatch transaction. A persisted empty result also prevents rerolls."""
        full = dict(log.full_log or {})
        if DISPATCH_LOG_FIELD in full:
            frozen = full[DISPATCH_LOG_FIELD]
            return project_dispatch_context(frozen) if isinstance(frozen, dict) else {}
        full[DISPATCH_LOG_FIELD] = {}
        log.full_log = full
        # Backend-only receipt, frozen with the departure, never an AI decision.
        decision = {
            'version': 1, 'captured_at': to_utc_iso(_portable_utc_now()),
            'target_dungeon': log.target_dungeon, 'locus_scope': locus_scope,
        }
        activity = None
        ticket = None

        def entry_allowed(*, lock=False):
            bound = Repo.get(ticket.campaign_dispatch_id, lock=lock) if ticket else None
            if (not bound or bound.campaign_root_id != activity['campaign_root_id']
                    or not bound.discovered_at or bound.ended_at
                    or bound.revision != ticket.campaign_dispatch_revision):
                return []
            stage = Stages.states([bound.id], lock=lock).get(bound.id)
            entries = S1CampaignDispatchService.entry_maps(
                bound, stage, {name: activity for name in activity['dungeons']},
                Repo.published_dungeons([bound.id])[bound.id])
            return entries if log.target_dungeon in entries else []

        def record(reason, context=None, **details):
            if activity and reason != 'activity_ticket_required':
                # No thread/actor locks are acquired after this fallback check.
                entries = None if context else entry_allowed(lock=True)
                if context or entries:
                    world = {key: value for key, value in activity.items() if key != 'challenge'}
                    if not context:
                        world['dungeons'] = entries
                        from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository
                        from db_repository.s1_campaign_evolution_repository import S1CampaignEvolutionRepository
                        state = S1CampaignWorldRepository.applied_local_state(
                            activity['campaign_root_id'], log.target_dungeon)
                        evolved = S1CampaignEvolutionRepository.departure_state(
                            ticket.campaign_dispatch_id, log.target_dungeon)
                        if evolved is not None or state:
                            world['current_world_state'] = evolved if evolved is not None else state
                    log.full_log = {**dict(log.full_log or {}), 'activity_world': world}
                else:
                    reason = 'activity_entry_changed'
            log.full_log = {
                **dict(log.full_log or {}),
                DISPATCH_LOG_FIELD: context or {},
                '_campaign_departure_decision': {**decision, 'reason': reason, **details},
            }
            return context or {}

        if not current_app.config.get('ADVENTURER_S1_WORLD_CAMPAIGN_ENABLED', True):
            return record('disabled')
        from .adventurer_s1_config_service import is_s1_independent_dungeon
        if is_s1_independent_dungeon(log.target_dungeon):
            return record('independent_dungeon')
        if log.hidden_dungeon:
            return record('hidden_dungeon')
        from plugins.adventurer_s1.services.s1_world_outline_service import (
            S1WorldOutlineService, _is_due_tavern_faction_interaction,
            _tavern_faction_interaction_cooldown_ready,
        )
        ticket = Repo.accepted_for_party(party.id)
        from .s1_campaign_world_service import campaign_world
        activity = campaign_world(log.target_dungeon, include_challenge=True)
        if activity:
            if not entry_allowed():
                return record('activity_ticket_required')
        if ticket:
            decision['ticket'] = {
                'id': ticket.id, 'dispatch_id': ticket.campaign_dispatch_id,
                'revision': ticket.campaign_dispatch_revision,
            }
        candidates = Repo.candidates(
            log.target_dungeon, ticket.campaign_dispatch_id if ticket else None,
            limit=max(1, int(_config('ADVENTURER_S1_CAMPAIGN_DISPATCH_CANDIDATE_LIMIT'))),
        )
        from plugins.adventurer_s1.services.s1_expedition_locus_service import story_scope_matches_expedition_locus
        stages = Stages.states([row.id for _, row in candidates])
        if not candidates:
            # Only inspect the bound invitation; do not scan every busy campaign.
            bound = Repo.get(ticket.campaign_dispatch_id) if ticket else None
            if bound:
                decision['bound_dispatch'] = {
                    'id': bound.id, 'revision': bound.revision,
                    'active_log_id': bound.active_log_id,
                    'ended_at': to_utc_iso(bound.ended_at),
                }
            return record('no_candidate')
        def map_eligible(row):
            stage = stages.get(row.id)
            if not stage:
                return log.target_dungeon in (row.target_dungeons or [])
            if stage.status != 'active':
                return False
            node = next((n for n in stage_definition(stage.plan, stage.stage_key)['nodes']
                         if n['dungeon'] == log.target_dungeon), None)
            if not node or stage.progress[node['key']]['result'] != 'pending':
                return False
            # Undiscovered local directions can still be encountered by ordinary
            # expeditions after another map first made the campaign public.
            return (stage.progress[node['key']]['knowledge'] != 'hidden' if ticket else
                    stage.progress[node['key']]['knowledge'] == 'hidden')
        eligible = []
        candidate_checks = []
        for thread, row in candidates:
            rejection = (
                'ticket_revision_changed' if ticket and row.revision != ticket.campaign_dispatch_revision
                else 'map_not_available' if not map_eligible(row)
                else 'locus_mismatch' if not story_scope_matches_expedition_locus(thread.scope_keys, locus_scope)
                else None
            )
            candidate_checks.append({
                'thread_id': thread.id, 'dispatch_id': row.id,
                'revision': row.revision, 'rejection': rejection,
            })
            if rejection is None:
                eligible.append((thread, row))
        decision['candidates'] = candidate_checks
        if not eligible:
            return record('no_eligible_candidate')
        # Preserve the older overdue tavern-interaction window; ordinary
        # departures with no eligible campaign never need this additional rank.
        if not activity and _tavern_faction_interaction_cooldown_ready(log.target_dungeon):
            priority_thread = next((
                row for score, row in
                S1WorldOutlineService._rank_world_story_threads_for_context(log.target_dungeon)
                if score > 0 and _is_due_tavern_faction_interaction(row)
                and story_scope_matches_expedition_locus(row.scope_keys, locus_scope)
            ), None)
            if priority_thread:
                return record('faction_priority', priority_thread_id=priority_thread.id)
        probability = float(_config('ADVENTURER_S1_CAMPAIGN_DISPATCH_TRIGGER_PROBABILITY'))
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('共同任务触发概率必须在 0 到 1 之间')
        draw = random.random()
        decision.update(probability=probability, draw=draw)
        if draw >= probability:
            return record('probability_miss')
        thread, candidate = eligible[0]
        from plugins.adventurer_s1.services.s1_narrative_lease_service import acquire_world_story_bundle_lease
        claimed = Repo.claim(thread, candidate.id, log, expected_revision=candidate.revision,
                             allow_hidden=ticket is None and activity is None,
                             acquire=lambda: acquire_world_story_bundle_lease(
            target_dungeon=log.target_dungeon, world_story_thread_id=thread.id,
            party_id=party.id, expedition_log_id=log.id, title=thread.title,
            campaign_dispatch_id=candidate.id,
        ))
        if not claimed:
            return record('claim_conflict', selected_thread_id=thread.id,
                          selected_dispatch_id=candidate.id)
        context = {'dispatch_id': claimed.id, 'revision': claimed.revision,
                   'thread_id': thread.id, 'source_revision': int(thread.revision or 1),
                   'discovery': claimed.discovered_at is None,
                   'objective': claimed.objective, 'public_summary': claimed.public_summary,
                   'current_world_state': claimed.current_world_state,
                   'allowed_dungeons': Repo.allowed_dungeons(claimed.campaign_root_id)}
        if claimed.max_active_teams > 1:
            context['parallel_sites'] = True
        if thread.status == 'dormant':
            context['closed_phase'] = True
            context['allowed_dungeons'] = sorted(set(context['allowed_dungeons']) | {log.target_dungeon})
        stage = Stages.states([claimed.id], lock=True).get(claimed.id)
        if stage:
            definition = stage_definition(stage.plan, stage.stage_key)
            node = next(n for n in definition['nodes'] if n['dungeon'] == log.target_dungeon)
            challenge_node = _node_with_challenge(node, activity)
            context['stage_task'] = {
                'stage_key': stage.stage_key, 'stage_instance': stage.stage_instance,
                'node_key': node['key'], 'title': node['title'], 'objective': node['objective'],
                'success_condition': node['success_condition'], 'failure_condition': node['failure_condition'],
            }
            context['objective'] = node['objective']
            context['allowed_dungeons'] = [n['dungeon'] for n in definition['nodes']
                                          if n['dungeon'] in context['allowed_dungeons']]
            published = (Repo.latest_reports_by_dungeon(claimed.id)
                         if (claimed.max_active_teams > 1 or revelation_enabled(stage.plan)
                              or (challenge_node.get('challenge') and not context['discovery'])) else {})
            if claimed.max_active_teams > 1:
                context['current_world_state'] = published.get(node['dungeon'], {}).get('applied_world_state', '')
            if revelation_enabled(stage.plan):
                freeze_revelation_context(context, definition, node, stage.progress,
                                          published)
            freeze_challenge_context(context, challenge_node, published)
            from plugins.adventurer_s1.services.s1_campaign_time_contract import freeze_action_window
            freeze_action_window(context, node, claimed, log)
        from db_repository.s1_campaign_evolution_repository import S1CampaignEvolutionRepository
        evolved = S1CampaignEvolutionRepository.departure_state(claimed.id, log.target_dungeon)
        if evolved is not None:
            context['current_world_state'] = evolved
        elif context.get('closed_phase') is True:
            # The actual closed scene outranks the last campaign announcement.
            context['current_world_state'] = thread.summary
        context = project_dispatch_context(context)
        return record('triggered', context, selected_thread_id=thread.id,
                      selected_dispatch_id=claimed.id)

    @staticmethod
    def context(log_id):
        log = Repo.log(log_id) if log_id else None
        value = (log.full_log or {}).get(DISPATCH_LOG_FIELD) if log else None
        return project_dispatch_context(value) if isinstance(value, dict) else {}

    @staticmethod
    def filter_fronts(rows, log_id=None):
        """Managed fronts can only enter the expedition frozen as their owner."""
        roots = Repo.managed_roots({r.campaign_root_id for r in rows if r.campaign_root_id})
        context = S1CampaignDispatchService.context(log_id)
        return [r for r in rows if r.campaign_root_id not in roots or (
            r.id == context.get('thread_id') and int(r.revision or 1) == context.get('source_revision'))]

    @staticmethod
    def validate_result(result, full_log):
        context = (full_log or {}).get(DISPATCH_LOG_FIELD)
        try:
            if not isinstance(result, dict):
                return ['共同任务缺少探险结果'] if context else []
            if isinstance(result.get('world_fact_updates'), list):
                result['world_fact_updates'] = project_dispatch_facts(result['world_fact_updates'], context)
            if not context:
                return []
            extract_dispatch_report((result or {}).get('world_fact_updates'), context)
        except (ValueError, KeyError, TypeError) as exc:
            return [f'共同任务战报不完整: {exc}']
        return []

    @staticmethod
    def publish_applied(candidate_ids, world_updates, applied_indexes):
        """Only acknowledge this exact source when maintenance actually applied it.

        Invoked after world writes, inside their transaction; a processed batch or
        an unrelated successful update never opens this campaign's next round.
        """
        applied_sources = {
            (world_updates[i].get('source_world_story_thread_id'),
             world_updates[i].get('source_revision'), world_updates[i].get('change_kind')): world_updates[i]
            for i in applied_indexes if 0 <= i < len(world_updates)
        }
        pending = []
        for candidate, log in Repo.completed_candidates(candidate_ids):
            context = (log.full_log or {}).get(DISPATCH_LOG_FIELD)
            if context:
                pending.append((candidate, log, context))
        owners = Repo.lock_dispatches({ctx['dispatch_id'] for _, _, ctx in pending})
        stages = Stages.states(list(owners), lock=True)
        published = Repo.published_log_ids({log.id for _, log, _ in pending})
        claims = Repo.occupancy_log_ids(list(owners))
        closed_sources = Repo.source_statuses({ctx['thread_id'] for _, _, ctx in pending
                                              if ctx.get('closed_phase') is True})
        investigation_maps = {
            row.campaign_root_id: available_stage_maps(
                stage_definition(stages[row.id].plan, stages[row.id].stage_key), stages[row.id].progress)
            for row in owners.values() if row.id in stages and stages[row.id].status == 'active'}
        active_maps = Repo.active_maps_by_root({row.campaign_root_id for row in owners.values()},
                                             investigation_maps=investigation_maps)
        for candidate, log, context in pending:
            if log.id in published:
                continue
            row = owners.get(context['dispatch_id'])
            if (not row or row.revision != context['revision']
                    or (row.active_log_id != log.id and log.id not in claims.get(row.id, set()))):
                raise ValueError('共同任务占用或轮次已变化，拒绝写回旧行动')
            report = extract_dispatch_report(candidate.world_fact_updates, context)
            report_fact = next(fact for fact in candidate.world_fact_updates
                               if isinstance(fact, dict) and 'campaign_dispatch' in fact
                               and fact.get('source_world_story_thread_id') == context['thread_id']
                               and fact.get('source_revision') == context['source_revision'])
            applied = applied_sources.get((context['thread_id'], context['source_revision'], report_fact['change_kind']))
            if not applied:
                # Roll back all attempted world writes; the failure receipt path
                # then releases only this log's claim without crediting progress.
                raise ValueError('共同任务对应世界事实尚未应用，拒绝发布战报')
            if context.get('closed_phase') is True and closed_sources.get(context['thread_id']) not in ('dormant', 'resolved'):
                raise ValueError('已收束现场不能恢复旧阶段，拒绝发布战报')
            if (context.get('closed_phase') is True and report_fact['change_kind'] == 'close'
                    and closed_sources.get(context['thread_id']) != 'dormant'):
                raise ValueError('余波 close 必须保留休眠现场，拒绝发布战报')
            if not set(report['target_dungeons']).issubset(active_maps.get(row.campaign_root_id, set())):
                raise ValueError('后续任务地图没有可推进前线，拒绝发布战报')
            state = applied.get('summary')
            if not isinstance(state, str) or not state.strip():
                raise ValueError('共同事件缺少已落库的当前世界状态')
            stage = stages.get(row.id)
            report_revision = Repo.next_report_revision(row)
            if stage:
                task = context.get('stage_task') or {}
                if (task.get('stage_key'), task.get('stage_instance')) != (stage.stage_key, stage.stage_instance):
                    raise ValueError('阶段实例已变化，拒绝旧任务写回')
                definition = stage_definition(stage.plan, stage.stage_key)
                permitted_statuses = ('active', 'ready') if row.max_active_teams > 1 else ('active',)
                if stage.status not in permitted_statuses or stage.progress.get(task.get('node_key'), {}).get('result') != 'pending':
                    raise ValueError('地图任务已结算，拒绝覆盖进度')
                stage.progress = apply_node_report(definition, stage.progress, task, report, log.id, report_revision,
                                                   discovery=context.get('discovery') is True)
            Repo.publish(row, log, report, state, staged=stage is not None,
                         revelation='revelation_context' in context, report_revision=report_revision)
            if stage:
                stage.status = 'ready' if eligible_branches(definition, stage.progress) else 'active'
                row.target_dungeons = [m for m in available_stage_maps(definition, stage.progress)
                                       if m in active_maps.get(row.campaign_root_id, set())] if stage.status == 'active' else []
                row.objective = public_stage_title(stage.plan, definition) if row.target_dungeons else '后续行动正在筹备。'
                Stages.touch(stage)

    @staticmethod
    def retry_publication(root_id):
        with db_manager.transaction():
            error = Repo.retry_publication(root_id)
            return ({'queued': True} if not error else None), error
