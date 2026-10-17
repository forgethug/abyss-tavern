"""Compact encounter draft used by the high-risk expedition pipeline.

The model proposes chronological effects. Derived outcomes are draft references;
the final narrative owns actual events and settlement outcomes.
"""
from copy import deepcopy
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator, model_validator


class _Object(BaseModel):
    model_config = ConfigDict(extra='ignore')


class _ImpactBase(_Object):
    id: Annotated[StrictInt, Field(gt=0, validation_alias=AliasChoices('id', 'member_id'))]
    healing: Annotated[StrictInt, Field(ge=0)] = 0
    terminal: Literal['none', 'durability_bypass', 'unable_to_return'] = 'none'


class _Impact(_ImpactBase):
    raw_damage: Annotated[StrictInt, Field(ge=0)]
    mitigation: Annotated[StrictInt, Field(ge=0)]


class _LegacyImpact(_ImpactBase):
    """Read only already-persisted net-damage checkpoints."""
    damage: Annotated[StrictInt, Field(ge=0)] = 0


class _Event(_Object):
    fact: Annotated[StrictStr, Field(min_length=1)]
    impacts: list[_Impact] = Field(default_factory=list, max_length=64)


class _Completion(_Object):
    core_resolved: StrictBool
    exit_resolved: StrictBool
    ending_type: Annotated[StrictStr, Field(min_length=1)]


class _Draft(_Object):
    resolution_contract_version: StrictInt
    events: list[_Event] = Field(min_length=1, max_length=64)
    completion_check: _Completion


class _LegacyEvent(_Event):
    impacts: list[_LegacyImpact] = Field(default_factory=list, max_length=64)


class _LegacyDraft(_Draft):
    events: list[_LegacyEvent] = Field(min_length=1, max_length=64)


class _NetImpact(_ImpactBase):
    damage: Annotated[StrictInt, Field(ge=0)]
    hp_after: Annotated[StrictInt, Field(ge=0)] | None = None
    fact: Annotated[StrictStr, Field(min_length=1)] | None = None

    @field_validator('fact')
    @classmethod
    def validate_personal_fact(cls, value):
        # Omission reads the historical event-level explanation. An explicitly
        # supplied explanation must be a nonempty string, not a null placeholder.
        if value is None or not value.strip():
            raise ValueError('empty_compact_draft_impact_fact')
        return value

    @model_validator(mode='before')
    @classmethod
    def restore_pure_healing_zero(cls, value):
        # v3 sparse recovery records a positive recovery; an otherwise empty
        # impact does not authorize treating unknown damage as zero.
        if (isinstance(value, dict) and 'damage' not in value
                and type(value.get('healing')) is int and value['healing'] > 0
                and value.get('terminal', 'none') == 'none'):
            return {**value, 'damage': 0}
        return value


class _NetEvent(_Event):
    impacts: list[_NetImpact] = Field(default_factory=list, max_length=64)


class _MemberSummary(_Object):
    id: Annotated[StrictInt, Field(gt=0)]
    summary: Annotated[StrictStr, Field(min_length=1)]


class _NetDraft(_Draft):
    events: list[_NetEvent] = Field(min_length=1, max_length=64)
    member_summaries: list[_MemberSummary] = Field(default_factory=list, max_length=64)


_COMPACT_SCHEMA = _NetDraft.model_json_schema()
_COMPACT_EVENT_LIMIT = _COMPACT_SCHEMA['properties']['events']['maxItems']
_COMPACT_IMPACT_LIMIT = _COMPACT_SCHEMA['$defs']['_NetEvent']['properties']['impacts']['maxItems']
_COMPACT_SUMMARY_LIMIT = _COMPACT_SCHEMA['properties']['member_summaries']['maxItems']


