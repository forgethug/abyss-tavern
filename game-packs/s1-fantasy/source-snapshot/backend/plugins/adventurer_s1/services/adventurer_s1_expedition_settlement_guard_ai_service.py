"""S1 探险结算 JSON 轻量修正 AI。

只读取结算 JSON 与轻量上下文，不读取剧情全文；用于在 member_updates 落库前修正
明显的结构/设定字段问题。AI 调用失败由调用方隔离关联写入并保留正文事实。

读路径优化：先走确定性审计/修正（无损），仅在无法安全推断时才调用 AI。
"""
import copy
import json
import logging
import math
import re
import requests
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import AIProfileAssignmentError, resolve_s1_ai_profile_snapshot
from plugins.adventurer_s1.services.s1_ai_player_name_service import frozen_player_names, post_s1_ai_request_with_player_aliases as post_s1_ai_request
from plugins.adventurer_s1.services.s1_expedition_ai_timeout_contract import S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS
from plugins.adventurer_s1.services.s1_ai_json_response_service import S1AiResponseContractResult, parse_s1_ai_http_json_response, parse_s1_ai_json_content
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_finite_number, normalize_ascii_integer, normalize_exact_boolean
from plugins.adventurer_s1.services.s1_existing_asset_update_contract import build_s1_existing_asset_context_by_member, normalize_s1_existing_asset_updates
from plugins.adventurer_s1.services.s1_gold_settlement_contract import has_current_s1_gold_settlement_contract
from flask import current_app
from db_repository import db_manager
from plugins.adventurer_s1.services.s1_ai_prompt_shared import S1_SKILL_TRAIT_BOUNDARY_RULES, S1_EXPEDITION_REVIEW_RESOURCE_AUTHORITY_BOUNDARY
from plugins.adventurer_s1.services.s1_settlement_field_projection import project_s1_description_aliases, project_s1_member_settlement_fields, project_s1_top_settlement_fields, project_s1_settlement_fields
from plugins.adventurer_s1.services.adventurer_s1_config_service import resolve_s1_special_event_kind
from plugins.adventurer_s1.services.adventurer_s1_parser_service import _S1_CURRENT_MEMBER_UPDATE_FIELDS, _S1_LEGACY_MEMBER_UPDATE_FIELDS, _S1_MEMBER_FINITE_NUMBER_FIELDS, _S1_MEMBER_INTEGER_FIELDS, repair_illegal_item_type, repair_illegal_rarity
from plugins.adventurer.services.adventurer_parser_service import _normalize_removed_name_list_payload
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
from plugins.adventurer_s1.services.s1_item_use_condition_service import build_use_condition_map, class_transfer_invitation_matches_dungeon, is_class_transfer_invitation, project_member_snapshot_for_expedition_ai
from plugins.adventurer_s1.services.s1_visible_name_policy import is_internal_code_identifier_name
logger = logging.getLogger(__name__)

class _DeferredSettlementGuardTokenUsage:
    """把一次 Guard token 记录延迟到完整结算消费链得出最终结果。"""

    def __init__(self, *, usage: dict, model: str, party_id: int, expedition_log_id: int, attempt_number: int) -> None:
        self._usage = usage
        self._model = model
        self._party_id = party_id
        self._expedition_log_id = expedition_log_id
        self._attempt_number = attempt_number
        self._finalized = False

    def finalize(self, *, is_success: bool) -> None:
        if self._finalized:
            return
        self._finalized = True
        record_token_usage(call_type='expedition_settlement_guard_ai', usage=self._usage, model=self._model, party_id=self._party_id, expedition_log_id=self._expedition_log_id, attempt_number=self._attempt_number, is_success=is_success)

class _SettlementGuardAIResult(dict):
    """保持既有 dict 契约，同时携带不进入 JSON 的延迟 token 句柄。"""

    def __init__(self, value: dict, *, deferred_token_usage: _DeferredSettlementGuardTokenUsage | None=None) -> None:
        super().__init__(value)
        self.deferred_token_usage = deferred_token_usage
