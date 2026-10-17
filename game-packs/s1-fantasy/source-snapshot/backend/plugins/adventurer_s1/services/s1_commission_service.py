"""深渊酒馆 S1 悬赏布告栏（委托任务）服务。

与现有任务栏（``adventurer_s1_task_service``）完全独立的并行系统：DS 批量生成分级委托
（每小时补缺口 + 每天 8:00 追加一整批），玩家用贡献度接取（消耗型），把绑定冒险者派进
目标副本时按概率触发，探险 AI 判定完成，回布告栏领奖。

职责：补满生成(refill) / 每日刷新(force_refresh) / 接取(accept) / 领奖(claim) /
看板(get_board)，以及供探险 runner 调用的出发掷骰(roll_encounters_for_party) /
结算(settle_expedition) / 阵亡释放(release_for_adventurer)。
"""
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    S1PlayerVisibleError, player_operation_failure,
)
import logging
import random
import re
from contextlib import nullcontext
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Commission,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
)
from plugins.adventurer_s1.services.s1_contribution_service import CONTRIBUTION_CODE, S1ContributionService
from plugins.adventurer_s1.services.s1_reward_item_preview_service import (
    collect_reward_item_names,
    enrich_reward_item_entries,
    load_item_preview_map,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_integer

logger = logging.getLogger(__name__)

_TIERS = ('junior', 'intermediate', 'advanced')
_TIER_LABELS = {'junior': '初级', 'intermediate': '中级', 'advanced': '高级'}
_ACTIVE_STATUSES = ('accepted', 'fulfilled')
_RECONCILIATION_RECEIPT_FIELD = '_commission_reconciliation_receipt'
_LEGACY_REVIEW_FAILURE_REASONS = frozenset({
    'observer_release_authorized_quarantined',
    'required_semantic_review_infra_exhausted_quarantined',
    'required_semantic_review_response_parse_repair_exhausted_quarantined',
})


def _prepare_commission_reconciliation(log, *, lock=False):
    """按保存的结构化结果准备补结算；预览与执行共享相同的资格检查。"""
    if not log:
        return None, '探险日志不存在'
    if log.status != 'completed':
        return None, '仅可补结算已完成的探险日志'
    full_log = log.full_log
    if not isinstance(full_log, dict):
        return None, '探险日志的委托结算数据不完整'
    context = full_log.get('commission_context', [])
    results = full_log.get('commission_results', [])
    source = 'commission_results'
    if not isinstance(context, list) or not isinstance(results, list):
        return None, '探险日志的委托结算数据不完整'
    if context and not results:
        quarantine = full_log.get('_review_failure_quarantine_receipt')
        if isinstance(quarantine, dict):
            entries = quarantine.get('entries')
            if not isinstance(entries, list):
                return None, '历史委托恢复凭证不完整'
            saved = [entry for entry in entries if isinstance(entry, dict)
                     and entry.get('path') == 'commission_results']
            if saved:
                if (
                    type(quarantine.get('schema_version')) is not int
                    or quarantine.get('schema_version') != 1
                    or quarantine.get('status') != 'quarantined_unconsumed'
                    or not isinstance(quarantine.get('reason_code'), str)
                    or quarantine.get('reason_code') not in _LEGACY_REVIEW_FAILURE_REASONS
                    or len(saved) != 1
                    or not isinstance(saved[0].get('value'), list)
                ):
                    return None, '历史委托结果无法安全恢复'
                results = saved[0]['value']
                source = 'review_failure_quarantine'

    receipt = full_log.get(_RECONCILIATION_RECEIPT_FIELD)
    applied = []
    if receipt is not None:
        if (
            not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int
            or receipt.get('schema_version') != 1
            or receipt.get('expedition_log_id') != log.id
            or not isinstance(receipt.get('applied'), list)
        ):
            return None, '委托补结算记录不完整，暂无法安全重放'
        applied = receipt['applied']
    applied_ids = set()
    for entry in applied:
        cid = entry.get('commission_id') if isinstance(entry, dict) else None
        if type(cid) is not int or cid <= 0 or cid in applied_ids:
            return None, '委托补结算记录不完整，暂无法安全重放'
        applied_ids.add(cid)

    plan = {'full_log': full_log, 'source': source, 'applied': applied, 'rows': []}
    if not context or not results:
        return plan, None
    from plugins.adventurer_s1.services.adventurer_s1_parser_service import (
        _normalize_s1_commission_results,
    )
    payload = {'commission_results': results}
    if _normalize_s1_commission_results(payload, context):
        return None, '日志中的委托结果未通过结算校验'
    result_by_id = {row['commission_id']: row for row in payload['commission_results']}
    context_by_id = {}
    for row in context:
        cid, _ = normalize_ascii_integer(row.get('commission_id'), minimum=1)
        context_by_id[cid] = row

    # 参与者索引不证明存活。没有明确生命结果时保持原状，不能将全员阵亡回退成存活。
    updates = full_log.get('member_updates')
    alive_ids, seen_ids = set(), set()
    if isinstance(updates, list):
        for update in updates:
            if not isinstance(update, dict):
                continue
            mid, _ = normalize_ascii_integer(update.get('id'), minimum=1)
            if type(mid) is not int or mid <= 0:
                continue
            if mid in seen_ids:
                return None, '探险日志的成员生命结果存在重复身份'
            seen_ids.add(mid)
            dead = update.get('is_dead')
            if dead is False or (type(dead) is int and dead == 0):
                alive_ids.add(mid)
    member_ids = {row[0] for row in AdventurerS1ExpeditionLogMember.query.with_entities(
        AdventurerS1ExpeditionLogMember.adventurer_id,
    ).filter_by(expedition_log_id=log.id).all()}
    alive_ids.intersection_update(member_ids)
    candidate_ids = [cid for cid, result in result_by_id.items()
                     if cid not in applied_ids and result['completed_count'] > 0]
    if not candidate_ids:
        return plan, None
    query = AdventurerS1Commission.query.filter(
        AdventurerS1Commission.id.in_(candidate_ids),
        AdventurerS1Commission.status == 'accepted',
    ).populate_existing().order_by(AdventurerS1Commission.id)
    if lock:
        query = query.with_for_update()
    for commission in query.all():
        ctx = context_by_id[commission.id]
        adv_id, _ = normalize_ascii_integer(ctx.get('adventurer_id'), minimum=1)
        if type(adv_id) is not int or adv_id not in alive_ids:
            continue
        if commission.accepted_by_adventurer_id != adv_id:
            continue
        # 同角色取消后重接也是新批次；旧探险不能推进新接取。
        if not commission.accepted_at or not log.created_at or commission.accepted_at > log.created_at:
            continue
        required = effective_required_count(commission.required_count, commission.requirement_text)
        frozen_required, _ = normalize_ascii_integer(ctx.get('required_count'), minimum=1)
        frozen_progress, _ = normalize_ascii_integer(ctx.get('progress_count'), minimum=0)
        if 'required_count' in ctx and (type(frozen_required) is not int or frozen_required != required):
            continue
        current = max(0, _to_int(commission.progress_count, 0))
        result = result_by_id[commission.id]
        if source == 'review_failure_quarantine':
            if (type(frozen_required) is not int or type(frozen_progress) is not int
                    or frozen_progress < 0 or current < frozen_progress):
                continue
        elif type(frozen_progress) is int:
            if current != frozen_progress:
                continue
        elif 'progress_count' in ctx or result['completed_count'] < required:
            # 旧部分进度没有消费回执或冻结起点，不能区分漏记与已经结算。
            continue
        if current >= required:
            continue
        plan['rows'].append({'commission': commission, 'context': ctx, 'result': result,
                             'progress_before': current, 'required_count': required})
    if plan['rows'] and source == 'review_failure_quarantine':
        # 旧隔离器保存的是当时的 candidate，可能混入尚未通过消费的审核覆盖。
        # 审核留档是尽力写入，全部失败或次数连续也不能证明它是独立的主剧情原稿。
        # 仅识别待核查的历史项，不授权重放；已领取或不属当前接取批次的项仍跳过。
        return None, '历史委托结果缺少可确认的原始结算来源，暂不自动补结算'
    return plan, None


def _commission_reconciliation_preview(log_id, plan):
    candidates = [{
        'commission_id': entry['commission'].id,
        'title': entry['commission'].title,
        'status': entry['commission'].status,
        'progress_count': entry['progress_before'],
        'required_count': entry['required_count'],
        'adventurer_id': entry['commission'].accepted_by_adventurer_id,
    } for entry in plan['rows']]
    return {'expedition_log_id': log_id, 'candidates': candidates, 'count': len(candidates)}


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def infer_required_count_from_text(text: str | None) -> int:
    """从中文委托需求中兜底提取数量目标，避免 AI 漏填 required_count 时退成 1。"""
    if not text:
        return 1
    normalized = str(text)
    for match in re.finditer(r'(\d{1,2})\s*(?:只|个|名|份|件|枚|具|头|株|瓶|块|条|批|箱|袋)', normalized):
        count = _to_int(match.group(1), 1)
        if 1 < count <= 20:
            return count
    chinese_digit_map = {
        '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
        '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    }
    for match in re.finditer(r'([一二两三四五六七八九十]{1,3})\s*(?:只|个|名|份|件|枚|具|头|株|瓶|块|条|批|箱|袋)', normalized):
        token = match.group(1)
        if token == '十':
            count = 10
        elif token.startswith('十'):
            count = 10 + chinese_digit_map.get(token[1:], 0)
        elif token.endswith('十'):
            count = chinese_digit_map.get(token[:-1], 1) * 10
        elif '十' in token:
            left, right = token.split('十', 1)
            count = chinese_digit_map.get(left, 1) * 10 + chinese_digit_map.get(right, 0)
        else:
            count = chinese_digit_map.get(token, 1)
        if 1 < count <= 20:
            return count
    return 1


def effective_required_count(stored_count, requirement_text: str | None) -> int:
    stored = max(1, _to_int(stored_count, 1))
    if stored > 1:
        return min(20, stored)
    return max(1, min(20, infer_required_count_from_text(requirement_text)))


def _commission_public_dict(
    commission: AdventurerS1Commission,
    preview_by_name: dict[str, dict] | None = None,
) -> dict:
    data = commission.to_dict()
    data['reward_items'] = enrich_reward_item_entries(
        data.get('reward_items'),
        preview_by_name,
    )
    data['required_count'] = effective_required_count(
        commission.required_count,
        commission.requirement_text,
    )
    data['progress_count'] = max(0, _to_int(commission.progress_count, 0))
    return data


def build_commission_prompt_block(
    commission_context: list | None,
    *,
    projection: str = 'main_narrative',
) -> str:
    """按调用阶段投影本次触发的委托；无触发返回 ''。

    主剧情投影要求把委托需求融入剧情并输出 commission_results；前置裁决只读取
    目标、归属、历史进度与目标主体事实，不取得主剧情结算出口。
    高级讨伐委托的 boss 只属于悬赏布告栏，完成判定走 commission_results；
    不启用任务栏/线索追查的 hidden_boss_context 状态机。
    """
    if projection not in {'main_narrative', 'event_resolution'}:
        raise ValueError('未知的委托 Prompt 投影用途')
    if not commission_context:
        return ''
    lines = [
        (
            '【委托裁决事实（结构化只读）】：下列正式委托只提供本场目标、归属、'
            '历史进度与目标主体事实：'
            if projection == 'event_resolution'
            else '【委托事件】：本次有队员持有的悬赏委托在本副本触发，请把下列委托需求自然融入剧情：'
        )
    ]
    for ctx in commission_context:
        cid = ctx.get('commission_id')
        adv_id = ctx.get('adventurer_id')
        title = ctx.get('title') or ''
        req = ctx.get('requirement_text') or ''
        tier_label = _TIER_LABELS.get(ctx.get('tier'), ctx.get('tier'))
        required_count = effective_required_count(ctx.get('required_count'), req)
        progress_count = max(0, _to_int(ctx.get('progress_count'), 0))
        seg = f'- 委托#{cid}（接取者成员id={adv_id}，{tier_label}）：{title}。达成条件：{req}'
        seg += f'\n  · 跨探险累计进度：当前 {min(progress_count, required_count)}/{required_count}；本次只报告新增完成数。'
        if ctx.get('boss_name'):
            seg += f'\n  · 讨伐目标Boss：{ctx["boss_name"]}——{ctx.get("boss_background", "")}'
        lines.append(seg)
    if projection == 'event_resolution':
        lines.append(
            '裁决使用边界：委托接取者与目标归属保持不变；只在委托目标与威胁接触、'
            '成员行动或离场因果相交时进入前置裁决。'
            '本阶段不生成委托完成数量、完成状态、Boss 完成判定、交付或奖励结算字段。'
        )
        return '\n'.join(lines)
    lines.append(
        '判定规则：每个触发的正式委托都必须在 commission_results 中给一条结果：[{"commission_id":委托id,"completed_count":本次新增完成数,"completed":0或1}]。'
        '正文写出委托目标完成、目标物已交付/提交/上交/送达、护送已抵达、讨伐目标已击败时，必须同步输出该委托 completed=1；正文未达成也必须输出 completed_count=0、completed=0，不能省略。'
        'completed_count 只写本次剧情中该接取者实际新增完成的数量，不能包含历史进度。若本次成功讨伐上述高级讨伐 Boss，同时在状态更新 JSON 顶层输出 hidden_boss_completed=1；未成功则输出 0。'
        '接取者成员id是委托归属，剧情必须固定写这个成员的委托推进；不能把 A 接的委托写成 B 的任务，也不能把 B 的行动算作 A 的新增完成数，除非剧情明确写 B 协助且 A 仍是该委托的接取者和任务推进者。'
        '只有剧情自行生成、且没有在【委托事件】里明确接取者的临时委托/差事，才允许你按剧情自然分配给其他成员。'
        '数量目标允许部分完成并跨探险累计；completed_count 固定为本次实际新增数，不得写目标总量或累计历史值；'
        '当历史进度 + 本次新增数达到目标数时 completed=1。单一目标完成时 completed_count=1、completed=1；未推进则 completed_count=0、completed=0。'
        '多名队员都有委托时可自行安排剧情让其中部分或全部达成，未在剧情中达成的固定输出 0；'
        '队员持有委托时可用委托动机替换本次探险开头的出发动机。'
        '正式委托的交付物归属：若达成条件包含采集并交还、找回/取回、送达、上交/提交、带回作为凭证等语义，'
        'completed=1 代表接取者已把该目标物交给委托方或交到指定地点；该目标物不得再作为接取者个人战利品留在 new_equipments。'
        '如果剧情需要给玩家保留额外收益，只能写成委托目标以外的副产品、额外残片、委托方允许留下的副本或独立战利品，且名称/描述要与已交付目标物区分。'
        '不要把“可上交换取报酬”“需送达酒馆”“作为委托凭证提交”等仍待交付的物品发给玩家后又判定委托完成。'
        '悬赏布告栏高级讨伐 Boss 是 commission 系统目标，不是任务栏隐藏Boss，也不是线索追查Boss；'
        '不要因此启用【隐藏Boss模式】，不要把它写入 hidden_boss_context；只用统一的 hidden_boss_completed 表达 Boss 是否被击败。'
        '玩家可见正文只写剧情，不要出现 commission_id、completed_count、completed、概率或字段名。'
    )
    return '\n'.join(lines)


class S1CommissionService:

    # ── 配置 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _slots() -> dict:
        cfg = current_app.config.get('ADVENTURER_S1_COMMISSION_SLOTS', {})
        return {t: int(cfg.get(t, 0) or 0) for t in _TIERS}

    @staticmethod
    def _accept_cost(tier: str) -> int:
        cfg = current_app.config.get('ADVENTURER_S1_COMMISSION_ACCEPT_COST', {})
        return int(cfg.get(tier, 0) or 0)

    @staticmethod
    def _encounter_prob(tier: str) -> float:
        cfg = current_app.config.get('ADVENTURER_S1_COMMISSION_ENCOUNTER_PROB', {})
        try:
            return float(cfg.get(tier, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ── 补满生成 ──────────────────────────────────────────────────────────────

    @staticmethod
    def enqueue_refill(force_refresh: bool = False) -> bool:
        """将委托补满或全量刷新投递到异步 worker。

        对同一布告栏任务重复投递时按幂等成功处理，避免 scheduler 与管理员请求
        同时命中时把“已在执行”误报为入队失败。
        """
        from services.async_task_service import AsyncTaskService

        task_type = 'ai_s1_commission_refresh' if force_refresh else 'ai_s1_commission_refill'
        payload: dict = {}
        if AsyncTaskService.is_task_enqueued_or_running(task_type, payload):
            return True
        if AsyncTaskService.submit_task(task_type, payload):
            return True
        return AsyncTaskService.is_task_enqueued_or_running(task_type, payload)

    @staticmethod
    def enqueue_force_refresh() -> bool:
        """将每日全量刷新投递到异步 worker。"""
        return S1CommissionService.enqueue_refill(force_refresh=True)

    @staticmethod
    def enqueue_refresh_one(commission_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """校验单条委托后投递精准刷新任务。"""
        commission = (
            db_manager.query(AdventurerS1Commission.id, AdventurerS1Commission.status)
            .filter(AdventurerS1Commission.id == commission_id)
            .first()
        )
        if not commission:
            return None, '委托不存在'
        if commission.status != 'open':
            return None, '只能刷新未接取的委托'

        from services.async_task_service import AsyncTaskService

        payload = {'commission_id': int(commission_id)}
        task_type = 'ai_s1_commission_refresh_one'
        if not AsyncTaskService.is_task_enqueued_or_running(task_type, payload):
            if not AsyncTaskService.submit_task(task_type, payload):
                if not AsyncTaskService.is_task_enqueued_or_running(task_type, payload):
                    return None, '委托刷新任务入队失败，请稍后重试'
        return {'queued': True, 'commission_id': int(commission_id)}, None

    @staticmethod
    def _build_commission_from_spec(spec: dict) -> AdventurerS1Commission:
        """把（已校验的）生成 spec 物化成一条 open 委托 ORM 对象。refill / 单条精准刷新共用。"""
        tier = spec['tier']
        reward_gold = spec.get('reward_gold')
        required_count = spec.get('required_count')
        if type(reward_gold) is not int:
            raise ValueError('已校验委托的 reward_gold 必须为整数')
        if type(required_count) is not int or not 1 <= required_count <= 20:
            raise ValueError('已校验委托的 required_count 必须为 1~20 整数')
        return AdventurerS1Commission(
            tier=tier,
            task_type=spec.get('task_type', 'combat'),
            title=spec['title'],
            background=spec.get('background', ''),
            requirement_text=spec.get('requirement_text', ''),
            target_dungeons=spec.get('target_dungeons', []),
            accept_cost=S1CommissionService._accept_cost(tier),
            encounter_prob=S1CommissionService._encounter_prob(tier),
            reward_gold=reward_gold,
            reward_items=spec.get('reward_items') or None,
            required_count=required_count,
            progress_count=0,
            boss_name=spec.get('boss_name'),
            boss_background=spec.get('boss_background'),
            status='open',
        )

    @staticmethod
    def refill() -> dict:
        """每小时补满各 tier 的 open 委托到名额。无缺口不调 AI。"""
        from sqlalchemy import func

        slots = S1CommissionService._slots()
        open_counts = dict(
            db_manager.query(
                AdventurerS1Commission.tier, func.count(AdventurerS1Commission.id),
            )
            .filter(AdventurerS1Commission.status == 'open')
            .group_by(AdventurerS1Commission.tier)
            .all()
        )
        missing = {
            t: max(0, slots.get(t, 0) - int(open_counts.get(t, 0) or 0))
            for t in _TIERS
        }
        if not any(missing.values()):
            return {'generated': 0, 'missing': missing}

        from plugins.adventurer_s1.services.s1_commission_gen_ai_service import generate_commissions
        generated = generate_commissions(missing)
        if not generated:
            logger.info('委托补满：本次未生成（AI 失败或返回空），缺口=%s', missing)
            return {'generated': 0, 'missing': missing}

        created = 0
        try:
            with db_manager.transaction():
                for spec in generated:
                    db_manager.add(S1CommissionService._build_commission_from_spec(spec))
                    created += 1
        except Exception as e:
            logger.exception('委托补满写库失败')
            return {'generated': 0, 'error': str(e)}

        S1CommissionService._notify_board_changed('commission_refill')
        return {'generated': created, 'missing': missing}

    @staticmethod
    def force_refresh() -> dict:
        """刷新布告栏：删除所有 open 委托，再按各档名额补满。

        管理员手动刷新与每日 8:00 定时任务共用本方法。
        """
        try:
            with db_manager.transaction():
                deleted = (
                    AdventurerS1Commission.query
                    .filter(AdventurerS1Commission.status == 'open')
                    .delete(synchronize_session=False)
                )
        except Exception as e:
            logger.exception('强制刷新删除失败')
            return {'deleted': 0, 'generated': 0, 'error': str(e)}

        refill_result = S1CommissionService.refill()
        S1CommissionService._notify_board_changed('commission_refresh')
        return {'deleted': deleted, 'generated': refill_result.get('generated', 0)}

    @staticmethod
    def force_refresh_one(commission_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """管理员精准刷新单个 open 委托：删除它并生成 1 个同档新委托替换。

        先生成替换委托，生成失败则保留原委托不动，避免在布告栏留下空缺。
        """
        commission = AdventurerS1Commission.query.session.get(AdventurerS1Commission, commission_id)
        if not commission:
            return None, '委托不存在'
        if commission.status != 'open':
            return None, '只能刷新未接取的委托'
        tier = commission.tier

        from plugins.adventurer_s1.services.s1_commission_gen_ai_service import generate_commissions
        generated = generate_commissions({tier: 1})
        new_spec = next((s for s in generated if s.get('tier') == tier), None)
        if not new_spec:
            return None, '生成新委托失败，请稍后重试（原委托已保留）'

        try:
            with db_manager.transaction():
                removed = (
                    AdventurerS1Commission.query
                    .filter(
                        AdventurerS1Commission.id == commission_id,
                        AdventurerS1Commission.status == 'open',
                    )
                    .delete(synchronize_session=False)
                )
                if not removed:
                    return None, '该委托已被接取或已失效'
                db_manager.add(S1CommissionService._build_commission_from_spec(new_spec))
        except Exception as e:
            logger.exception('精准刷新委托失败 id=%s', commission_id)
            return None, f'刷新失败：{e}'

        S1CommissionService._notify_board_changed('commission_refresh')
        return {'deleted': commission_id, 'tier': tier}, None

    # ── 看板 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _accept_reject_reason(adv, has_active: bool, contribution: int, commission) -> Optional[str]:
        if not adv:
            return None
        if adv.status == 'dead':
            return '冒险者已阵亡'
        if adv.status == 'exploring':
            return '探险中无法接取委托'
        if has_active:
            return '已有进行中的委托'
        if contribution < int(commission.accept_cost or 0):
            return f'贡献度不足（需要 {commission.accept_cost}，当前 {contribution}）'
        return None

    @staticmethod
    def get_board(
        adventurer_id: Optional[int] = None, *, user_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """看板：各 tier 的 open 委托 + 我的进行中委托 + 我的贡献余额。"""
        adv = None
        if adventurer_id is not None:
            # 看板只消费状态；标量投影避免加载角色正文和默认资产关联。
            adv = (
                db_manager.query(AdventurerS1.id, AdventurerS1.status)
                .filter(AdventurerS1.id == adventurer_id, AdventurerS1.owner_id == user_id)
                .first()
            )
            if not adv:
                return None, '只能查看自己冒险者的委托'
        open_rows = (
            AdventurerS1Commission.query
            .filter(AdventurerS1Commission.status == 'open')
            .order_by(AdventurerS1Commission.created_at.asc())
            .all()
        )

        contribution = 0
        contribution_total = 0
        mine_row = None
        if adv is not None:
            contribution = S1ContributionService.get_balance(adventurer_id)
            contribution_total = S1ContributionService.get_total_balance(adventurer_id)
            mine_row = (
                AdventurerS1Commission.query
                .filter(
                    AdventurerS1Commission.accepted_by_adventurer_id == adventurer_id,
                    AdventurerS1Commission.status.in_(_ACTIVE_STATUSES),
                )
                .order_by(AdventurerS1Commission.id.desc())
                .first()
            )

        preview_names: set[str] = set()
        for row in open_rows:
            preview_names.update(collect_reward_item_names(row.reward_items))
        if mine_row:
            preview_names.update(collect_reward_item_names(mine_row.reward_items))
        preview_by_name = load_item_preview_map(preview_names)
        mine = _commission_public_dict(mine_row, preview_by_name) if mine_row else None

        has_active = mine is not None
        open_by_tier: dict[str, list] = {t: [] for t in _TIERS}
        for c in open_rows:
            if c.tier not in open_by_tier:
                continue
            data = _commission_public_dict(c, preview_by_name)
            if adv is not None:
                reason = S1CommissionService._accept_reject_reason(adv, has_active, contribution, c)
                data['can_accept'] = reason is None
                data['reject_reason'] = reason
            open_by_tier[c.tier].append(data)

        return {
            'open': open_by_tier,
            'mine': mine,
            'contribution': contribution,
            'contribution_total': contribution_total,
            'slots': S1CommissionService._slots(),
        }, None

    # ── 接取 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def accept(commission_id: int, adventurer_id: int,
               user_id: Optional[int] = None, is_admin: bool = False,
               ) -> Tuple[Optional[dict], Optional[str]]:
        """接取委托：扣贡献、锁定到该冒险者。一个冒险者同时只能有 1 个进行中委托。"""
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if not is_admin and user_id is not None and adv.owner_id != user_id:
            return None, '只能操作自己的冒险者'
        can, reason = adv.can_dispatch()
        if not can:
            return None, reason or '当前状态无法接取委托'

        existing = (
            AdventurerS1Commission.query
            .filter(
                AdventurerS1Commission.accepted_by_adventurer_id == adventurer_id,
                AdventurerS1Commission.status.in_(_ACTIVE_STATUSES),
            ).first()
        )
        if existing:
            return None, '已有进行中的委托，需先完成领奖'

        try:
            with db_manager.transaction():
                commission = (
                    AdventurerS1Commission.query
                    .filter_by(id=commission_id)
                    .with_for_update()
                    .first()
                )
                if not commission:
                    return None, '委托不存在'
                if commission.status != 'open':
                    return None, '该委托已被接取或已失效'

                # 事务内重查唯一性，防并发双接导致双扣贡献
                dup = (
                    AdventurerS1Commission.query
                    .filter(
                        AdventurerS1Commission.accepted_by_adventurer_id == adventurer_id,
                        AdventurerS1Commission.status.in_(_ACTIVE_STATUSES),
                    ).first()
                )
                if dup:
                    return None, '已有进行中的委托，需先完成领奖'

                cost = int(commission.accept_cost or 0)
                if cost > 0:
                    contribution = S1ContributionService.get_balance(adventurer_id)
                    if contribution < cost:
                        return None, f'贡献度不足（需要 {cost}，当前 {contribution}）'
                    _, err = S1ContributionService.spend(
                        adventurer_id, cost, source_type='commission_accept',
                        related_id=commission.id, related_type='commission',
                        description=f'接取委托「{commission.title}」', auto_commit=False,
                    )
                    if err:
                        return None, err

                commission.status = 'accepted'
                commission.accepted_by_adventurer_id = adventurer_id
                commission.accepted_by_user_id = adv.owner_id
                commission.accepted_at = _portable_utc_now()
        except Exception as e:
            return None, player_operation_failure(e, '接取失败')

        S1CommissionService._notify_board_changed('commission_accepted')
        return _commission_public_dict(commission), None

    # ── 取消 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def cancel(commission_id: int, user_id: Optional[int] = None, is_admin: bool = False,
               ) -> Tuple[Optional[dict], Optional[str]]:
        """取消已接取的委托：全额退还贡献度、重置进度、委托回到 open。仅 accepted 状态可取消。"""
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService

        commission = AdventurerS1Commission.query.session.get(AdventurerS1Commission, commission_id)
        if not commission:
            return None, '委托不存在'
        if commission.status != 'accepted':
            return None, '只有进行中的委托才能取消'
        if not is_admin and user_id is not None and commission.accepted_by_user_id != user_id:
            return None, '只有接取该委托的玩家才能取消'

        cost = int(commission.accept_cost or 0)
        try:
            with db_manager.transaction():
                locked = (
                    AdventurerS1Commission.query
                    .filter_by(id=commission_id)
                    .with_for_update()
                    .first()
                )
                if not locked or locked.status != 'accepted':
                    return None, '委托状态已变化，无法取消'

                # 退还贡献度（仅增加可消耗余额，不增加累计总额）
                if cost > 0:
                    adv_id = locked.accepted_by_adventurer_id
                    if adv_id:
                        S1CurrencyService.add(
                            adv_id, CONTRIBUTION_CODE, float(cost),
                            source_type='commission_cancel',
                            related_id=locked.id, related_type='commission',
                            description=f'取消委托「{locked.title}」退还',
                            auto_commit=False,
                        )

                locked.status = 'open'
                locked.accepted_by_adventurer_id = None
                locked.accepted_by_user_id = None
                locked.accepted_at = None
                locked.progress_count = 0
        except Exception as e:
            return None, player_operation_failure(e, '取消失败')

        S1CommissionService._notify_board_changed('commission_cancelled')
        return _commission_public_dict(commission), None

    # ── 领奖 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def claim(commission_id: int, user_id: Optional[int] = None, is_admin: bool = False,
              ) -> Tuple[Optional[dict], Optional[str]]:
        """领取已完成委托的奖励（金币 + 物品），仅接取本人。"""
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item, grant_raw_item

        commission = AdventurerS1Commission.query.session.get(AdventurerS1Commission, commission_id)
        if not commission:
            return None, '委托不存在'
        if commission.status == 'claimed':
            return None, '奖励已领取'
        if commission.status != 'fulfilled':
            return None, '委托尚未完成'
        if not is_admin and user_id is not None and commission.accepted_by_user_id != user_id:
            return None, '只有接取该委托的玩家才能领奖'

        adv_id = commission.accepted_by_adventurer_id
        adv = AdventurerS1.query.session.get(AdventurerS1, adv_id) if adv_id else None
        if not adv:
            return None, '接取该委托的冒险者不存在'

        granted_items: list[str] = []
        try:
            with db_manager.transaction():
                locked = (
                    AdventurerS1Commission.query
                    .filter_by(id=commission_id)
                    .with_for_update()
                    .populate_existing()
                    .first()
                )
                if not locked or locked.status != 'fulfilled':
                    return None, '委托状态已变化，无法领奖'

                if locked.reward_gold and locked.reward_gold > 0:
                    _, err = S1CurrencyService.add(
                        adv_id, 'gold', float(locked.reward_gold),
                        source_type='commission_reward',
                        related_id=locked.id, related_type='commission',
                        description=f'委托「{locked.title}」奖励', auto_commit=False,
                    )
                    if err:
                        return None, err

                for entry in (locked.reward_items or []):
                    name = str(entry.get('name') or '').strip()
                    qty = int(entry.get('quantity', 1) or 1)
                    if not name:
                        continue
                    if entry.get('desc') or entry.get('description') or entry.get('type') or entry.get('rarity'):
                        _, item_err = grant_raw_item(
                            adv, name,
                            {
                                'type': entry.get('type') or 'special',
                                'rarity': entry.get('rarity') or 'common',
                                'desc': entry.get('desc') or entry.get('description') or '',
                                'quantity': qty,
                            },
                            source_type='commission_reward',
                            description=f'委托「{locked.title}」奖励',
                        )
                        names = [name] * max(1, qty)
                    else:
                        names, item_err = grant_catalog_item(
                            adv, name, quantity=qty,
                            source_type='commission_reward',
                            description=f'委托「{locked.title}」奖励',
                        )
                    if item_err:
                        # 领奖是金币、全部物品与状态的一次原子结算；普通发放
                        # 错误也必须回滚，保留待领取状态，不能吞掉未到账奖励。
                        raise S1PlayerVisibleError(item_err)
                    granted_items.extend(names)

                locked.status = 'claimed'
                locked.claimed_at = _portable_utc_now()
                commission = locked
        except Exception as e:
            return None, player_operation_failure(e, '领奖失败')

        S1CommissionService._notify_board_changed('commission_claimed')
        return {
            'id': commission.id,
            'status': 'claimed',
            'reward_gold': commission.reward_gold,
            'granted_items': granted_items,
        }, None

    # ── 探险集成 ──────────────────────────────────────────────────────────────

    @staticmethod
    def roll_encounters_for_party(target_dungeon: str, member_ids: list[int]) -> list[dict]:
        """出发掷骰：为持有 accepted 委托且目标副本匹配的成员按 encounter_prob 掷骰。

        返回命中的委托上下文列表（供 runner 存入日志 + 注入 prompt）。纯掷骰+读，不改状态。
        """
        if not member_ids:
            return []
        rows = (
            AdventurerS1Commission.query
            .filter(
                AdventurerS1Commission.accepted_by_adventurer_id.in_(member_ids),
                AdventurerS1Commission.status == 'accepted',
            ).all()
        )
        context = []
        for c in rows:
            if target_dungeon not in (c.target_dungeons or []):
                continue
            if random.random() >= float(c.encounter_prob or 0.0):
                continue
            entry = {
                'commission_id': c.id,
                'adventurer_id': c.accepted_by_adventurer_id,
                'tier': c.tier,
                'task_type': c.task_type,
                'title': c.title,
                'requirement_text': c.requirement_text,
                'required_count': effective_required_count(c.required_count, c.requirement_text),
                'progress_count': max(0, _to_int(c.progress_count, 0)),
            }
            if c.boss_name:
                entry['boss_name'] = c.boss_name
                entry['boss_background'] = c.boss_background
            context.append(entry)
        return context

    @staticmethod
    def settle_expedition(commission_context: list, commission_results: list,
                          alive_member_ids: set, expedition_log_id: Optional[int] = None,
                          hidden_boss_completed: bool = False,
                          auto_commit: bool = True, notify_board: bool = True) -> dict:
        """结算委托进度：AI 返回本次完成数，后端跨探险累计，达标才转 fulfilled。"""
        if not commission_context:
            return {'updated': 0, 'fulfilled': 0}
        result_map: dict[int, dict] = {}
        for r in (commission_results or []):
            if not isinstance(r, dict):
                continue
            try:
                cid = int(r.get('commission_id'))
            except (TypeError, ValueError):
                continue
            result_map[cid] = {
                'completed': _to_int(r.get('completed'), 0),
                'completed_count': max(0, _to_int(
                    r.get('completed_count', r.get('count', r.get('progress_count'))), 0,
                )),
            }

        if hidden_boss_completed:
            for ctx in commission_context or []:
                if ctx.get('task_type') != 'subjugation' and not ctx.get('boss_name'):
                    continue
                try:
                    cid = int(ctx.get('commission_id'))
                except (TypeError, ValueError):
                    continue
                result_map.setdefault(cid, {'completed': 1, 'completed_count': 1})

        context_by_id: dict[int, int] = {}
        for ctx in commission_context:
            try:
                cid = int(ctx.get('commission_id'))
            except (TypeError, ValueError):
                continue
            result = result_map.get(cid)
            if not result or (result['completed'] != 1 and result['completed_count'] <= 0):
                continue
            adv_id = ctx.get('adventurer_id')
            if adv_id not in alive_member_ids:
                continue
            context_by_id[cid] = adv_id

        if not context_by_id:
            return {'updated': 0, 'fulfilled': 0}

        changed = False
        updated = 0
        fulfilled = 0
        try:
            transaction = db_manager.transaction() if auto_commit else nullcontext()
            with transaction:
                commissions = (
                    AdventurerS1Commission.query
                    .filter(
                        AdventurerS1Commission.id.in_(list(context_by_id.keys())),
                        AdventurerS1Commission.status == 'accepted',
                    )
                    .with_for_update()
                    .all()
                )
                for commission in commissions:
                    adv_id = context_by_id.get(commission.id)
                    if commission.accepted_by_adventurer_id != adv_id:
                        continue
                    result = result_map.get(commission.id) or {}
                    required = effective_required_count(
                        commission.required_count,
                        commission.requirement_text,
                    )
                    current = max(0, _to_int(commission.progress_count, 0))
                    increment = max(0, _to_int(result.get('completed_count'), 0))
                    if increment <= 0 and _to_int(result.get('completed'), 0) == 1:
                        increment = max(1, required - current)
                    if increment <= 0:
                        continue
                    commission.progress_count = min(required, current + increment)
                    updated += 1
                    if commission.progress_count >= required:
                        commission.status = 'fulfilled'
                        commission.fulfilled_at = _portable_utc_now()
                        commission.completed_expedition_log_id = expedition_log_id
                        fulfilled += 1
                    changed = True
        except Exception:
            logger.exception('委托结算失败 commissions=%s', list(context_by_id.keys()))
            if not auto_commit:
                raise
            return {'updated': 0, 'fulfilled': 0, 'error': 'commission_settlement_failed'}

        if changed and notify_board:
            S1CommissionService._notify_board_changed('commission_progress')
        return {'updated': updated, 'fulfilled': fulfilled}

    @staticmethod
    def preview_expedition_reconciliation(expedition_log_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """预览某条已完成日志中仍未落库的委托结算，不写数据库。"""
        log = AdventurerS1ExpeditionLog.query.session.get(AdventurerS1ExpeditionLog, expedition_log_id)
        plan, err = _prepare_commission_reconciliation(log)
        if err:
            return None, err
        return _commission_reconciliation_preview(log.id, plan), None

    @staticmethod
    def reconcile_expedition_log(expedition_log_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """锁内重核批次与生命事实，委托进度与逐日志补结算凭证原子提交。"""
        try:
            with db_manager.transaction():
                log = (AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id)
                       .populate_existing().with_for_update().first())
                plan, err = _prepare_commission_reconciliation(log, lock=True)
                if err:
                    return None, err
                preview = _commission_reconciliation_preview(log.id, plan)
                result = S1CommissionService.settle_expedition(
                    [entry['context'] for entry in plan['rows']],
                    [entry['result'] for entry in plan['rows']],
                    {entry['commission'].accepted_by_adventurer_id for entry in plan['rows']},
                    expedition_log_id=log.id, auto_commit=False, notify_board=False,
                )
                if result['updated']:
                    applied = list(plan['applied'])
                    for entry in plan['rows']:
                        commission = entry['commission']
                        if commission.progress_count <= entry['progress_before']:
                            continue
                        applied.append({
                            'commission_id': commission.id,
                            'adventurer_id': commission.accepted_by_adventurer_id,
                            'accepted_at': commission.accepted_at.isoformat(),
                            'source': plan['source'],
                            'completed_count': entry['result']['completed_count'],
                            'progress_before': entry['progress_before'],
                            'progress_after': commission.progress_count,
                        })
                    log.full_log = {**plan['full_log'], _RECONCILIATION_RECEIPT_FIELD: {
                        'schema_version': 1, 'expedition_log_id': log.id, 'applied': applied,
                    }}
        except Exception:
            logger.exception('委托补结算失败 expedition_log_id=%s', expedition_log_id)
            return None, '委托补结算失败，请查看服务端日志'
        if result['updated']:
            S1CommissionService._notify_board_changed('commission_progress')
        return {**preview, **result}, None

    @staticmethod
    def release_for_adventurer(adventurer_id: int) -> None:
        """阵亡释放：该冒险者进行中(accepted)/已完成未领(fulfilled)的委托一律置 expired。"""
        if not adventurer_id:
            return
        try:
            (AdventurerS1Commission.query
             .filter(
                 AdventurerS1Commission.accepted_by_adventurer_id == adventurer_id,
                 AdventurerS1Commission.status.in_(_ACTIVE_STATUSES),
             )
             .update({'status': 'expired'}, synchronize_session=False))
        except Exception:
            logger.exception('阵亡释放委托失败 adv=%s', adventurer_id)

    # ── 通知 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _notify_board_changed(reason: str) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed',
                          {'action': 'commission_changed', 'reason': reason},
                          namespace='/')
        except Exception:
            pass