COMPACT_DRAFT_PROMPT = f"""你负责裁定本次遭遇每个关键阶段已经发生的作用，供正文展开为故事。世界规则、当前模式的相对实力和冻结能力是判定前提；各方选择行动，行动效果由这些前提裁定。先按全队当前状态核对成员身份和能力归属，再从交锋起点推进；每个转折使用最近已经成立的状态，不重新领取出发时的资源。气氛、对白和动作修饰留给正文。

沿【深渊酒馆·双方能力作用边界】统一判定，既有经验沿【深渊酒馆·既有知识的应用】参与行动选择。每个 fact 交代决定本阶段结果的施用者、已有来源、受作用对象与真实变化；将已耗尽或失能的作用继续视为结束，只有原条件允许且实际完成恢复才重新参与。无来源的行动不发生；主动使用他人资源须先实际取得，主动支援由能力持有者决定并实施。自动与已生效的持续作用沿原条件直接参与，无需新增主动行动。条件相同的连续作用可以合并，决定条件改变时另起阶段；impacts 记录各阶段已成立的生命变化，未改变的状态沿用，不重述无关能力。

只输出 JSON 对象，包含以下字段；资产疑点反馈为独立可选字段：
- resolution_contract_version：整数 3。
- events：按实际时间排列的关键转折数组。每项先写 fact，交代共同的行动来源、相遇与局势变化；再写 impacts，逐一交付各实际受作用成员的个人交互。每个 impacts 条目先写准确 id，再写 fact，最后写 damage 与可选 healing、terminal。条目内 fact 只交代这名成员所受作用、当时实际防护及生效后的后果，紧接着的数值记录同一次个人交互的实际净损耗与有效恢复。个人交互只在本人的条目表达，不在共同 fact 另写一份；没有生命损耗但防护或应对实际发生，也可用 damage=0 保留该人的过程。没有个人交互时 impacts 为 []。
- member_summaries：按准确 id 完整覆盖本队，每项包含 id 与 summary；事件完成后只归纳该成员仍存的影响与后续行动条件。生命变化数值沿各阶段 impacts，不在总结重算或补写。按【生命变化归纳】回看全部事件，漏记的作用在实际发生阶段修正，后续状态随之衔接；总结不补造事件、能力或治疗。
- completion_check：core_resolved、exit_resolved 两个布尔值，以及简短 ending_type。核心冲突已实际解决才令 core_resolved=true，核心仍可继续施压则为 false。exit_resolved 表示本队此次去向已完整成立：仍能归队者已实际离场，其余成员的不可逆终态已经形成时为 true；还有成员的离场处境待定则为 false。本场完整结束可以与核心仍存在同时成立，分别记录。

数组容量：events 为 1 至 {_COMPACT_EVENT_LIMIT} 项，每项 impacts 为 0 至 {_COMPACT_IMPACT_LIMIT} 项且每名实际受作用成员最多一项；member_summaries 按输入成员顺序完整覆盖，每名成员恰好一项，上限 {_COMPACT_SUMMARY_LIMIT}。上限不是目标篇幅。优先省去重复叙述，只合并条件未改变的连续作用；独立转折、成员与实际生命变化完整保留，不截断记录或为减少条数改变先后与结果。

每个 impacts 条目按生命变化的承受者归集，条目内 fact 为非空字符串：id 是实际承受本条生命作用或形成独立终态的本队成员，damage 是该成员本阶段经过适用防护后实际损失的 HP，healing 是该成员已经得到的有效恢复。先确定结果作用于谁，再用该人的准确成员 id 记录；施用者只有自身生命或终态也实际变化时才有对应条目。对队外主体造成的损伤保留在事实中，成员自身实际支付的生命代价另记在该成员条目。damage 为必填非负整数，healing 可省略零值。独立作用分别核对适用防护与后果，再按同一承受者、同一阶段汇总；持续作用与主动发动的代价不因本阶段主要攻击被阻断而漏记。未作用于生命的资源、疲惫不填入 damage。同一转折每位成员最多一项。

普通生命损伤只交付 damage、healing，不需要 terminal。大纲 resolution_modes 绑定的来源类别沿【普通伤害与生命判定】决定生命通道，impacts 记录本阶段实际触发的作用：先由接触与实际防护确定净损耗，再与当前生命比较生还或死亡。damage 保留本阶段全部净损耗，不截成剩余 HP，也不按所求生死反算。后续追击和持续作用按实际发生另行记录。

terminal 仅供独立于普通伤害的终态使用：此前已定义的不依赖 HP 的直接致死机制在本阶段实际触发，填写 durability_bypass，并在 fact 指明原机制及触发事实；已符合当前状态规则的不可逆失踪或滞留填写 unable_to_return。其它情况省略或填 none。它不表示伤势严重程度或结束本场行动。

纯恢复条目在 healing 为正整数且 terminal 缺省或为 none 时，可以省略 damage 表示零损耗；其它条目仍须交付 damage。

实际防护的来源、覆盖、次数、持续与耗尽写在相关 fact 中；只有功能适用且实际承担的部分才减免，同一有限防护继承真实余量。先裁定本次接触经过这些防护后的实际 HP 损耗，再填写 damage；该数值已完成减伤，与成员 HP 使用同一量纲，直接扣除，不再减伤或换算。damage 可以超过当前生命或生命上限，不能为保留行动或治疗机会而截断。

每条 impacts 在个人 fact、damage 与 healing 之后写 hp_after，作为下一次个人交互的起点。当前 HP 取入场 hp 或该成员上一条 hp_after：damage >= 当前 HP 时，hp_after=0，该阶段死亡，后续停止主动行动、治疗和生命扣减；damage < 当前 HP 且没有独立终态时，hp_after=min(max_hp, 当前 HP-damage+healing)。独立终态沿原合同归零。恢复先于攻击时拆成前后两个阶段，死亡以后发生的治疗不计入。未变化成员沿用上次值。个人事实的生死、后续行动与这个余额一致；只在条目内记录一次余额，共同 fact 和 member_summaries 不重复计算。

"""