_GUARD_SNAPSHOT_TRAIT_KEYS = frozenset({'name', 'description'})
_GUARD_SNAPSHOT_EQUIP_KEYS = frozenset({'asset_id', 'name', 'type', 'rarity', 'quantity', 'custom_desc', 'description', 'uses_left', 'evolution_stage', 'recovery_minutes', 'recovering', 'immutable'})
_GUARD_SNAPSHOT_SKILL_KEYS = frozenset({'asset_id', 'name', 'description', 'equipped', 'recovery_minutes', 'recovering'})
_STRAY_MEMBER_UPDATE_KEYS = frozenset({'internal_debug', 'raw_prompt', 'prompt', 'system', 'user', 'assistant', 'completion_check', 'member_updates', 'events', 'summary', 'debug'})
_POLLUTION_IN_VISIBLE_NAME_RE = re.compile('^(member_updates|new_traits|new_skills|new_equipments|remaining_hp|gold_change|rarity|completion_check|prompt_tokens|prompt|system|user|assistant)\\b', re.IGNORECASE)
_FAILED_SPECIAL_DIRECTIVES = frozenset({'new_class_failed', 'advance_class_failed', 'remove_trait_failed'})
_GUARD_RESULT_ALLOWED_FIELDS = frozenset({'decision', 'member_updates_patch', 'top_level_patch', 'retry_feedback'})
_SETTLEMENT_GUARD_SYSTEM_PROMPT = S1_SKILL_TRAIT_BOUNDARY_RULES + '\n\n' + S1_EXPEDITION_REVIEW_RESOURCE_AUTHORITY_BOUNDARY + '\n\n你是【深渊酒馆 S1 结算 JSON 修正员】。\n\n任务：只检查并修正探险 AI 输出的结算 JSON 是否适合落库。你不会看到剧情全文，也不得凭空补发剧情收益。\n\n你只允许依据输入中的：结算 JSON、出发成员快照、地牢/特殊事件/隐藏 Boss/委托/特殊副本上下文，修正明显字段错误。\n\n总原则：凡结算 JSON 不符合 S1 的字段设计、数据类型、玩家可见文本规范、成员归属边界或轻量上下文约束，都必须处理。能用现有结算内容和快照安全修正的，输出 patched；集合中的错误新增或错误变化无法安全恢复时，删除该项并保留其它合法结算，仍输出 patched。唯一例外是 `new_equipments` 的成员归属、名称集合与 `quantity`：本守卫只能按下方专用规则保留，不能删除、改名、转移或改数。只有不能通过字段修正或删除隔离、继续落库仍会污染玩家数据时，才输出 regenerate，但不得仅因本守卫无权二次裁决实物权益而重试。\n\nS1 member_update 设计字段只允许表达这些含义：成员 id、生命/死亡状态与原因、评分、四维/最大 HP 成长、职业变化、装备道具获得与资产扣减、获得/移除特质、获得/移除技能、按冻结资产 ID 修改既有装备/技能、稳定恢复触发、好感变化、心理状态、本次冒险史摘要、隐藏剧情线更新。除此以外的陌生业务字段、调试字段、prompt 字段、JSON 结构残片或解释性清单，不应进入玩家数据；若已经混入可见字段，要清理或打回。\n\n玩家可见字段规范：装备名、特质名、技能名必须是自然的游戏内名称；描述和 history_story_append 必须是自然中文叙事。名称不得使用 snake_case、UPPER_SNAKE_CASE、lowerCamelCase 内部代码标识或文件路径；英文枚举只允许留在 type/rarity 等协议字段值中。不得出现英文 schema 字段名、JSON 语法、代码片段、布尔值标签、概率/后台判定、prompt/system/user/assistant/DM 暗示、内部审核/结算/模型说明。\n\n允许修正：\n- member_updates 中现有成员的字段错位、格式错误、明确错误的描述、非法 rarity/type。`new_equipments` 的成员归属、名称集合与 `quantity` 已由上游完整剧情审核负责，本守卫不得新增、删除、改名、跨成员转移或改数；如需修正现有同成员同名条目的 type/rarity/明确错误描述，必须保留完整原集合，后端还会机械恢复上述权益字段且不会回流 Observer。仅缺失、空白或不完整的描述不由本守卫补写；新特质与新技能留给结算后的独立描述补全链，物品没有对应补全结果时仍原样保留。\n- 陌生字段或错位字段：若字段明显是内部调试/JSON/schema/prompt 产物，且不属于 S1 结算设计，必须移除；若其内容明显应该归入已有设计字段（如描述误写进 name、数量误写进 desc、特质描述误写成字符串列表），应归位到正确字段。\n- 名称/描述污染：特质名、技能名、装备名不得是英文字段名、JSON key、整段说明、带冒号的清单标题或 `{}`/`[]` 片段；能提取自然名称就提取，不能提取时从对应完整集合中删除污染条目并保留其它合法项，不得仅因此 regenerate。`new_equipments` 名称是本规则的例外：不得提取、改名或删除，只能保留原键并修正同键的非权益元数据；名称本身仍非法时交给后端最终 validator 阻断，不能由本守卫猜测身份。\n- 数据类型和取值：quantity 必须为正整数；rarity 只能 common/rare/epic/legendary；type 必须是合理物品类型；removed_traits/removed_skills 应是名称列表；consumed_equipments 只能是 `[{"name":"稳定原名","quantity":正整数}]`。数值字段不得写成说明文字。\n- 结算字段结构：new_equipments 固定为 `{名称: {type, rarity, desc, quantity}}`；new_traits 固定为 `[{name, desc, asset_dependencies}]`，逐项保留已成立的依赖，独立特质也须有明确的空数组，缺失证据时不得猜补；new_skills 固定为 `[{name, desc}]`。skill_updates/equipment_updates 固定为按冻结资产 ID 绑定的完整数组；relationship_changes 应只引用本队成员 id，score/delta 为克制数值；mental_state 只能是 1~2 个自然中文状态词，不得是句子或字段名。\n- 数量字段边界：数量必须已经写进 quantity，name 或 desc 不应夹带数量文案；本守卫没有剧情全文，不得从名称或描述提取、推断或重写 quantity，也不得把缺失或非法 quantity 兜成 1。若清理其它字段会触及 `new_equipments`，只能在同成员同名条目内修正非权益元数据；后端确定性保留原集合、原所有者与原 quantity，不为此增加任何 AI 调用。\n- 明显物品 type 误标：只依据候选完整描述、当前合法 type 与冻结快照中的精确 predecessor 身份选择系统英文白名单值，不得依据名称词表或孤立关键词猜测。明确按份或按次数耗尽的实体才使用一次性类型；可长期保留和反复使用的实体使用与完整功能一致的持久类型；精确 predecessor 保持同一长期用途时继承其冻结 type。当前 type 已合法但完整证据不足时保持原值；当前 type 非法且无法进一步细分时使用 `special`，不得仅因此 regenerate。`new_equipments[*].uses_left` 缺失是合法输入，后端会按图鉴声明或 type 初始化；本守卫不得把缺失改写成 null，也不得在没有上游明确依据时猜测初始次数。\n- `recovery_minutes` 与 `recovery_triggers` 的语义补漏已由上游完整剧情审核负责。你没有剧情全文，不主动判断、增加、删除或改变恢复时长与恢复触发；专用更新省略恢复时长时后端只沿用同 ID 出发快照中的原值，禁止从名称或描述猜测。\n- 既有装备发生本体变化且仍是同一实体时使用 `equipment_updates`：每项输出 `equipment_id/name/rarity/desc/evidence_event_indexes`，ID 精确命中该成员出发快照，名称可以变化，数量不得在此修改。`uses_left` 只在上游候选已有直接依据并唯一确定次数变化时保留非负整数或 null；缺失时后端继承同 ID 冻结值，表示没有可证明的次数变化。本守卫没有剧情全文，无法判断是否用过或无法唯一确定扣减量时必须省略，不得猜测。不要输出没有本职责修改权限的 `type/evolution_stage`；后端按精确 ID 从冻结快照补齐并无条件覆盖。只有本次候选已经明确改变稳定恢复时长时才保留 `recovery_minutes`，否则省略并由后端沿用冻结值。\n- 现有 `equipment_updates` 若只有结构或白名单错误，可输出该字段完整修正版。返程名称精确撞上同成员另一件冻结装备时属于稳定 ID 身份冲突；你没有剧情全文，不得把当前条目猜测改绑到另一 ID，而应从该成员完整数组覆盖中移除冲突条目，保留其它合法项和两件装备的出发状态。缺少精确 ID、返程完整状态或事件依据时，同样不按同名、近名或描述猜测，也不改扣其它装备或资源；移除不确定条目并输出 patched，不得仅因此 regenerate。只有原实例彻底消失、一拆多或转赠时才是 consumed + new。\n- 成员终态不改变本场装备变化的身份和证据要求。合法 `equipment_updates` 先结算并进入回执，后端随后没收终态成员的剩余随身装备；不能仅因死亡而清空已成立的变化。技能行不在装备没收范围内。\n- 既有技能返程状态变化或改名只允许 `skill_updates`：每项必须包含 `skill_id/name/desc/evidence_event_indexes`，只有本次候选已经明确改变稳定恢复时长时才保留 `recovery_minutes`，否则省略并由后端沿用同 ID 冻结值。ID 精确命中该成员出发快照，描述为返程完整当前效果。`new_skills` 只表示真正新学会，`removed_skills` 只表示旧实例永久消失。你没有剧情全文，不能凭名称猜测或自行补造技能代价；结构化专用数组中缺少精确 ID、完整返程状态或事件依据的条目，从该成员完整 `skill_updates` 覆盖中移除，保留其它合法条目和出发技能状态并输出 patched。不得仅因技能代价漏记或技能修改无法安全确定而输出 regenerate；只有另有独立阻断问题时才按该问题处理。\n- 明确技能代价可以由上游完整剧情审核落入 `new_traits`、`skill_updates`、`equipment_updates` 或已有外部资源字段。你没有剧情全文，不能自行判断技能是否实际发动，也不能新增、迁移或替换代价；只审核现有结算的结构与冻结身份。若某项代价变更无法安全保留，移除该不确定变更并输出 patched，禁止随机选择其它装备、技能、特质、HP、金币或物品补扣，也不得仅因代价遗漏或无法确定而输出 regenerate。\n- 两个专用修改数组可为空；非空时同一资产 ID 最多一条，条目数不得超过该成员快照中的对应资产数。修补只能完整覆盖该字段并保留其它独立合法项，不得截断或合并。\n- `new_traits` / `new_skills` 只有名称或描述不完整时保留原条目，不按名称补造通用描述，也不删除已经成立的新增；结算后的独立描述补全链会依据冻结证据处理，补全失败也不回流本守卫。若 member_snapshot 已有该成员精确同名技能，且没有独立新学依据，不得把它继续留在 new_skills；不得改用 removed_skills 配对来覆盖旧技能。\n- 冻结装备 `immutable=true` 的条目不允许 `equipment_updates`；移除这类字段修改并保留其它合法项，不通过 consumed + new 同名重建绕过。正常使用及实际次数消耗继续遵循现有消费合同。\n- new_skills/new_traits 错栏：按共同规则【技能与特质分界】patched 迁移或删除。\n- history_story_append 已存在但含字段名、JSON 片段或内部说明时，只能在不增加事实的前提下清理表达；本任务没有剧情全文，字段缺失、为空或无法从原文无损还原时不得新写叙事，保持原值并继续处理其它结算，不得仅因此 regenerate。\n- 内部 prompt、概率、后台标签、DM/user/system 字样泄露到玩家可见字段时，改成世界观内表述。\n- 英文字段名或 JSON 片段污染到玩家可见字段时必须清理；字段名、结构片段和协议标签都不得进入特质名、技能名、装备名、描述或 history_story_append。集合条目无法无损还原时删除该条目并输出 patched；但 `new_equipments` 仍按上方权益例外保留原成员、原键与原 quantity，只修同键非权益元数据。其它非集合结算字段无法清理或隔离时才 decision=regenerate，history_story_append 的缺失、空白或不可无损补写按上一条放行。\n- commission_results 只能引用输入 commission_context 中存在的正式委托；明显不存在的委托结果应移除。\n- progress_protocol=count_authoritative_v2 的正式委托以 completed_count 作为唯一新增进度依据；completed 只是达标一致性标记，不能把 completed=1 当作缺失数量的替代值，也不得凭空补数量。\n- 正式委托交付物的最终实物集合已由能读取完整剧情的上游 Observer 审核。本守卫不根据 requirement_text、物品名称或描述再次删除、改名或重分配 `new_equipments`，也不为此 decision=regenerate；这里只校验 commission_results 的结构与其它本职责字段，避免同一实物归属在下游被二次改判。\n- 共同带回实物的 unassigned_items 由上游 Observer 绑定全文证据后登记原出发小队待分配池。本守卫没有相应全文和分配权限，不新增、删除、改名、改数量、分给成员或折现；保留上游审核结果与回执，沿既有职责修正其它独立结算字段。\n- 特殊机缘副本字段：若 context.special_dungeon_context.directives 标记某成员为 *_failed 或没有对应成功指令，不得出现对应 new_class / new_skills / removed_traits 产出；若标记 remove_trait，removed_traits 必须来自该成员出发特质栏，不得用 new_traits 轻症化替代。\n- 正式转职邀请函消费由后端确定性前置步骤依据图鉴结构化副本条件、成员出发持有状态、本次副本与非空 new_class 托管：先剥离模型写入的全部正式邀请函，再只补扣确实持有且匹配本次副本的一件。本守卫 AI 不得根据职业名、description 或剧情猜测这类 consumed_equipments。\n- 职业通用技能卷轴漏扣：本规则同样只比较出发快照与 member_update。若出发装备包含稳定原名“职业通用技能卷轴”，出发快照已有职业或本次 member_update.new_class 非空，并且 member_update 新增了与该有效职业相关的 new_skills，则认定使用了 1 份卷轴。若 consumed_equipments 尚未扣除它，在 member_updates_patch 输出完整修正版 consumed_equipments，保留原有合法条目并补入“职业通用技能卷轴”quantity=1；已经扣除不得重复扣。无论新增多少个相关技能最多补扣 1 份；没有有效职业、没有新增技能或技能与职业无关时不扣。技能与职业的相关性只根据结构化职业名和技能名/描述判断，不需要剧情全文。\n- 跨成员特质归属：出发快照中的每条既有特质只属于其 adventurer_id。若某成员的 new_traits 或 removed_traits 精确引用了另一成员快照中的既有特质名、且自己快照没有该名称，不得把该变化留在当前成员名下；无法仅凭轻量上下文确定应否迁回原成员时，不迁移、不猜测，从当前成员完整字段中删除该错误变化并输出 patched。\n- 隐藏剧情线字段：story_threads 若出现，status 只能是 active/dormant/resolved 等生命周期状态，title/summary 必须是自然中文，不能把本次字段 JSON 或 prompt 文本写进去；无法安全修正则移除该条或 regenerate。\n- 隐藏剧情线实体引用：story_threads[*].entity_refs 是已由生成上下文与后端校验的稳定身份锚点；本守卫不掌握完整世界实体快照，不判断 NPC/势力是否“应该”绑定，也不得根据名称、M 编号或正文猜测、添加、删除、替换 world_actor_ids/world_story_thread_ids/faction_keys。修正同一 story_threads 条目的其它字段时必须原样保留合法 entity_refs；仅当 entity_refs 本身不是对象或其三个已知字段不是数组时，在该成员的 member_updates_patch.story_threads 完整数组中只移除损坏的 entity_refs，保留该剧情线其它合法字段及其它合法剧情线，decision=patched。\n\n禁止修正：\n- 不得根据想象新增剧情中才可能成立的金币、物品、特质、技能、职业。\n- 你没有剧情全文，也不接收 `gold_settlement` 修正权限；不得新增、删除、重分配或改写任何金币分量。金币归属由上游完整剧情审核与后端合同负责。\n- 属性成长只处理字段本身结构非法或明显越界，不按奖励多少调整。remaining_hp 与 is_dead 继承最终正文的生命结果，由完整剧情审核与后端一致性校验处理；本任务没有剧情全文，不修正这两个字段，也不为此提出补丁或重新生成要求。\n- 不得新增非本队成员；不得改变成员 id。\n- 不得把全文剧情审核规则硬套到本任务：你没有剧情全文，不能凭空判断“剧情应该给谁奖励”。只处理结算字段自身和轻量上下文已经能确认的问题。\n如果可以修正，输出 patched；确实无法安全修正时输出 regenerate，并在 retry_feedback 中说明当前结算的阻断依据。\n只有确认不存在需要修正或重试的风险时才输出 allow；不得用无人消费的解释字段附带未解决风险。\n\n输出纯 JSON：\n{\n  "decision": "allow|patched|regenerate",\n  "member_updates_patch": [],\n  "top_level_patch": {},\n  "retry_feedback": "<仅 decision=regenerate 时填写>"\n}\n\nmember_updates_patch 每项必须含本队正整数 id，并只包含需要修改的 S1 成员结算字段。\ntop_level_patch 只允许 hidden_boss_completed、commission_results、completion_check。\ndecision=allow 时两个 patch 必须为空且 retry_feedback 为空；decision=patched 时至少一个 patch 非空且 retry_feedback 为空；decision=regenerate 时两个 patch 必须为空且 retry_feedback 非空。'
_CURRENT_ALLOWED_MEMBER_PATCH_FIELDS = _S1_CURRENT_MEMBER_UPDATE_FIELDS - {'recovery_triggers', 'remaining_hp', 'is_dead'}
_LEGACY_ALLOWED_MEMBER_PATCH_FIELDS = _S1_LEGACY_MEMBER_UPDATE_FIELDS - {'gold_change', 'used_equipments', 'used_skills'}
_ALLOWED_MEMBER_PATCH_FIELDS = _CURRENT_ALLOWED_MEMBER_PATCH_FIELDS | _LEGACY_ALLOWED_MEMBER_PATCH_FIELDS
_ALLOWED_TOP_LEVEL_PATCH_FIELDS = frozenset({'hidden_boss_completed', 'commission_results', 'completion_check'})

