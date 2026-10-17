"""Pure contract for a squad's published campaign report and next invitation."""
from __future__ import annotations

from plugins.adventurer_s1.services.s1_world_outline_contract import MAX_WORLD_FACT_VALUE_CHARS

DISPATCH_LOG_FIELD = 'campaign_dispatch'
DISPATCH_FACT_FIELD = 'campaign_dispatch'
OUTCOMES = frozenset({'discovered', 'succeeded', 'failed', 'ongoing'})
TEXT_LIMITS = {'public_title': 120, 'public_summary': 1200, 'next_objective': 1200}


def _map_limit():
    from flask import current_app, has_app_context
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    key = 'ADVENTURER_S1_WORLD_CAMPAIGN_MAX_FRONTS'
    return int(current_app.config.get(key, AdventurerS1Plugin.default_config[key])
               if has_app_context() else AdventurerS1Plugin.default_config[key])


def project_dispatch_context(context: dict) -> dict:
    """Keep frozen identity, but expose task conditions only after discovery."""
    result = {key: context[key] for key in (
        'dispatch_id', 'revision', 'thread_id', 'source_revision', 'discovery',
        'objective', 'public_summary', 'current_world_state', 'allowed_dungeons', 'parallel_sites',
    ) if key in context}
    if context.get('closed_phase') is True:
        result['closed_phase'] = True
    if 'revelation_context' in context:
        from plugins.adventurer_s1.services.s1_campaign_revelation_contract import project_revelation_context
        result['revelation_context'] = project_revelation_context(context['revelation_context'])
    if context.get('discovery') is False and context.get('stage_task') and 'challenge_context' in context:
        from plugins.adventurer_s1.services.s1_campaign_challenge_contract import project_challenge_context
        result['challenge_context'] = project_challenge_context(context['challenge_context'])
    if context.get('discovery') is False and context.get('stage_task') and 'action_window_context' in context:
        from plugins.adventurer_s1.services.s1_campaign_time_contract import project_action_window_context
        result['action_window_context'] = project_action_window_context(context['action_window_context'])
    task = context.get('stage_task')
    if isinstance(task, dict) and task:
        keys = ('stage_key', 'stage_instance', 'node_key')
        if context.get('discovery') is True:
            result.pop('objective', None)
        else:
            keys += ('title', 'objective', 'success_condition', 'failure_condition')
        result['stage_task'] = {key: task[key] for key in keys if key in task}
    return result


def normalize_dispatch_report(value: object, *, discovery=False) -> dict:
    """Project unused fields away; reject ambiguous consumed data without truncation."""
    if not isinstance(value, dict):
        raise ValueError('campaign_dispatch 必须为对象')
    result = {}
    for key, limit in TEXT_LIMITS.items():
        text = value.get(key)
        if not isinstance(text, str) or len(text.strip()) > limit:
            raise ValueError(f'campaign_dispatch.{key} 无效')
        result[key] = text.strip()
    if not result['public_title'] or not result['public_summary']:
        raise ValueError('campaign_dispatch 缺少公开标题或战报')
    if not isinstance(value.get('outcome'), str) or value['outcome'] not in OUTCOMES:
        raise ValueError('campaign_dispatch.outcome 无效')
    result['outcome'] = value['outcome']
    maps = value.get('target_dungeons')
    if not isinstance(maps, list) or len(maps) > _map_limit():
        raise ValueError('campaign_dispatch.target_dungeons 无效')
    if any(not isinstance(x, str) or not x.strip() or len(x.strip()) > 100 for x in maps):
        raise ValueError('campaign_dispatch.target_dungeons 必须为地图名称数组')
    maps = [x.strip() for x in maps]
    if len(set(maps)) != len(maps) or bool(maps) != bool(result['next_objective']):
        raise ValueError('后续目标与地图必须同时非空，结束时同时为空')
    result['target_dungeons'] = maps
    if 'node_result' in value or 'node_evidence' in value:
        from plugins.adventurer_s1.services.s1_campaign_stage_contract import project_node_result
        result.update(project_node_result(value, discovery=discovery))
    return result