COMPACT_REVIEW_PROMPT = """你负责核对服务端指定成员已经发生的生命作用与结果是否一致。输入保留全队资源和事件，以核实实际支援；指定成员的接触事实由底稿提供。冻结资产说明可能具备的功能，事件和原触发条件决定哪些功能已经生效；本次审核以已生效作用为依据，不以资产清单的使用完整程度为依据。随附的冻结对抗依据与底稿生成阶段相同，用于判断实际作用的适用、覆盖和减免；能力及强度沿冻结依据核对，候选底稿不提供新增能力的权限。不重新设计主体、调整其强度、替角色选择更优行动或评价伤亡人数。没有不可逆结果时后端不会调用你。
member_outcomes 中的 id 与 member_ref 固定绑定同一成员；按该代号读取 event_facts，并按同一 id 读取资产和 hp_trace。event_facts 每项的 event_fact_index 是服务端给定的零基编号，text 保留完整候选事件；与 hp_trace 关联及报告问题均使用该编号，相同文本的不同事件仍独立。hp_trace 中的损耗与恢复均归属该成员；先核对作用的实际承受者，再对应轨迹，施用者身份本身不说明其自身生命发生变化。数组位置只表达此次待审集合的排列，不重新编号人物。summary 仅归纳该成员剩余影响，实际过程由所引用事件确定，生命数值读取 hp_trace，不要求事实或总结重述。已有净损耗仍须符合冻结作用与实际防护，不能仅因填在数值字段中就认定正确。
沿 event_facts 的时间核对已经自动、持续或实际发动的防护与治疗，及逐成员实际接触、来源、对象、持续、次数和耗尽。自动作用由已有条件与本步事实确定是否触发，再按实际功能判定改变哪次作用；后一次成立的防护不返改此前已经承受的接触。主动作用须有已发动的事件依据；仅有可用资源或可选择的更优行动时，该作用仍未发生，不作为漏计。hp_trace.changes 中 damage_taken 是适用防护后的净伤害，healing_received 是实际有效恢复，当前 HP 由后台沿事件顺序计算。输入若保留原始伤害与减免分解则按已有事实核对，不要求补造缺失分解。已成立且覆盖该成员此次作用的资源必须计入。治疗的受益对象和恢复类型沿原功能匹配，只有匹配的实际损耗才得到恢复。共享作用继承真实余量，生命结果按【普通伤害与生命判定】核对；底稿写出的死亡标签不是独立证据，不能用它解释掉事实与生命变化的矛盾。
只输出 JSON，含 verdict、reviewed_member_ids、reviewed_ledger_ids、issue_event_fact_indexes、reason。两个 reviewed 数组准确覆盖服务端范围；本版本 reviewed_ledger_ids 为空。已成立资源完整计入且生命时间线相容时 verdict=confirmed、issue_event_fact_indexes=[]；只有遗漏已生效资源、越权来源或时序、重复消耗同一余量或事实与生命结果的冲突会改变指定终态时，verdict=retry_causal_draft 并引用具体零起始事件索引及原因。每项纠错说明已有来源、当时已成立的触发或发动事实、与候选结果的具体冲突；缺少其中依据的可能行动不形成纠错意见。复核只要求重新推演，不提供事件、伤害或生命补丁。
"""