def _slim_dict_row(row: dict | None, allowed_keys: frozenset) -> dict:
    if not isinstance(row, dict):
        return {}
    return project_s1_description_aliases({k: row[k] for k in allowed_keys if k in row and row[k] is not None}, description_key='description', alias_keys=('custom_desc',))

def _compress_guard_member_snapshot(member_snapshot: list | None, *, use_condition_by_name: dict[str, dict | None] | None=None, target_dungeon: str | None=None) -> list:
    """Guard 只需出发职业/特质/装备/技能与 id，省略 HP/性格等无关字段。"""
    member_snapshot = project_member_snapshot_for_expedition_ai(member_snapshot, use_condition_by_name=use_condition_by_name, target_dungeon=target_dungeon)
    compressed: list[dict] = []
    for row in member_snapshot or []:
        if not isinstance(row, dict):
            continue
        try:
            adv_id = int(row.get('adventurer_id'))
        except (TypeError, ValueError):
            continue
        traits = [_slim_dict_row(t, _GUARD_SNAPSHOT_TRAIT_KEYS) for t in row.get('traits') or [] if isinstance(t, dict) and str(t.get('name') or '').strip()]
        equipment = [_slim_dict_row(e, _GUARD_SNAPSHOT_EQUIP_KEYS) for e in row.get('equipment') or [] if isinstance(e, dict) and str(e.get('name') or '').strip()]
        skills = [_slim_dict_row(s, _GUARD_SNAPSHOT_SKILL_KEYS) for s in row.get('skills') or [] if isinstance(s, dict) and str(s.get('name') or '').strip()]
        compressed.append({'adventurer_id': adv_id, 'character_class': str(row.get('character_class') or '').strip(), 'traits': traits, 'equipment': equipment, 'skills': skills})
    return compressed

def _compress_guard_context(context: dict | None) -> dict:
    if not isinstance(context, dict):
        return {}
    out: dict = {}
    out['special_event_kind'] = resolve_s1_special_event_kind(context)
    commissions = context.get('commission_context')
    if isinstance(commissions, list):
        slim_commissions = []
        for row in commissions:
            if not isinstance(row, dict):
                continue
            entry = {}
            for key in ('commission_id', 'id', 'title', 'requirement_text', 'adventurer_id', 'acceptor_adventurer_id', 'required_count', 'progress_count', 'commission_source', 'reward_settlement', 'progress_protocol'):
                if key in row and row[key] is not None:
                    entry[key] = row[key]
            if entry:
                slim_commissions.append(entry)
        if slim_commissions:
            out['commission_context'] = slim_commissions
    sdc = context.get('special_dungeon_context')
    if isinstance(sdc, dict):
        slim_sdc = {}
        if sdc.get('reward_kind'):
            slim_sdc['reward_kind'] = sdc['reward_kind']
        if sdc.get('directives'):
            slim_sdc['directives'] = sdc['directives']
        if slim_sdc:
            out['special_dungeon_context'] = slim_sdc
    hbc = context.get('hidden_boss_context')
    if isinstance(hbc, dict):
        slim_hbc = {k: hbc[k] for k in ('boss_name', 'commission_id', 'clue_item_name', 'triggered') if k in hbc and hbc[k] is not None}
        if slim_hbc:
            out['hidden_boss_context'] = slim_hbc
    return out

def _build_guard_user_payload(*, target_dungeon: str, result_data: dict, member_snapshot: list | None, context: dict | None, use_condition_by_name: dict[str, dict | None] | None=None) -> dict:
    result_data = project_s1_settlement_fields(result_data)
    return {'target_dungeon': target_dungeon, 'member_snapshot': _compress_guard_member_snapshot(member_snapshot, use_condition_by_name=use_condition_by_name, target_dungeon=target_dungeon), 'context': _compress_guard_context(context), 'settlement_json': {'hidden_boss_completed': result_data.get('hidden_boss_completed'), 'commission_results': result_data.get('commission_results') or [], 'completion_check': result_data.get('completion_check'), 'member_updates': result_data.get('member_updates') or []}}

def _snapshot_by_adventurer_id(member_snapshot: list | None) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in member_snapshot or []:
        if not isinstance(row, dict):
            continue
        try:
            out[int(row.get('adventurer_id'))] = row
        except (TypeError, ValueError):
            continue
    return out

def _snapshot_trait_names(snapshot_row: dict | None) -> set[str]:
    if not isinstance(snapshot_row, dict):
        return set()
    names: set[str] = set()
    for trait in snapshot_row.get('traits') or []:
        if isinstance(trait, dict):
            name = str(trait.get('name') or '').strip()
            if name:
                names.add(name)
    return names

def _member_update_by_id(result_data: dict, member_id: int) -> dict | None:
    for update in result_data.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        try:
            if int(update.get('id')) == member_id:
                return update
        except (TypeError, ValueError):
            continue
    return None

def _valid_commission_ids(context: dict | None) -> set[int]:
    ids: set[int] = set()
    for row in (context or {}).get('commission_context') or []:
        if not isinstance(row, dict):
            continue
        for key in ('commission_id', 'id'):
            try:
                ids.add(int(row[key]))
            except (TypeError, ValueError, KeyError):
                continue
    return ids