def extract_dispatch_report(facts: object, context: dict) -> dict:
    matches = [row for row in facts if isinstance(row, dict)
               and row.get('source_world_story_thread_id') == context['thread_id']
               and row.get('source_revision') == context['source_revision']
               and DISPATCH_FACT_FIELD in row] if isinstance(facts, list) else []
    if len(matches) != 1:
        raise ValueError('触发的共同任务必须有且只有一份同源战报')
    fact = matches[0]
    state = fact.get('summary')
    if not isinstance(state, str) or not state.strip() or len(state.strip()) > MAX_WORLD_FACT_VALUE_CHARS:
        raise ValueError('共同事件当前态必须为世界事实合同内的非空 summary')
    discovery = bool(context.get('stage_task')) and context.get('discovery') is True
    report = normalize_dispatch_report(fact[DISPATCH_FACT_FIELD], discovery=discovery)
    if not set(report['target_dungeons']).issubset(context['allowed_dungeons']):
        raise ValueError('后续任务地图超出冻结的战役范围')
    if context.get('stage_task'):
        from plugins.adventurer_s1.services.s1_campaign_stage_contract import validate_node_result
        report.update(validate_node_result(report, context['stage_task'], discovery=discovery))
        if fact.get('change_kind') not in {'advance', 'successor', 'close', 'resolve'}:
            raise ValueError('阶段行动需报告准确同源的世界事实变化')
        if context.get('closed_phase') is True and fact.get('change_kind') not in {'successor', 'close', 'resolve'}:
            raise ValueError('已收束现场只能核验余波或有据建立后继，不能原地重启旧阶段')
        if context.get('closed_phase') is True and report['node_result'] == 'pending' and fact.get('change_kind') == 'resolve':
            raise ValueError('余波调查尚未完成时用 close 保留稳定现场，有真实新局势时才建立 successor')
    else:
        report.pop('node_result', None)
        report.pop('node_evidence', None)
        if not report['next_objective'] and fact.get('change_kind') not in {'close', 'resolve'}:
            raise ValueError('结束共同任务必须同时收束对应世界线')
        if report['next_objective'] and fact.get('change_kind') not in {'advance', 'successor'}:
            raise ValueError('仍有后续行动时应推进或建立后继，不得只收束当前前线')
    if context['discovery'] and report['outcome'] != 'discovered':
        raise ValueError('首次发现必须记录发现结果')
    if not context['discovery'] and report['outcome'] == 'discovered':
        raise ValueError('已公开任务必须记录本次行动结果')
    return report


def project_dispatch_maintenance_change(facts: object, context: dict | None) -> dict | None:
    """Bind publication to the approved fact without exposing its private report."""
    if not context:
        return None
    extract_dispatch_report(facts, context)
    fact = next(row for row in facts if isinstance(row, dict)
                and row.get('source_world_story_thread_id') == context['thread_id']
                and row.get('source_revision') == context['source_revision']
                and DISPATCH_FACT_FIELD in row)
    result = {key: fact[key] for key in (
        'source_world_story_thread_id', 'source_revision', 'change_kind',
    )}
    if context.get('closed_phase') is True:
        result['closed_phase'] = True
    return result


def validate_dispatch_maintenance_changes(rows: list, required_changes: list[dict]) -> str | None:
    """Check exact approved changes before AI output enters the write transaction."""
    keys = ('source_world_story_thread_id', 'source_revision', 'change_kind')
    for required in required_changes:
        count = sum(1 for row in rows if isinstance(row, dict)
                    and all(row.get(key) == required[key] for key in keys))
        if count != 1:
            reason = 'missing' if count == 0 else 'duplicate'
            source = ','.join(f'{key}={required[key]}' for key in keys)
            return f'campaign_required_world_story_change_{reason}:{source}'
        if required.get('closed_phase') is True and required['change_kind'] == 'close':
            matched = next(row for row in rows if all(row.get(key) == required[key] for key in keys))
            if matched.get('status') != 'dormant':
                return 'campaign_closed_phase_status_mismatch'
    return None


DISPATCH_MAINTENANCE_RULE = '''
maintenance_output_contract.required_world_story_changes 是本轮共同任务发布必须消费的已审核事实引用，属于 world_story_write_authority.candidate_changes 的子集，不增加写权限。每项 candidate_id/expedition_log_id 指向 new_candidates 中的准确来源；最终 world_story_threads 必须恰好有一项逐字匹配该引用的 source_world_story_thread_id、source_revision 和 change_kind，沿既有字段合同组织该事实造成的当前态。不得遗漏、重复或把推进改成收束；同源的收束不能代替另一个后继变化，独立合法变化仍完整保留。引用只约束已成立事实的入库，不重新裁定小队成败，不决定战役下一阶段，也不授权补造事实；最终输出仍使用原 world_story_threads 形状，不回显引用数组或任务公告。
引用中的 closed_phase=true 表示来源阶段已经休眠。close 更新沿现行形状保持 status=dormant；需要终结来源时使用既有 resolve 与 status=resolved，不能退回 closing 或活动状态。successor 仅沿已审核事实建立真实新阶段，原来源仍保持终态。该标记不回显到维护输出。
'''.strip()