from plugins.adventurer_s1.services.s1_description_feedback_service import FEEDBACK_PROMPT, normalize_feedback
COMPACT_DRAFT_PROMPT += "\n" + FEEDBACK_PROMPT


def parse_compact_draft(data, member_ids, max_hp, current_hp, *, failure_feedback=None):
    if failure_feedback is not None:
        failure_feedback.clear()
    return _parse_compact_draft(data, member_ids, max_hp, current_hp,
                                failure_feedback=failure_feedback)


def _parse_compact_draft(data, member_ids, max_hp, current_hp, *, legacy_checkpoint=False, net_changes=True,
                         failure_feedback=None, normalize_terminal_labels=True):
    from utils.ai_field_names import normalize_ai_field_names
    schema = _LegacyDraft if legacy_checkpoint else (_NetDraft if net_changes else _Draft)
    compatible = normalize_ai_field_names(data, schema.model_json_schema())
    if compatible.issues:
        return None, f'compact_draft_field_name_conflict: {compatible.issues[:3]}'
    try:
        proposal = schema.model_validate(compatible.data).model_dump()
    except ValidationError as exc:
        return None, f'invalid_compact_draft_schema: {exc.errors(include_url=False)[:3]}'
    if proposal['resolution_contract_version'] != 3:
        return None, 'unsupported_compact_draft_version'
    if not proposal['completion_check']['ending_type'].strip():
        return None, 'empty_compact_draft_ending'
    if len(set(member_ids)) != len(member_ids):
        return None, 'duplicate_frozen_member_id'
    summaries = proposal.get('member_summaries', [])
    if summaries and (
        len(summaries) != len(member_ids)
        or {row['id'] for row in summaries} != set(member_ids)
        or any(not row['summary'].strip() for row in summaries)
    ):
        return None, 'invalid_compact_draft_member_summaries'
    # Consume the local explanation once, in the same indexed event. Review,
    # retry, main and checkpoints retain their existing canonical fact shape;
    # restoring a normalized checkpoint never appends the explanation again.
    for event in proposal['events']:
        facts = [event['fact'].strip()]
        if not facts[0]:
            return None, 'empty_compact_draft_fact'
        for impact in event['impacts']:
            personal_fact = impact.pop('fact', None)
            if personal_fact is not None:
                if not personal_fact.strip():
                    return None, 'empty_compact_draft_impact_fact'
                facts.append(f"成员 id={impact['id']}：{personal_fact.strip()}")
        event['fact'] = '\n'.join(facts)
    outcomes = {}
    terminal_conflicts = {}
    hint_conflicts = []

    def remember_hint_conflict(mid, index, reported, expected):
        hint_conflicts.append({
            'member_id': mid, 'issue_event_fact_index': index,
            'prior_candidate_hp_trace': deepcopy(outcomes[mid]['hp_trace']),
            'reported_hp_after': reported, 'effects_hp_after': expected,
            'reason': '本步生命备忘与候选已经交付的净作用不一致，核对作用与备忘并使后续一致。',
        })

    def hint_failure(index):
        first = hint_conflicts[0]
        if failure_feedback is not None:
            failure_feedback.update({
                **first,
                'candidate_events': deepcopy(proposal['events'][:index + 1]),
                'state_hint_conflicts': deepcopy(hint_conflicts),
            })
        return None, (f"compact_draft_state_hint_mismatch member={first['member_id']} "
                      f"event={first['issue_event_fact_index']}")

    for mid in member_ids:
        initial, ceiling = current_hp.get(mid), max_hp.get(mid)
        if type(initial) is not int or type(ceiling) is not int or not 0 < initial <= ceiling:
            return None, f'invalid_compact_draft_frozen_hp member={mid}'
        outcomes[mid] = {'id': mid, 'remaining_hp': initial, 'is_dead': False,
                         'death_reason': '', 'direct_terminal_pressure_index': None,
                         'hp_trace': {'starting_hp': initial, 'changes': [],
                                      'durability_bypass_triggered': False,
                                      'life_state_basis': 'survived'}}
    for index, event in enumerate(proposal['events']):
        event['fact'] = event['fact'].strip()
        if not event['fact']:
            return None, 'empty_compact_draft_fact'
        seen = set()
        for impact in event['impacts']:
            mid = impact['id']
            state_hint = impact.pop('hp_after', None)
            if mid not in outcomes or mid in seen:
                if hint_conflicts:
                    return hint_failure(index)
                return None, f'invalid_compact_draft_member member={mid} event={index}'
            seen.add(mid)
            row = outcomes[mid]
            healing, terminal = impact['healing'], impact['terminal']
            if legacy_checkpoint or net_changes:
                damage = impact['damage']
                breakdown = {}
            else:
                raw_damage, mitigation = impact['raw_damage'], impact['mitigation']
                if mitigation > raw_damage:
                    return None, f'compact_draft_mitigation_exceeds_damage member={mid} event={index}'
                damage = raw_damage - mitigation
                breakdown = {'raw_damage': raw_damage, 'mitigation': mitigation}
            if damage == 0 and healing == 0 and terminal == 'none' and not breakdown.get('raw_damage'):
                if state_hint is not None and state_hint != row['remaining_hp']:
                    remember_hint_conflict(mid, index, state_hint, row['remaining_hp'])
                continue
            if row['is_dead']:
                if mid not in terminal_conflicts:
                    terminal_conflicts[mid] = {
                        'member_id': mid,
                        'issue_event_fact_index': index,
                        'terminal_event_fact_index': row['hp_trace']['changes'][-1]['event_fact_index'],
                        'candidate_remaining_hp': row['remaining_hp'],
                        'prior_candidate_hp_trace': deepcopy(row['hp_trace']),
                        'reason': '此前候选作用已形成生命终态，此处却继续提交生命变化。',
                    }
                # Diagnose independent members in the same candidate. Invalid
                # post-terminal effects never update state or form a result.
                continue
            hp = max(0, row['remaining_hp'] - damage)
            if normalize_terminal_labels and hp == 0 and terminal == 'durability_bypass':
                # Match the existing v2 normalization: ordinary net damage has
                # independently exhausted HP, so a redundant bypass label is
                # not needed to establish this same terminal result. Preserve
                # the proposed damage and facts for resource/causal review.
                terminal = 'none'
                impact['terminal'] = terminal
            ended = hp == 0 or terminal != 'none'
            if ended and healing:
                if hint_conflicts:
                    return hint_failure(index)
                if failure_feedback is not None:
                    conflict = {
                        'member_id': mid,
                        'issue_event_fact_index': index,
                        'terminal_event_fact_index': index,
                        'candidate_remaining_hp': 0,
                        'prior_candidate_hp_trace': deepcopy(row['hp_trace']),
                        'reason': '同一条候选作用先形成终态，却又给该成员记录后续恢复。',
                    }
                    failure_feedback.update({
                        **conflict,
                        'candidate_events': deepcopy(proposal['events'][:index + 1]),
                        'terminal_conflicts': [conflict],
                    })
                return None, f'compact_draft_healing_after_terminal member={mid} event={index}'
            hp = 0 if ended else min(max_hp[mid], hp + healing)
            if state_hint is not None and state_hint != hp:
                remember_hint_conflict(mid, index, state_hint, hp)
            row['remaining_hp'] = hp
            row['is_dead'] = ended
            row['hp_trace']['changes'].append({'event_fact_index': index,
                **breakdown, 'damage_taken': damage, 'healing_received': healing})
            if ended:
                row['death_reason'] = event['fact']
                row['hp_trace']['life_state_basis'] = terminal if terminal != 'none' else 'ordinary_hp_exhausted'
                row['hp_trace']['durability_bypass_triggered'] = terminal == 'durability_bypass'
        if hint_conflicts:
            # Diagnose every independent member at the first inconsistent
            # stage, but never advance a contradictory candidate to later stages.
            return hint_failure(index)
    if terminal_conflicts:
        conflicts = list(terminal_conflicts.values())
        first = conflicts[0]
        if failure_feedback is not None:
            last_index = max(row['issue_event_fact_index'] for row in conflicts)
            failure_feedback.update({
                **first,
                'candidate_events': deepcopy(proposal['events'][:last_index + 1]),
                'terminal_conflicts': conflicts,
            })
        return None, (f"compact_draft_effect_after_terminal member={first['member_id']} "
                      f"event={first['issue_event_fact_index']}")
    feedback = normalize_feedback(data.get('asset_description_feedback')) if isinstance(data, dict) else []
    return {**({'asset_description_feedback': feedback} if feedback else {}),
            **({} if legacy_checkpoint else {'_damage_breakdown_version': 2 if net_changes else 1}),
            **({'member_summaries': summaries} if net_changes else {}),
            'resolution_contract_version': 3, 'events': proposal['events'],
            'event_facts': [e['fact'] for e in proposal['events']],
            'member_outcomes': list(outcomes.values()),
            'completion_check': proposal['completion_check']}, None