def _looks_like_polluted_visible_name(name: str | None) -> bool:
    text = str(name or '').strip()
    if not text:
        return False
    if is_internal_code_identifier_name(text):
        return True
    if _POLLUTION_IN_VISIBLE_NAME_RE.match(text):
        return True
    if text.startswith('{"') or text.startswith('[{'):
        return True
    return False

def _iter_named_collection_entries(value):
    if isinstance(value, dict):
        for name, data in value.items():
            yield (str(name), data)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield (item, None)
            elif isinstance(item, dict):
                yield (str(item.get('name') or ''), item)

def _find_unfixable_guard_issues(result_data: dict, member_snapshot: list | None, context: dict | None) -> list[str]:
    """返回必须交给 Guard AI 处理的阻断项；空列表表示可确定性处理或放行。"""
    from plugins.adventurer_s1.services.adventurer_s1_prompt_service import _parse_special_dungeon_directive
    issues: list[str] = []
    snapshots = _snapshot_by_adventurer_id(member_snapshot)
    sdc = (context or {}).get('special_dungeon_context') or {}
    directives = sdc.get('directives') or {}
    for mid_raw, directive_raw in directives.items() if isinstance(directives, dict) else []:
        try:
            mid = int(mid_raw)
        except (TypeError, ValueError):
            continue
        kind, _trait_name = _parse_special_dungeon_directive(directive_raw)
        update = _member_update_by_id(result_data, mid)
        if not update:
            continue
        if kind in _FAILED_SPECIAL_DIRECTIVES:
            if update.get('new_class') or update.get('character_class'):
                issues.append(f'member {mid} special directive {kind} but has new_class')
            if kind == 'remove_trait_failed' and (update.get('removed_traits') or update.get('new_traits')):
                issues.append(f'member {mid} remove_trait_failed but has trait changes')
            if kind in ('new_class_failed', 'advance_class_failed') and update.get('new_skills'):
                issues.append(f'member {mid} class-failed directive but has new_skills')
        elif kind == 'remove_trait':
            removed = update.get('removed_traits') or []
            allowed = _snapshot_trait_names(snapshots.get(mid))
            for item in removed:
                name = item if isinstance(item, str) else str(item.get('name') or '') if isinstance(item, dict) else ''
                name = name.strip()
                if name and allowed and (name not in allowed):
                    issues.append(f'member {mid} removed_trait not in snapshot: {name}')
    for idx, update in enumerate(result_data.get('member_updates') or []):
        if not isinstance(update, dict):
            continue
        try:
            update_mid = int(update.get('id'))
        except (TypeError, ValueError):
            update_mid = None
        own_trait_names = _snapshot_trait_names(snapshots.get(update_mid))
        other_trait_names = set().union(*(_snapshot_trait_names(row) for mid, row in snapshots.items() if mid != update_mid)) if snapshots else set()
        for field_name in ('new_traits', 'removed_traits'):
            for name, _data in _iter_named_collection_entries(update.get(field_name)):
                trait_name = str(name or '').strip()
                if trait_name and trait_name not in own_trait_names and (trait_name in other_trait_names):
                    issues.append(f'member {update_mid} {field_name} references another member trait: {trait_name}')
        for field_name in ('new_traits', 'new_skills', 'new_equipments'):
            for name, _data in _iter_named_collection_entries(update.get(field_name)):
                if _looks_like_polluted_visible_name(name):
                    issues.append(f'member_updates[{idx}].{field_name} polluted name: {name[:40]}')
    return issues

def _normalize_new_traits_for_guard(raw) -> tuple[object | None, bool]:
    """只做结构清洗；缺失描述保留给结算后的描述补全链。"""
    changed = False
    if raw is None:
        return (None, False)
    if isinstance(raw, dict):
        out = {}
        for raw_name, data in raw.items():
            name = str(raw_name or '').strip()
            if not name:
                changed = True
                continue
            if name != raw_name:
                changed = True
            if isinstance(data, str):
                desc = data.strip()
                if desc != data:
                    changed = True
                out[name] = desc
            elif isinstance(data, dict):
                entry = dict(data)
                for desc_key in ('description', 'desc'):
                    if isinstance(entry.get(desc_key), str):
                        cleaned = entry[desc_key].strip()
                        if cleaned != entry[desc_key]:
                            entry[desc_key] = cleaned
                            changed = True
                out[name] = entry
            else:
                out[name] = {'description': ''}
                changed = True
        return (out, changed)
    if isinstance(raw, list):
        out_list = []
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
                if not name:
                    changed = True
                    continue
                out_list.append(name)
                if name != item:
                    changed = True
            elif isinstance(item, dict):
                entry = dict(item)
                name = str(entry.get('name') or '').strip()
                if not name:
                    changed = True
                    continue
                if name != entry.get('name'):
                    entry['name'] = name
                    changed = True
                for desc_key in ('description', 'desc'):
                    if isinstance(entry.get(desc_key), str):
                        cleaned = entry[desc_key].strip()
                        if cleaned != entry[desc_key]:
                            entry[desc_key] = cleaned
                            changed = True
                out_list.append(entry)
            else:
                changed = True
        return (out_list, changed)
    return (raw, False)

def _normalize_new_skills_for_guard(raw) -> tuple[object | None, bool]:
    """Normalize shape without inventing descriptions owned by backfill AI."""
    changed = False
    if not isinstance(raw, list):
        return (raw, False)
    out_list = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
            if not name:
                changed = True
                continue
            out_list.append({'name': name, 'description': ''})
            changed = True
        elif isinstance(item, dict):
            entry = dict(item)
            name = str(entry.get('name') or '').strip()
            if not name:
                changed = True
                continue
            if name != entry.get('name'):
                entry['name'] = name
                changed = True
            for desc_key in ('description', 'desc'):
                if isinstance(entry.get(desc_key), str):
                    cleaned = entry[desc_key].strip()
                    if cleaned != entry[desc_key]:
                        entry[desc_key] = cleaned
                        changed = True
            out_list.append(entry)
        else:
            changed = True
    return (out_list or None, changed)

def _normalize_new_equipments_for_guard(raw) -> tuple[dict | None, bool]:
    changed = False
    if not isinstance(raw, dict):
        return (None, False)
    out: dict = {}
    for name, data in raw.items():
        item_name = str(name or '').strip()
        if not item_name or not isinstance(data, dict):
            continue
        entry = dict(data)
        rarity = repair_illegal_rarity(entry.get('rarity'))
        if rarity != entry.get('rarity'):
            changed = True
        entry['rarity'] = rarity
        item_type = repair_illegal_item_type(entry.get('type'))
        if item_type != entry.get('type'):
            changed = True
        entry['type'] = item_type
        out[item_name] = entry
    return (out or None, changed)

def _build_deterministic_guard_patch(result_data: dict, member_snapshot: list | None, context: dict | None) -> dict:
    """构建与 Guard AI 相同结构的 patched 输出；无改动时 member_updates_patch 为空。"""
    member_patches: list[dict] = []
    valid_commissions = _valid_commission_ids(context)
    raw_comm_results = result_data.get('commission_results') or []
    filtered_comm_results = []
    comm_changed = False
    if isinstance(raw_comm_results, list):
        for row in raw_comm_results:
            if not isinstance(row, dict):
                continue
            cid = row.get('commission_id', row.get('id'))
            try:
                cid_int = int(cid)
            except (TypeError, ValueError):
                comm_changed = True
                continue
            if valid_commissions and cid_int not in valid_commissions:
                comm_changed = True
                continue
            filtered_comm_results.append(row)
    top_level_patch: dict = {}
    if comm_changed:
        top_level_patch['commission_results'] = filtered_comm_results
    snapshot_by_id = _snapshot_by_adventurer_id(member_snapshot)
    for update in result_data.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        try:
            adv_id = int(update.get('id'))
        except (TypeError, ValueError):
            continue
        patch: dict = {'id': adv_id}
        local_changed = False
        new_eqs, eq_changed = _normalize_new_equipments_for_guard(update.get('new_equipments'))
        if eq_changed and new_eqs is not None:
            patch['new_equipments'] = new_eqs
            local_changed = True
        new_traits, trait_changed = _normalize_new_traits_for_guard(update.get('new_traits'))
        if trait_changed and new_traits is not None:
            patch['new_traits'] = new_traits
            local_changed = True
        new_skills, skill_changed = _normalize_new_skills_for_guard(update.get('new_skills'))
        if isinstance(new_skills, list):
            snapshot_skills = {str(row.get('name') or '').strip(): str(row.get('description') or row.get('desc') or '').strip() for row in (snapshot_by_id.get(adv_id) or {}).get('skills') or [] if isinstance(row, dict) and str(row.get('name') or '').strip()}
            removed_skills = set(_normalize_removed_name_list_payload(update.get('removed_skills')) or [])
            guarded_skills = []
            for row in new_skills:
                if not isinstance(row, dict):
                    guarded_skills.append(row)
                    continue
                name = str(row.get('name') or '').strip()
                old_desc = snapshot_skills.get(name)
                if old_desc is None:
                    guarded_skills.append(row)
                    continue
                if name not in removed_skills:
                    skill_changed = True
                    continue
                desc = str(row.get('description') or row.get('desc') or '').strip()
                if old_desc and (not desc):
                    row = dict(row)
                    row['description'] = old_desc
                    row.pop('desc', None)
                    skill_changed = True
                guarded_skills.append(row)
            new_skills = guarded_skills
        if skill_changed and new_skills is not None:
            patch['new_skills'] = new_skills
            local_changed = True
        stray_keys = [k for k in update.keys() if k in _STRAY_MEMBER_UPDATE_KEYS]
        if stray_keys:
            local_changed = True
        if local_changed:
            member_patches.append(patch)
    return {'member_updates_patch': member_patches, 'top_level_patch': top_level_patch}