def project_dispatch_facts(facts: object, context: dict | None) -> list:
    """Only the exact frozen source may retain this conditional report."""
    output = []
    for fact in facts if isinstance(facts, list) else []:
        if not isinstance(fact, dict):
            output.append(fact)
            continue
        row = dict(fact)
        if not context or (
            row.get('source_world_story_thread_id'), row.get('source_revision')
        ) != (context.get('thread_id'), context.get('source_revision')):
            row.pop(DISPATCH_FACT_FIELD, None)
        elif DISPATCH_FACT_FIELD in row:
            value = row[DISPATCH_FACT_FIELD]
            if not context.get('stage_task') and isinstance(value, dict):
                value = {key: item for key, item in value.items() if key not in {'node_result', 'node_evidence'}}
            row[DISPATCH_FACT_FIELD] = normalize_dispatch_report(
                value, discovery=bool(context.get('stage_task')) and context.get('discovery') is True)
        output.append(row)
    return output


DISPATCH_CONTINUATION_RULE = 'next_objective 从本次实际结果和已知局势形成后续行动方向，可以因成败改变，也可以继续尚未完成的调查；它不是未来已发生的事实，不得预定下一队回应或结局。继续时 next_objective 与 target_dungeons 同时非空，以同源 advance/successor 保持可继续的前线；本事件确已结束时二者分别为空字符串和空数组，并以同源 close/resolve 收束。其他合法世界变化仍完整保留，仅这一个同源条目携带公告对象。未含冻结行动上下文时省略该对象。'


DISPATCH_GENERATION_RULE = f'''
冻结 current_world_state 是共同事件最近一次已经落库的世界变化；它明确改变的旧状态按时间承接，其余本地事实与既有能力边界继续保留。它是叙事因果权威，public_summary 是酒馆已公开信息；两者均不自动成为现场所有角色的知识。同源世界事实的 summary 以最多{MAX_WORLD_FACT_VALUE_CHARS}字保存本次演进后的完整当前态，承接其中仍有效的事实并移除已失效状态，不拼接历史流水，也不把后续行动方向写成已发生的事实。
议事厅行动是当前共同世界线的行动方向。只有冻结条目含 campaign_dispatch_context 时，才处理其同一 thread_id/source_revision 下的共同任务；discovery=true 时建立可传播的发现经历，后台议程只提供世界动向，不要求队伍完成它；否则本场围绕当前 objective 兑现行动，成功、失手或尚待继续都由正文经历决定，不保证成功，也不因失败抹掉已成立的世界变化。
该准确来源的一条 world_fact_updates 附带 campaign_dispatch 对象，字段为 public_title（非空公开事件标题，最多120字）、public_summary（非空公开战报，最多1200字）、outcome、next_objective（下一项公开行动目标，最多1200字）、target_dungeons（不重复的地图名称数组，最多16项，只能使用冻结 allowed_dungeons）。首次 discovery=true 时 outcome=discovered；否则 outcome=succeeded 表示当前目标达成，failed 表示正文建立失败后果，ongoing 表示仍需继续。正文必须先建立战报所述经历；公开内容只包含队伍已知且可向酒馆报告的信息，不能泄露未揭露的秘密、内部身份或后台安排。
{DISPATCH_CONTINUATION_RULE}
所有公开字段（包括后续目标与地图）须有正文建立的可传播依据或酒馆可观察的后果；队伍无法返程时只报告外界已知结果与未知范围，不为形成战报补造见证者。对象及地图数组超出上述上限视为合同错误，不能截断合法独立内容或用空值规避。
'''.strip()

DISPATCH_OBSERVER_RULE = '''
若唯一冻结世界情报条目含 campaign_dispatch_context，核对同一来源的 campaign_dispatch 公告：public_summary 和 outcome 必须来自最终 events 中实际发生的发现或行动结果，公开标题和战报只能包含队伍可知信息；next_objective 仅是本次结果支持的后续行动方向，不能宣告未来结局，target_dungeons 必须属于冻结 allowed_dungeons。按生成合同核对首次发现、成败、继续与结束的字段一致性。正文已成立而公告漏写、泄密、结果错误或后续目标不受支持时，保持 needs_regeneration=false，通过完整 override_fields.world_fact_updates 修正这份同源公告并保留其他合法变化；不得为使任务成功而改写经历。正文缺少本场必须兑现的事件时仍沿共同战役既有正文审核出口处理。
'''.strip()


PARALLEL_SITE_RULE = '''
parallel_sites=true 表示多个小队可同时处理不同现场。本场 node_result 与 node_evidence 只裁冻结 stage_task 的节点，current_world_state 是该现场最近已发布的当前态。其他现场的关联信息以已公开战报和本场实际获知为据；尚在进行的小队行动与结果未知，不能代裁其他小队节点完成状态、补写未知在途行动或宣告整个阶段与战役结束。成员力量按冻结功能、真实范围、知识与实际证据生效，有据的跨现场作用沿本轮既有世界事实字段权限照实保留，不因并行模式缩减能力范围；这不扩张场景身份、他队实体租约、世界写回或资产结算权限，也不证明远方小队已完成任务。本场 next_objective / target_dungeons 只提出有据的后续方向；后端以全部实际发布的节点证据决定开放现场与转段。是否需要后续行动取决于仍存在的目标。
'''.strip()