def validate_compact_narrative_result(result, draft):
    """Check identity and result shape, never compare final outcomes to a draft.

    Main validation owns max HP and semantic review owns final-event consistency.
    Draft disagreement is neither a failure nor permission to overwrite the story.
    """
    expected = {row['id']: row for row in draft['member_outcomes']}
    rows = result.get('member_updates')
    if not isinstance(rows, list):
        return ['compact_draft_member_updates_missing']
    actual = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] in actual:
            return ['compact_draft_member_identity_invalid']
        actual[row['id']] = row
    errors = []
    if set(actual) != set(expected):
        errors.append('compact_draft_member_identity_mismatch')
    for mid in expected:
        other = actual.get(mid, {})
        hp, dead = other.get('remaining_hp'), other.get('is_dead', False)
        if type(hp) is not int or hp < 0:
            errors.append(f'narrative_hp_invalid member={mid}')
        if type(dead) is not bool or (type(hp) is int and dead != (hp == 0)):
            errors.append(f'narrative_life_state_invalid member={mid}')
    completion = result.get('completion_check')
    if not isinstance(completion, dict):
        errors.append('compact_draft_completion_missing')
    else:
        for key in ('core_resolved', 'exit_resolved'):
            if type(completion.get(key)) is not bool:
                errors.append(f'narrative_completion_invalid field={key}')
    return errors