def _normalized_consumed_equipment_entries(raw) -> list[dict]:
    """把历史字符串与当前对象统一为 Guard 可回写的稳定扣减列表。"""
    rows = raw if isinstance(raw, list) else [raw]
    merged: list[dict] = []
    index_by_name: dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get('name') or '').strip()
            try:
                quantity = int(row.get('quantity', 1))
            except (TypeError, ValueError):
                quantity = 0
        else:
            name = str(row or '').strip()
            quantity = 1
        if not name or quantity <= 0:
            continue
        if name in index_by_name:
            merged[index_by_name[name]]['quantity'] += quantity
            continue
        index_by_name[name] = len(merged)
        merged.append({'name': name, 'quantity': quantity})
    return merged

def _class_transfer_consumption_patches(result_data: dict, member_snapshot: list | None, target_dungeon: str | None) -> dict[int, list[dict]]:
    """返回正式邀请函的后端权威扣减字段；只有非空 new_class 才补扣。"""
    snapshot_by_id = _snapshot_by_adventurer_id(member_snapshot)
    relevant_updates: list[tuple[int, dict, dict, bool]] = []
    item_names: set[str] = set()
    for update in result_data.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        try:
            adv_id = int(update.get('id'))
        except (TypeError, ValueError):
            continue
        snapshot = snapshot_by_id.get(adv_id) or {}
        current_consumed = _normalized_consumed_equipment_entries(update.get('consumed_equipments'))
        relevant_updates.append((adv_id, update, snapshot, bool(str(update.get('new_class') or '').strip())))
        item_names.update((str(item.get('name') or '').strip() for item in snapshot.get('equipment') or [] if isinstance(item, dict) and str(item.get('name') or '').strip()))
        item_names.update((row['name'] for row in current_consumed))
    condition_by_name = build_use_condition_map(item_names, independent_read=True)
    patches: dict[int, list[dict]] = {}
    for adv_id, update, snapshot, has_new_class in relevant_updates:
        current_consumed = _normalized_consumed_equipment_entries(update.get('consumed_equipments'))
        formal_names: set[str] = {row['name'] for row in current_consumed if is_class_transfer_invitation(condition_by_name.get(row['name']))}
        candidates: set[str] = set()
        for item in snapshot.get('equipment') or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            try:
                quantity = int(item.get('quantity', 1))
            except (TypeError, ValueError):
                quantity = 1
            if quantity <= 0:
                continue
            condition = condition_by_name.get(name)
            if is_class_transfer_invitation(condition):
                formal_names.add(name)
            if class_transfer_invitation_matches_dungeon(condition, target_dungeon):
                candidates.add(name)
        canonical_consumed = [row for row in current_consumed if row['name'] not in formal_names]
        if has_new_class and candidates:
            canonical_consumed.append({'name': sorted(candidates)[0], 'quantity': 1})
        if canonical_consumed != current_consumed:
            patches[adv_id] = canonical_consumed
    return patches

def has_unresolved_class_transfer_consumption(result_data: dict, member_snapshot: list | None, target_dungeon: str | None) -> bool:
    """正式转职邀请函扣减是否仍偏离后端权威结果。"""
    patches = _class_transfer_consumption_patches(result_data, member_snapshot, target_dungeon)
    return bool(patches)

def _profession_skill_scroll_review_member_ids(result_data: dict, member_snapshot: list | None) -> set[int]:
    """返回仍需 Guard 判断职业技能卷轴关系的精确成员集合。"""
    snapshot_by_id = _snapshot_by_adventurer_id(member_snapshot)
    member_ids: set[int] = set()
    for update in result_data.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        try:
            adv_id = int(update.get('id'))
        except (TypeError, ValueError):
            continue
        snapshot = snapshot_by_id.get(adv_id) or {}
        equipment = [item for item in snapshot.get('equipment') or [] if isinstance(item, dict) and str(item.get('name') or '').strip()]
        consumed_names = {row['name'] for row in _normalized_consumed_equipment_entries(update.get('consumed_equipments'))}
        if update.get('new_skills') and any((str(item.get('name') or '').strip() == '职业通用技能卷轴' and '职业通用技能卷轴' not in consumed_names for item in equipment)):
            member_ids.add(adv_id)
    return member_ids

def _needs_profession_skill_scroll_ai_review(result_data: dict, member_snapshot: list | None) -> bool:
    """职业技能卷轴仍需根据技能与职业的语义关系交给 Guard AI。"""
    return bool(_profession_skill_scroll_review_member_ids(result_data, member_snapshot))

def try_deterministic_settlement_guard(result_data: dict, member_snapshot: list | None, context: dict | None, target_dungeon: str | None=None) -> dict | None:
    """无损确定性 Guard：可安全处理则返回 Guard 形结果；返回 None 表示必须调 AI。"""
    if not isinstance(result_data, dict):
        return {'decision': 'allow', 'member_updates_patch': [], 'top_level_patch': {}, 'retry_feedback': ''}
    blockers = _find_unfixable_guard_issues(result_data, member_snapshot, context)
    if blockers:
        return None
    effective_target_dungeon = str(target_dungeon or (context or {}).get('target_dungeon') or '').strip()
    class_transfer_patches = _class_transfer_consumption_patches(result_data, member_snapshot, effective_target_dungeon)
    if _needs_profession_skill_scroll_ai_review(result_data, member_snapshot):
        return None
    patch_payload = _build_deterministic_guard_patch(result_data, member_snapshot, context)
    if class_transfer_patches:
        patch_by_id = {int(patch.get('id')): patch for patch in patch_payload['member_updates_patch'] if isinstance(patch, dict) and str(patch.get('id') or '').isdigit()}
        for adv_id, consumed in sorted(class_transfer_patches.items()):
            patch = patch_by_id.get(adv_id)
            if patch is None:
                patch = {'id': adv_id}
                patch_payload['member_updates_patch'].append(patch)
                patch_by_id[adv_id] = patch
            patch['consumed_equipments'] = consumed
    has_patches = bool(patch_payload['member_updates_patch'] or patch_payload['top_level_patch'])
    has_stray = any((key in _STRAY_MEMBER_UPDATE_KEYS for update in result_data.get('member_updates') or [] if isinstance(update, dict) for key in update.keys()))
    if has_patches or has_stray:
        member_patches = list(patch_payload['member_updates_patch'])
        if has_stray and (not member_patches):
            for update in result_data.get('member_updates') or []:
                if not isinstance(update, dict):
                    continue
                if any((k in _STRAY_MEMBER_UPDATE_KEYS for k in update.keys())):
                    try:
                        member_patches.append({'id': int(update.get('id'))})
                    except (TypeError, ValueError):
                        continue
        return {'decision': 'patched', 'member_updates_patch': member_patches, 'top_level_patch': patch_payload['top_level_patch'], 'retry_feedback': ''}
    return {'decision': 'allow', 'member_updates_patch': [], 'top_level_patch': {}, 'retry_feedback': ''}

def needs_career_consumption_review(result_data: dict, member_snapshot: list | None, target_dungeon: str | None=None) -> bool:
    """是否必须在落库前规范正式邀请函，或语义复核职业技能卷轴。"""
    return has_unresolved_class_transfer_consumption(result_data, member_snapshot, target_dungeon) or _needs_profession_skill_scroll_ai_review(result_data, member_snapshot)

def build_settlement_guard_quarantine_scope(result_data: dict, member_snapshot: list | None, target_dungeon: str | None=None) -> dict[int, dict[str, tuple[str, ...]]]:
    """冻结 Guard 故障时可撤销的精确成员字段与职业凭证条目。"""
    class_transfer_member_ids = set(_class_transfer_consumption_patches(result_data, member_snapshot, target_dungeon))
    skill_scroll_member_ids = _profession_skill_scroll_review_member_ids(result_data, member_snapshot)
    target_member_ids = class_transfer_member_ids | skill_scroll_member_ids
    if not target_member_ids:
        return {}
    snapshot_by_id = _snapshot_by_adventurer_id(member_snapshot)
    update_by_id = {int(update.get('id')): update for update in result_data.get('member_updates') or [] if isinstance(update, dict) and str(update.get('id') or '').isdigit()}
    item_names: set[str] = set()
    for member_id in class_transfer_member_ids:
        snapshot = snapshot_by_id.get(member_id) or {}
        item_names.update((str(item.get('name') or '').strip() for item in snapshot.get('equipment') or [] if isinstance(item, dict) and str(item.get('name') or '').strip()))
        update = update_by_id.get(member_id) or {}
        item_names.update((row['name'] for row in _normalized_consumed_equipment_entries(update.get('consumed_equipments'))))
    condition_by_name = build_use_condition_map(item_names, independent_read=True)
    allow_legacy_fields = not has_current_s1_gold_settlement_contract(result_data)
    scope: dict[int, dict[str, tuple[str, ...]]] = {}
    for member_id in sorted(target_member_ids):
        update = update_by_id.get(member_id) or {}
        member_fields: set[str] = set()
        consumed_names: set[str] = set()
        if member_id in class_transfer_member_ids:
            member_fields.add('new_class')
            if allow_legacy_fields:
                member_fields.add('character_class')
            if update.get('new_class') or (allow_legacy_fields and update.get('character_class')):
                member_fields.add('new_skills')
            consumed_names.update((row['name'] for row in _normalized_consumed_equipment_entries(update.get('consumed_equipments')) if is_class_transfer_invitation(condition_by_name.get(row['name']))))
        if member_id in skill_scroll_member_ids:
            member_fields.add('new_skills')
            consumed_names.add('职业通用技能卷轴')
        scope[member_id] = {'member_fields': tuple(sorted(member_fields)), 'consumed_equipment_names': tuple(sorted(consumed_names))}
    return scope