CLOSED_PHASE_CONTEXT_RULE = '''
closed_phase=true 表示冻结来源已经进入稳定余波，本场只执行 stage_task 尚未满足的证据条件。current_world_state 中已经发生的结果保持有效；从当前仍存在的证据、知情者及可观察后果组织调查，不能为再次接取还原旧现场。原来源不改回活动状态，未查明的远方去向保持未知。
'''.strip()

CLOSED_PHASE_OUTLINE_RULE = CLOSED_PHASE_CONTEXT_RULE + '\n本轮只规划能够核验任务条件的行动机会，不预写成败、结算或后续世界阶段。'

CLOSED_PHASE_RULE = CLOSED_PHASE_CONTEXT_RULE + '\n' + '''
若本场只能核实旧阶段，同源世界事实用 close/resolve 记录其稳定结果；close 保持 dormant 现场，resolve 表示准确来源已终结。调查证据仍不足时保留 node_result=pending 并用 close 留住稳定的余波现场，不能 resolve 掉尚待核验的任务入口。只有实际行动建立了能够独立继续的新局势时才用 successor 创建后继。任务成败仍按实际条件裁定。
'''.strip()


def dispatch_generation_rule(context):
    rule = DISPATCH_GENERATION_RULE.replace('最多16项', f'最多{_map_limit()}项')
    if context.get('stage_task'):
        from plugins.adventurer_s1.services.s1_campaign_stage_contract import STAGE_GENERATION_RULE
        rule = rule.replace(DISPATCH_CONTINUATION_RULE, STAGE_GENERATION_RULE)
    if context.get('parallel_sites') is True:
        rule += '\n' + PARALLEL_SITE_RULE
    if context.get('closed_phase') is True:
        rule += '\n' + CLOSED_PHASE_RULE
    if 'revelation_context' in context:
        from plugins.adventurer_s1.services.s1_campaign_revelation_contract import REVELATION_GENERATION_RULE
        rule += '\n' + REVELATION_GENERATION_RULE
    if 'challenge_context' in context:
        from plugins.adventurer_s1.services.s1_campaign_challenge_contract import CHALLENGE_GENERATION_RULE
        rule += '\n' + CHALLENGE_GENERATION_RULE
    if 'action_window_context' in context:
        from plugins.adventurer_s1.services.s1_campaign_time_contract import ACTION_WINDOW_RULE
        rule += '\n' + ACTION_WINDOW_RULE
    return rule


def observer_dispatch_addendum(full_log: object) -> str:
    """Project the conditional contract only when its frozen evidence is sent."""
    if not isinstance(full_log, dict):
        return ''
    snapshot = full_log.get('generation_world_intel_injection')
    if not isinstance(snapshot, dict) or not snapshot:
        snapshot = full_log.get('objective_world_intel_injection')
    if not isinstance(snapshot, dict):
        return ''
    items = snapshot.get('items')
    if not isinstance(items, list) or not any(
        isinstance(item, dict) and isinstance(item.get('campaign_dispatch_context'), dict)
        and item['campaign_dispatch_context'] for item in items
    ):
        return ''
    context = next(item['campaign_dispatch_context'] for item in items
                   if isinstance(item, dict) and item.get('campaign_dispatch_context'))
    from plugins.adventurer_s1.services.s1_campaign_stage_contract import STAGE_OBSERVER_RULE
    from plugins.adventurer_s1.services.s1_campaign_revelation_contract import REVELATION_OBSERVER_RULE
    from plugins.adventurer_s1.services.s1_campaign_challenge_contract import CHALLENGE_OBSERVER_RULE
    from plugins.adventurer_s1.services.s1_campaign_time_contract import ACTION_WINDOW_OBSERVER_RULE
    return ('\n【议事厅共同任务公告】\n' + dispatch_generation_rule(context) + '\n'
            + DISPATCH_OBSERVER_RULE + ('\n' + STAGE_OBSERVER_RULE if context.get('stage_task') else '')
            + ('\n' + REVELATION_OBSERVER_RULE if 'revelation_context' in context else '')
            + ('\n' + CHALLENGE_OBSERVER_RULE if 'challenge_context' in context else '')
            + ('\n' + ACTION_WINDOW_OBSERVER_RULE if 'action_window_context' in context else ''))