def restore_compact_draft(value, members):
    """Re-derive a trusted checkpoint against departure HP, never model outcomes."""
    if not isinstance(value, dict) or value.get('resolution_contract_version') != 3:
        return None
    breakdown_version = value.get('_damage_breakdown_version')
    if '_damage_breakdown_version' in value and (type(breakdown_version) is not int or breakdown_version not in (1, 2)):
        raise ValueError('invalid_compact_draft_damage_breakdown_version')
    restored, error = _parse_compact_draft(
        value, [m.id for m in members],
        {m.id: m.max_hp for m in members}, {m.id: m.hp for m in members},
        legacy_checkpoint=breakdown_version is None,
        net_changes=breakdown_version == 2,
        # A checkpoint and its review receipt bind the already-normalized
        # historical representation. Verify it without migrating that label.
        normalize_terminal_labels=False,
    )
    if error:
        raise ValueError(f'invalid_compact_draft_checkpoint: {error}')
    if any(restored[key] != value.get(key) for key in restored):
        raise ValueError('compact_draft_checkpoint_derivation_mismatch')
    dead_ids = sorted(row['id'] for row in restored['member_outcomes'] if row['is_dead'])
    if dead_ids:
        review = value.get('_resource_review')
        reviewed_ids = review.get('reviewed_member_ids') if isinstance(review, dict) else None
        if not (
            isinstance(review, dict)
            and review.get('verdict') == 'confirmed'
            and isinstance(reviewed_ids, list)
            and all(type(mid) is int for mid in reviewed_ids)
            and sorted(reviewed_ids) == dead_ids
            and review.get('reviewed_ledger_ids') == []
            and review.get('issue_event_fact_indexes') == []
        ):
            raise ValueError('compact_draft_checkpoint_resource_review_missing')
        restored['_resource_review'] = deepcopy(review)
    return restored