def resolve_settlement_guard(*, expedition_log_id: int, party_id: int, result_data: dict, target_dungeon: str, member_snapshot: list, context: dict, attempt_number: int=1) -> dict | None:
    """先尝试确定性 Guard，无法无损推断时再调用 AI。"""
    deterministic = try_deterministic_settlement_guard(result_data, member_snapshot, context, target_dungeon=target_dungeon)
    if deterministic is not None:
        logger.info('S1 结算 Guard 确定性路径 log=%s decision=%s', expedition_log_id, deterministic.get('decision'))
        return deterministic
    return call_settlement_guard_ai(expedition_log_id=expedition_log_id, party_id=party_id, result_data=result_data, target_dungeon=target_dungeon, member_snapshot=member_snapshot, context=context, attempt_number=attempt_number)

def _coerce_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _extract_json_content_with_error(content: str) -> tuple[dict | None, str | None]:
    text = str(content or '').strip()
    if '```' in text:
        start, end = (text.find('{'), text.rfind('}'))
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        pass
    else:
        if isinstance(parsed, dict):
            return (parsed, None)
        return (None, f'path=$ reason=not_object expected=object actual_type={type(parsed).__name__}')
    result = parse_s1_ai_json_content(content, repair_mode='lossless_syntax_only')
    if result.error_code == 'empty_content':
        return (None, 'path=$ reason=empty_response expected=json_object')
    if result.error_code == 'not_object':
        return (None, f'path=$ reason=not_object expected=object actual_type={result.error_detail}')
    if not result.ok:
        return (None, f'path=$ reason=invalid_json detail={str(result.error_detail)[:240]}')
    return (result.data, None)

def _extract_json_content(content: str) -> dict | None:
    parsed, _ = _extract_json_content_with_error(content)
    return parsed

def _validate_guard_member_patch(row: object, allowed_ids: set[int], *, allow_legacy_fields: bool=True, member_asset_context_by_id: dict[int, dict] | None=None) -> dict | None:
    if not isinstance(row, dict):
        return None
    allowed_fields = _ALLOWED_MEMBER_PATCH_FIELDS if allow_legacy_fields else _CURRENT_ALLOWED_MEMBER_PATCH_FIELDS
    row = {str(key): value for key, value in row.items() if str(key) in allowed_fields}
    row = project_s1_member_settlement_fields(row)
    raw_id = row.get('id')
    member_id, _ = normalize_ascii_integer(raw_id, minimum=1)
    if type(member_id) is not int:
        return None
    if member_id not in allowed_ids:
        return None
    for field in _S1_MEMBER_INTEGER_FIELDS:
        if field not in row:
            continue
        normalized_integer, _ = normalize_ascii_integer(row[field])
        if type(normalized_integer) is not int:
            return None
        row[field] = normalized_integer
    for field in _S1_MEMBER_FINITE_NUMBER_FIELDS:
        if field not in row:
            continue
        normalized_number, _ = normalize_ascii_finite_number(row[field])
        try:
            is_finite_number = not isinstance(normalized_number, bool) and isinstance(normalized_number, (int, float)) and math.isfinite(normalized_number)
        except (TypeError, ValueError, OverflowError):
            is_finite_number = False
        if not is_finite_number:
            return None
        row[field] = normalized_number
    if 'is_dead' in row:
        normalized_is_dead, _ = normalize_exact_boolean(row['is_dead'])
        if type(normalized_is_dead) is not bool:
            return None
        row['is_dead'] = normalized_is_dead
    for field in ('new_equipments',):
        if field in row and (not isinstance(row[field], dict)):
            return None
    for field in ('consumed_equipments', 'removed_traits', 'new_skills', 'removed_skills', 'relationship_changes', 'story_threads', 'skill_updates', 'equipment_updates'):
        if field in row and (not isinstance(row[field], list)):
            return None
    require_asset_context = isinstance(member_asset_context_by_id, dict)
    member_asset_context = None
    if require_asset_context:
        candidate_context = member_asset_context_by_id.get(member_id)
        member_asset_context = candidate_context if isinstance(candidate_context, dict) else {}
    asset_update_errors = normalize_s1_existing_asset_updates(row, member_asset_context=member_asset_context, event_count=None, path='member_updates_patch', require_asset_context=require_asset_context)
    if asset_update_errors:
        return None
    if 'consumed_equipments' in row:
        row['consumed_equipments'] = [{key: entry[key] for key in ('name', 'quantity') if key in entry} if isinstance(entry, dict) else entry for entry in row['consumed_equipments']]
        for entry in row['consumed_equipments']:
            if not isinstance(entry, dict) or 'quantity' not in entry:
                continue
            quantity, _ = normalize_ascii_integer(entry.get('quantity'), minimum=1, maximum=999999)
            entry['quantity'] = quantity
    if 'consumed_equipments' in row and any((not isinstance(entry, dict) or frozenset(entry) != frozenset({'name', 'quantity'}) or (not isinstance(entry.get('name'), str)) or (not entry['name'].strip()) or isinstance(entry.get('quantity'), bool) or (not isinstance(entry.get('quantity'), int)) or (not 1 <= entry['quantity'] <= 999999) for entry in row['consumed_equipments'])):
        return None
    if 'new_traits' in row and (not isinstance(row['new_traits'], (dict, list))):
        return None
    for field in ('death_reason', 'mental_state', 'history_story_append', 'new_class', 'character_class'):
        if field in row and (not isinstance(row[field], str)):
            return None
    return {**row, 'id': member_id}

def _validate_settlement_guard_result(result: object, *, allowed_ids: set[int], allow_legacy_member_update_fields: bool=True, member_asset_context_by_id: dict[int, dict] | None=None) -> dict | None:
    if not isinstance(result, dict):
        return None
    result = {str(key): value for key, value in result.items() if str(key) in _GUARD_RESULT_ALLOWED_FIELDS}
    decision = result.get('decision')
    if decision not in ('allow', 'patched', 'regenerate'):
        return None
    retry_feedback = result.get('retry_feedback')
    if decision == 'regenerate':
        normalized_retry_feedback = retry_feedback.strip() if isinstance(retry_feedback, str) and retry_feedback.strip() else '结算 Guard 判定当前候选无法无损修复，请重新生成。'
    else:
        normalized_retry_feedback = ''
    if retry_feedback != normalized_retry_feedback:
        logger.warning('S1 结算 Guard 软归一审计说明 code=settlement_guard_retry_feedback_normalized decision=%s raw_type=%s', decision, type(retry_feedback).__name__)
    result['retry_feedback'] = normalized_retry_feedback
    if frozenset(result) != _GUARD_RESULT_ALLOWED_FIELDS:
        return None
    member_patches = result.get('member_updates_patch')
    top_patch = result.get('top_level_patch')
    retry_feedback = result['retry_feedback']
    if not isinstance(member_patches, list) or not isinstance(top_patch, dict) or (not isinstance(retry_feedback, str)):
        return None
    top_patch = {str(key): value for key, value in top_patch.items() if str(key) in _ALLOWED_TOP_LEVEL_PATCH_FIELDS}
    top_patch = project_s1_top_settlement_fields(top_patch)
    normalized_patches: list[dict] = []
    seen_member_ids: set[int] = set()
    for row in member_patches:
        normalized = _validate_guard_member_patch(row, allowed_ids, allow_legacy_fields=allow_legacy_member_update_fields, member_asset_context_by_id=member_asset_context_by_id)
        if normalized is None:
            return None
        member_id = normalized['id']
        if member_id in seen_member_ids:
            return None
        seen_member_ids.add(member_id)
        if set(normalized) == {'id'}:
            continue
        normalized_patches.append(normalized)
    for key, value in top_patch.items():
        if key == 'hidden_boss_completed':
            normalized_completed, _ = normalize_ascii_integer(value, minimum=0, maximum=1)
            if type(normalized_completed) is not int or normalized_completed not in (0, 1):
                return None
            top_patch[key] = normalized_completed
        elif key == 'commission_results':
            if not isinstance(value, list) or any((not isinstance(item, dict) for item in value)):
                return None
        elif key == 'completion_check' and (not isinstance(value, dict)):
            return None
    has_patch = bool(normalized_patches or top_patch)
    if decision == 'allow' and (has_patch or retry_feedback.strip()):
        return None
    if decision == 'patched' and (not has_patch or retry_feedback.strip()):
        return None
    if decision == 'regenerate' and (has_patch or not retry_feedback.strip()):
        return None
    return {'decision': decision, 'member_updates_patch': normalized_patches, 'top_level_patch': top_patch, 'retry_feedback': retry_feedback.strip()}

def _guard_contract_error(path: str, reason: str, *, expected: object | None=None, actual: object | None=None) -> str:
    parts = [f'path={path}', f'reason={reason}']
    if expected is not None:
        encoded = json.dumps(expected, ensure_ascii=False, default=str, separators=(',', ':'))
        parts.append(f'expected={encoded[:240]}')
    if actual is not None:
        encoded = json.dumps(actual, ensure_ascii=False, default=str, separators=(',', ':'))
        parts.append(f'actual={encoded[:240]}')
    return ' '.join(parts)

def _diagnose_guard_member_patch(row: object, allowed_ids: set[int], path: str, *, allow_legacy_fields: bool=True, member_asset_context_by_id: dict[int, dict] | None=None) -> str | None:
    if not isinstance(row, dict):
        return _guard_contract_error(path, 'not_object', expected='object', actual=type(row).__name__)
    allowed_fields = _ALLOWED_MEMBER_PATCH_FIELDS if allow_legacy_fields else _CURRENT_ALLOWED_MEMBER_PATCH_FIELDS
    row = {str(key): value for key, value in row.items() if str(key) in allowed_fields}
    raw_id = row.get('id')
    member_id, _ = normalize_ascii_integer(raw_id, minimum=1)
    if type(member_id) is not int:
        return _guard_contract_error(f'{path}.id', 'not_allowed_member_id', expected=sorted(allowed_ids), actual=type(raw_id).__name__)
    if member_id not in allowed_ids:
        return _guard_contract_error(f'{path}.id', 'not_allowed_member_id', expected=sorted(allowed_ids), actual=member_id)
    for field in _S1_MEMBER_INTEGER_FIELDS:
        if field not in row:
            continue
        raw_value = row.get(field)
        normalized_integer, _ = normalize_ascii_integer(raw_value)
        if type(normalized_integer) is not int:
            return _guard_contract_error(f'{path}.{field}', 'not_integer', expected='integer', actual=type(raw_value).__name__)
    for field in _S1_MEMBER_FINITE_NUMBER_FIELDS:
        if field not in row:
            continue
        raw_value = row.get(field)
        value, _ = normalize_ascii_finite_number(raw_value)
        try:
            valid_number = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
        except (TypeError, ValueError, OverflowError):
            valid_number = False
        if not valid_number:
            return _guard_contract_error(f'{path}.{field}', 'not_number', expected='number', actual=type(raw_value).__name__)
    if 'is_dead' in row:
        is_dead, _ = normalize_exact_boolean(row['is_dead'])
        if type(is_dead) is not bool:
            return _guard_contract_error(f'{path}.is_dead', 'not_boolean', expected='boolean', actual=type(row['is_dead']).__name__)
    if 'new_equipments' in row and (not isinstance(row['new_equipments'], dict)):
        return _guard_contract_error(f'{path}.new_equipments', 'not_object', expected='object', actual=type(row['new_equipments']).__name__)
    list_fields = ('consumed_equipments', 'removed_traits', 'new_skills', 'removed_skills', 'relationship_changes', 'story_threads', 'skill_updates', 'equipment_updates')
    for field in list_fields:
        if field in row and (not isinstance(row[field], list)):
            return _guard_contract_error(f'{path}.{field}', 'not_list', expected='array', actual=type(row[field]).__name__)
    normalized_asset_row = copy.deepcopy(row)
    require_asset_context = isinstance(member_asset_context_by_id, dict)
    member_asset_context = None
    if require_asset_context:
        candidate_context = member_asset_context_by_id.get(member_id)
        member_asset_context = candidate_context if isinstance(candidate_context, dict) else {}
    asset_update_errors = normalize_s1_existing_asset_updates(normalized_asset_row, member_asset_context=member_asset_context, event_count=None, path=path, require_asset_context=require_asset_context)
    if asset_update_errors:
        return _guard_contract_error(path, 'existing_asset_update_contract', actual=asset_update_errors[0])
    for index, entry in enumerate(row.get('consumed_equipments') or []):
        entry_path = f'{path}.consumed_equipments[{index}]'
        if not isinstance(entry, dict):
            return _guard_contract_error(entry_path, 'not_object', expected='object', actual=type(entry).__name__)
        if not {'name', 'quantity'}.issubset(entry):
            return _guard_contract_error(entry_path, 'unexpected_shape', expected=['name', 'quantity'], actual=sorted(map(str, entry)))
        if not isinstance(entry.get('name'), str) or not entry['name'].strip():
            return _guard_contract_error(f'{entry_path}.name', 'not_non_empty_string', expected='non_empty_string')
        raw_quantity = entry.get('quantity')
        quantity, _ = normalize_ascii_integer(raw_quantity, minimum=1, maximum=999999)
        if type(quantity) is not int:
            return _guard_contract_error(f'{entry_path}.quantity', 'not_integer', expected='integer_1_to_999999', actual=type(raw_quantity).__name__)
        if not 1 <= quantity <= 999999:
            return _guard_contract_error(f'{entry_path}.quantity', 'out_of_range', expected='integer_1_to_999999', actual=quantity)
    if 'new_traits' in row and (not isinstance(row['new_traits'], (dict, list))):
        return _guard_contract_error(f'{path}.new_traits', 'not_object_or_list', expected='object_or_array', actual=type(row['new_traits']).__name__)
    for field in ('death_reason', 'mental_state', 'history_story_append', 'new_class', 'character_class'):
        if field in row and (not isinstance(row[field], str)):
            return _guard_contract_error(f'{path}.{field}', 'not_string', expected='string', actual=type(row[field]).__name__)
    return None

def _diagnose_settlement_guard_result(result: object, allowed_ids: set[int], *, allow_legacy_member_update_fields: bool=True, member_asset_context_by_id: dict[int, dict] | None=None) -> str:
    if not isinstance(result, dict):
        return _guard_contract_error('$', 'not_object', expected='object', actual=type(result).__name__)
    if not _GUARD_RESULT_ALLOWED_FIELDS.issubset(result):
        return _guard_contract_error('$', 'unexpected_shape', expected=sorted(_GUARD_RESULT_ALLOWED_FIELDS), actual=sorted(map(str, result)))
    decision = result.get('decision')
    if decision not in ('allow', 'patched', 'regenerate'):
        return _guard_contract_error('decision', 'invalid_enum', expected=['allow', 'patched', 'regenerate'], actual=decision)
    member_patches = result.get('member_updates_patch')
    if not isinstance(member_patches, list):
        return _guard_contract_error('member_updates_patch', 'not_list', expected='array', actual=type(member_patches).__name__)
    seen_member_ids: set[int] = set()
    for index, row in enumerate(member_patches):
        row_path = f'member_updates_patch[{index}]'
        error = _diagnose_guard_member_patch(row, allowed_ids, row_path, allow_legacy_fields=allow_legacy_member_update_fields, member_asset_context_by_id=member_asset_context_by_id)
        if error:
            return error
        member_id, _ = normalize_ascii_integer(row.get('id'), minimum=1)
        if member_id in seen_member_ids:
            return _guard_contract_error(f'{row_path}.id', 'duplicate_member_id', expected='unique_member_id', actual=member_id)
        seen_member_ids.add(member_id)
    top_patch = result.get('top_level_patch')
    if not isinstance(top_patch, dict):
        return _guard_contract_error('top_level_patch', 'not_object', expected='object', actual=type(top_patch).__name__)
    for key, value in top_patch.items():
        if key == 'hidden_boss_completed':
            normalized_completed, _ = normalize_ascii_integer(value, minimum=0, maximum=1)
            if type(normalized_completed) is not int or normalized_completed not in (0, 1):
                return _guard_contract_error(f'top_level_patch.{key}', 'not_zero_or_one', expected=[0, 1], actual=value)
        if key == 'commission_results':
            if not isinstance(value, list):
                return _guard_contract_error(f'top_level_patch.{key}', 'not_list', expected='array', actual=type(value).__name__)
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    return _guard_contract_error(f'top_level_patch.{key}[{index}]', 'not_object', expected='object', actual=type(item).__name__)
        if key == 'completion_check' and (not isinstance(value, dict)):
            return _guard_contract_error(f'top_level_patch.{key}', 'not_object', expected='object', actual=type(value).__name__)
    retry_feedback = result.get('retry_feedback')
    if not isinstance(retry_feedback, str):
        return _guard_contract_error('retry_feedback', 'not_string', expected='string', actual=type(retry_feedback).__name__)
    has_patch = bool(member_patches or top_patch)
    if decision == 'allow' and (has_patch or retry_feedback.strip()):
        return _guard_contract_error('decision', 'allow_payload_conflict', expected='no patch or retry_feedback')
    if decision == 'patched' and (not has_patch or retry_feedback.strip()):
        return _guard_contract_error('decision', 'patched_payload_conflict', expected='patch without retry_feedback')
    if decision == 'regenerate' and (has_patch or not retry_feedback.strip()):
        return _guard_contract_error('decision', 'regenerate_payload_conflict', expected='retry_feedback without patch')
    return 'path=$ reason=unknown_contract_error'

def _sanitize_member_patch(row: dict, allowed_ids: set[int], *, allow_legacy_fields: bool=True) -> dict | None:
    if not isinstance(row, dict):
        return None
    adv_id, _ = normalize_ascii_integer(row.get('id'), minimum=1)
    if type(adv_id) is not int:
        return None
    if adv_id not in allowed_ids:
        return None
    allowed_fields = _ALLOWED_MEMBER_PATCH_FIELDS if allow_legacy_fields else _CURRENT_ALLOWED_MEMBER_PATCH_FIELDS
    clean = {k: v for k, v in row.items() if k in allowed_fields}
    clean['id'] = adv_id
    return project_s1_member_settlement_fields(clean)

def _merge_guard_new_equipment_metadata(base_value: object, patch_value: object) -> object:
    """Let Guard fix metadata without changing Observer-owned entitlement."""
    if not isinstance(base_value, dict) or not isinstance(patch_value, dict):
        return copy.deepcopy(base_value)
    merged: dict = {}
    for name, base_item in base_value.items():
        patch_item = patch_value.get(name)
        if not isinstance(base_item, dict) or not isinstance(patch_item, dict):
            merged[name] = copy.deepcopy(base_item)
            continue
        item = copy.deepcopy(base_item)
        item.update(copy.deepcopy(patch_item))
        if 'quantity' in base_item:
            item['quantity'] = copy.deepcopy(base_item['quantity'])
        else:
            item.pop('quantity', None)
        merged[name] = item
    return merged

def _merge_member_patch(base_update: dict, patch: dict) -> dict:
    merged = copy.deepcopy(base_update)
    for key, value in patch.items():
        if key == 'id':
            continue
        if value is None:
            continue
        if key == 'new_equipments':
            if key not in base_update:
                continue
            merged[key] = _merge_guard_new_equipment_metadata(base_update.get(key), value)
            continue
        merged[key] = value
    return merged

def apply_settlement_guard_patch(result_data: dict, ai_result: dict, member_ids: list[int]) -> dict:
    """按白名单合并 Guard 输出；实物权益字段始终继承 Observer 成品。"""
    if not isinstance(result_data, dict) or not isinstance(ai_result, dict):
        return result_data
    patched = copy.deepcopy(result_data)
    allowed_ids = {int(i) for i in member_ids}
    allow_legacy_fields = not has_current_s1_gold_settlement_contract(result_data)
    top_patch = ai_result.get('top_level_patch')
    if isinstance(top_patch, dict):
        top_patch = project_s1_top_settlement_fields(top_patch)
        for key, value in top_patch.items():
            if key in _ALLOWED_TOP_LEVEL_PATCH_FIELDS and value is not None:
                if key == 'completion_check' and isinstance(value, dict):
                    current = patched.get(key)
                    merged = dict(current) if isinstance(current, dict) else {}
                    merged.update(value)
                    patched[key] = merged
                else:
                    patched[key] = value
    raw_member_patches = ai_result.get('member_updates_patch') or []
    patches_by_id: dict[int, dict] = {}
    if isinstance(raw_member_patches, list):
        for row in raw_member_patches:
            clean = _sanitize_member_patch(row, allowed_ids, allow_legacy_fields=allow_legacy_fields)
            if clean:
                patches_by_id[int(clean['id'])] = clean
    updates = patched.get('member_updates')
    if isinstance(updates, list) and patches_by_id:
        patched_updates = []
        for update in updates:
            if not isinstance(update, dict):
                patched_updates.append(update)
                continue
            try:
                adv_id = int(update.get('id'))
            except (TypeError, ValueError):
                patched_updates.append(update)
                continue
            patch = patches_by_id.get(adv_id)
            patched_updates.append(_merge_member_patch(update, patch) if patch else update)
        patched['member_updates'] = patched_updates
    for update in patched.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        for key in list(update.keys()):
            if key in _STRAY_MEMBER_UPDATE_KEYS:
                del update[key]
    return project_s1_settlement_fields(patched)

def call_settlement_guard_ai(*, expedition_log_id: int, party_id: int, result_data: dict, target_dungeon: str, member_snapshot: list, context: dict, attempt_number: int=1) -> dict | None:
    """调用轻量结算修正 AI；失败返回 None，由调用方保守放行。"""
    try:
        route = resolve_s1_ai_profile_snapshot('settlement_guard')
    except AIProfileAssignmentError as exc:
        logger.warning('S1 结算 Guard 用途路由不可用，隔离 Guard 所有权字段后发布正文 party=%s log=%s error=%s', party_id, expedition_log_id, str(exc)[:500])
        return None
    api_key = route.api_key
    api_base = route.api_base
    model = route.model
    timeout_floor = _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_AI_TIMEOUT_FLOOR_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS)
    timeout = max(S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS, timeout_floor, _coerce_int(current_app.config.get('ADVENTURER_S1_AI_DEFAULT_TIMEOUT_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_SETTLEMENT_GUARD_AI_TIMEOUT_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS))
    payload = _build_guard_user_payload(target_dungeon=target_dungeon, result_data=result_data, member_snapshot=member_snapshot, context=context)
    deferred_token_usage = None
    defer_token_usage_to_consumer = False
    request_metadata: dict[str, str] = {}
    allowed_ids = set(_snapshot_by_adventurer_id(member_snapshot))
    member_asset_context_by_id = build_s1_existing_asset_context_by_member(member_snapshot)
    allow_legacy_member_update_fields = not has_current_s1_gold_settlement_contract(result_data)

    def validate_settlement_guard_response(decoded) -> S1AiResponseContractResult:
        parsed_candidate = decoded.json_result.data
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'invalid_json', failure_stage='response_parse')
        normalized_candidate = _validate_settlement_guard_result(parsed_candidate, allowed_ids=allowed_ids, allow_legacy_member_update_fields=allow_legacy_member_update_fields, member_asset_context_by_id=member_asset_context_by_id)
        if normalized_candidate is not None:
            return S1AiResponseContractResult.accept()
        return S1AiResponseContractResult.reject(_diagnose_settlement_guard_result(parsed_candidate, allowed_ids, allow_legacy_member_update_fields=allow_legacy_member_update_fields, member_asset_context_by_id=member_asset_context_by_id), failure_stage='contract_validation')
    try:
        db_manager.remove()
        resp = post_s1_ai_request(f'{api_base}/chat/completions', feature='settlement_guard', player_names=frozen_player_names({'audit_member_snapshot': member_snapshot, 'context': context}), resolved_profile=route, json_object_mode_if_supported=True, request_metadata_out=request_metadata, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'system', 'content': _SETTLEMENT_GUARD_SYSTEM_PROMPT}, {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}], 'temperature': 0.1}, timeout=timeout, response_validator=validate_settlement_guard_response)
        decoded = parse_s1_ai_http_json_response(resp)
        usage = decoded.http_result.usage
        if isinstance(usage, dict) and usage:
            deferred_token_usage = _DeferredSettlementGuardTokenUsage(usage=usage, model=request_metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_number)
        parsed = decoded.json_result.data
        parse_error = None if decoded.json_result.ok else decoded.json_result.error_code or 'invalid_json'
        if parse_error:
            logger.warning('S1 AI JSON 解析失败 stage=settlement_guard party=%s log=%s attempt=%s error=%s', party_id, expedition_log_id, attempt_number, parse_error)
            return None
        result = _validate_settlement_guard_result(parsed, allowed_ids=allowed_ids, allow_legacy_member_update_fields=allow_legacy_member_update_fields, member_asset_context_by_id=member_asset_context_by_id)
        if result is None:
            logger.warning('S1 AI 输出契约失败 stage=settlement_guard party=%s log=%s attempt=%s error=%s', party_id, expedition_log_id, attempt_number, _diagnose_settlement_guard_result(parsed, allowed_ids, allow_legacy_member_update_fields=allow_legacy_member_update_fields, member_asset_context_by_id=member_asset_context_by_id))
        if result is not None and deferred_token_usage is not None:
            result = _SettlementGuardAIResult(result, deferred_token_usage=deferred_token_usage)
            defer_token_usage_to_consumer = True
        return result
    except Exception as exc:
        if deferred_token_usage is None:
            record_token_usage(call_type='expedition_settlement_guard_ai', usage=None, model=request_metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_number, is_success=False, failure_stage='http_or_response', validation_error_code=f'http_or_response.{type(exc).__name__}', record_without_usage=True, request_metadata=request_metadata, request_exception=exc)
        logger.warning('S1 AI 调用失败 stage=settlement_guard party=%s log=%s attempt=%s error=%s detail=%s action=fail_open', party_id, expedition_log_id, attempt_number, type(exc).__name__, str(exc)[:500])
        return None
    finally:
        if deferred_token_usage is not None and (not defer_token_usage_to_consumer):
            deferred_token_usage.finalize(is_success=False)
