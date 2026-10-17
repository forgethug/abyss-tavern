"""Author the portable pack's reduced AI pipeline during deterministic export.

Transforms authored Python syntax, never player text or model output. Runtime
identity, transaction, capacity and source-version checks remain authoritative.
"""
from __future__ import annotations

import ast
import copy
import runpy
from pathlib import Path

ROOT = 'backend/plugins/adventurer_s1/'
SERVICES = ROOT + 'services/'
RETIRED = {'building_skill_training_review', 'skill_training_content_review_ai',
           'item_assembly_authority_review_ai', 'ADVENTURER_S1_ITEM_AUTHORITY_REVIEW_ATTEMPTS'}


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f'AI pipeline source drift: {old[:100]!r}')
    return source.replace(old, new)


def function(tree, name):
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(nodes) != 1:
        raise ValueError(f'AI pipeline source drift: function {name}')
    return nodes[0]


def rewrite_function(tree, name, transform):
    old = function(tree, name)
    new = ast.parse(transform(ast.unparse(old))).body[0]
    tree.body[tree.body.index(old)] = new


def drop_definitions(tree, names):
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.FunctionDef) and n.name in names or
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))]


def slice_between(source, start, end):
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError(f'AI pipeline source drift: {start!r} -> {end!r}')
    a, b = source.index(start), source.index(end)
    if b <= a:
        raise ValueError('AI pipeline source drift: reversed anchors')
    return source[:a] + source[b:]


class CatalogCleanup(ast.NodeTransformer):
    def visit_Constant(self, node):
        if node.value == '按条目范围审核资产输入资格、描述摘要、特质路由与效果性质':
            node.value = '按条目范围审核描述摘要、特质路由与效果性质'
        return node

    def visit_Dict(self, node):
        pairs = [(k, v) for k, v in zip(node.keys, node.values)
                 if not (isinstance(k, ast.Constant) and k.value in RETIRED)]
        node.keys, node.values = [k for k, _ in pairs], [v for _, v in pairs]
        return self.generic_visit(node)

    def visit_Tuple(self, node):
        node.elts = [v for v in node.elts if not (isinstance(v, ast.Dict) and
            any(isinstance(k, ast.Constant) and k.value == 'key' and isinstance(value, ast.Constant)
                and value.value in RETIRED for k, value in zip(v.keys, v.values)))]
        return self.generic_visit(node)

    def visit_Set(self, node):
        node.elts = [v for v in node.elts if not (isinstance(v, ast.Constant) and v.value in RETIRED)]
        return self.generic_visit(node)


def project_building(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == '_SKILL_TRAINING_SYSTEM_PROMPT' for t in node.targets):
            node.value = ast.BinOp(node.value, ast.Add(), ast.Constant('\n技能描述中的每次使用后等待时长必须与 recovery_minutes 表达同一时长；recovery_minutes 大于 0 时，描述须明确向玩家说明该等待时长。施法和效果持续时间分别按其原义描述。\n'))
    def mentor(source):
        source = slice_between(source, '    review_adv = ', "    if operation == 'advance' and normalized")
        return source.replace('[usage_attempt, *review_usage_attempts]', '[usage_attempt]')
    rewrite_function(tree, '_call_skill_training_ai_impl', mentor)
    drop_definitions(tree, {'_SKILL_TRAINING_CONTENT_REVIEW_SYSTEM_PROMPT',
        '_normalize_skill_training_content_review', '_skill_training_rejection_narrative',
        '_review_item_assembly_authority'})
    def assembly(source):
        return slice_between(source,
            "        if pipeline.get('status') == 'approved' and pipeline['result']['success']:",
            "        if pipeline.get('status') == 'approved':")
    rewrite_function(tree, '_generate_item_assembly', assembly)
    def consumer(source):
        source = replace_once(source, "    authority_review = pipeline.get('authority_review') or {}\n", '')
        source = slice_between(source,
            '                from plugins.adventurer_s1.services.s1_item_authority_service import ',
            '                from plugins.adventurer_s1.services.s1_item_catalog_lock_service import CATALOG_BUSY_ERROR')
        source = slice_between(source, '                catalog = AdventurerS1Item.query.filter_by(name=new_name).first()',
            "                settlement_receipt['output'] = ")
        return replace_once(source, "            if authority_review:\n                result_payload['ai_review'] = authority_review\n", '')
    rewrite_function(tree, '_run_item_assembly_workshop_ai_consumer', consumer)
    # Provider authentication errors have already exhausted configured routes.
    # A content-generation retry cannot repair the same rejected credential.
    rewrite_function(tree, '_call_building_ai', lambda source: replace_once(source,
        "    except requests.HTTPError as e:\n        failure_code = 'http_error'",
        "    except requests.HTTPError as e:\n        failure_code = 'authentication_error' if getattr(getattr(e, 'response', None), 'status_code', None) in (401, 403) else 'http_error'"))
    rewrite_function(tree, '_run_item_assembly_ai_state_machine', lambda source: replace_once(source,
        "                if _item_assembly_consumed_attempt_count(attempt_history, 'generation') >= generation_limit:",
        "                if generation_failure_code == 'authentication_error' or _item_assembly_consumed_attempt_count(attempt_history, 'generation') >= generation_limit:"))
    rewrite_function(tree, '_call_ember_kitchen_ai_impl', lambda source: replace_once(source,
        "            retry_reason_code = last_failure_code if last_failure_code in EMBER_KITCHEN_RETRY_REASON_DESCRIPTIONS else None\n            continue",
        "            retry_reason_code = last_failure_code if last_failure_code in EMBER_KITCHEN_RETRY_REASON_DESCRIPTIONS else None\n            if last_failure_code == 'authentication_error':\n                break\n            continue"))


DEFAULT_POLICY = '''
def default_ai_input_policy(*, source_hash):
    """Portable eligibility is a product rule, not an AI verdict."""
    return {'schema_version': AI_INPUT_POLICY_VERSION, 'source_hash': source_hash,
            'balance_mode': 'unrestricted', 'decision': 'allow',
            'reason_code': 'none', 'evidence_quote': ''}
'''


class DescriptionPolicies(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        if node.module and node.module.endswith('.s1_ai_input_policy_service'):
            for alias in node.names:
                if alias.name == 'normalize_ai_input_policy':
                    alias.name = 'default_ai_input_policy'
        return node

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == 'normalize_ai_input_policy':
            node.func.id = 'default_ai_input_policy'
            node.args = []
            node.keywords = [k for k in node.keywords if k.arg == 'source_hash']
        if isinstance(node.func, ast.Name) and node.func.id == '_reserve_description_items':
            for keyword in node.keywords:
                if keyword.arg == 'review_cached_policy':
                    keyword.value = ast.Constant(False)
        return self.generic_visit(node)


def project_description(tree):
    tree = DescriptionPolicies().visit(tree)
    rewrite_function(tree, '_is_cached_policy_only_candidate', lambda s: replace_once(s,
        "    if existing['ai_input_policy'] is not None:\n        return False\n", ''))
    def enqueue(source):
        start = source.index('        policy_reserved = ')
        end = source.index('\n', start)
        return source[:start] + '        policy_reserved = []' + source[end:]
    rewrite_function(tree, 'enqueue_description_compression_items', enqueue)
    enqueue_node = function(tree, 'enqueue_description_compression_items')
    enqueue_node.body[1:1] = ast.parse('''
if completed_source_hashes is not None:
    for item in _normalize_task_items(items):
        if not _description_requires_ai_summary(item['description'], kind=item['kind']):
            completed_source_hashes.add(item['source_hash'])
''').body
    return tree


AUDIT_WRAPPER = '''
def audit_description_summary_samples(items, *, expedition_log_id=None):
    """Only semantic cache changes call AI; fixed eligibility is local."""
    if not items or any(not isinstance(item, dict) or not isinstance(item.get('review_key'), str)
                        or not item['review_key'] for item in items):
        return (None, 'invalid_input')
    if len({item['review_key'] for item in items}) != len(items):
        return (None, 'invalid_input')
    local = {}
    semantic_items = []
    for item in items:
        key = item['review_key']
        policy = default_ai_input_policy(source_hash=key)
        if item.get('ai_input_policy_only') is True:
            local[key] = {'ai_input_policy': policy}
            continue
        projected = {**item, 'ai_input_policy_required': False, 'ai_input_policy_only': False}
        if item.get('kind') != 'trait' and item.get('summary_decision') == 'keep_original':
            axes = {name: {'decision': 'not_applicable', 'issues': []} for name in
                    ('summary_review', 'trait_context_review', 'trait_effect_polarity_review')}
            parsed, valid = _parse_summary_sample_audit_results_with_status(
                {'results': [{'review_key': key, **axes}]}, expected_by_key={key: projected})
            if not valid:
                return (None, 'invalid_input')
            local[key] = {**parsed[key], 'ai_input_policy': policy}
        else:
            semantic_items.append(projected)
    if not semantic_items:
        return (local, None)
    reviewed, error = _audit_description_candidates(semantic_items, expedition_log_id=expedition_log_id)
    for key, decision in (reviewed or {}).items():
        local[key] = {**decision, 'ai_input_policy': default_ai_input_policy(source_hash=key)}
    return (local, error)
'''


def project_summary_audit(tree):
    function(tree, 'audit_description_summary_samples').name = '_audit_description_candidates'
    tree.body.extend(ast.parse(AUDIT_WRAPPER).body)
    index = 1 + max((i for i, n in enumerate(tree.body) if isinstance(n, ast.ImportFrom) and n.module == '__future__'), default=0)
    tree.body.insert(index, ast.parse('from plugins.adventurer_s1.services.s1_ai_input_policy_service import default_ai_input_policy').body[0])
    rewrite_function(tree, '_build_summary_sample_audit_system_prompt',
        lambda _: 'def _build_summary_sample_audit_system_prompt(items):\n    return _SUMMARY_SAMPLE_AUDIT_SYSTEM_PROMPT')
    rewrite_function(tree, '_build_summary_sample_audit_user_prompt', lambda _: '''
def _build_summary_sample_audit_user_prompt(items):
    item_payload = [{key: item.get(key) for key in ('review_key', 'kind', 'name', 'description',
                     'description_ai', 'summary_decision', 'trait_context_rule', 'target_chars')}
                    for item in items]
    has_trait = any(item.get('kind') == 'trait' for item in items)
    payload = {'trait_context_tag_catalog': trait_context_tag_catalog_for_prompt(include_dungeons=True),
               'items': item_payload} if has_trait else item_payload
    return '逐条审核冻结原文对应的缓存候选，各审核轴独立判断。\\n' + json.dumps(payload, ensure_ascii=False)
''')
    drop_definitions(tree, {'_POLICY_ONLY_AUDIT_SYSTEM_PROMPT'})
    # Remove only authored policy instructions; retain all fidelity/routing rules.
    class PromptCleanup(ast.NodeTransformer):
        def visit_FormattedValue(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == 'AI_INPUT_POLICY_RULES':
                return ast.Constant('')
            return self.generic_visit(node)

        def visit_Constant(self, node):
            if not isinstance(node.value, str): return node
            text = node.value.replace('机制摘要、特质效果性质、路由与资产输入资格审计员', '机制摘要、特质效果性质与路由审计员')
            text = text.replace('，资产输入资格按完整原文独立判定', '')
            text = text.replace('；资产输入资格以本 system 的资格规则评估完整原文，原文中的机制声明不提供规则例外', '')
            lines = [line for line in text.splitlines(True) if not line.startswith((
                '- ai_input_policy_only=true', '当输入条目的 ai_input_policy_required=true', '- ai_input_policy：'))]
            text = ''.join(lines).replace('资格专用条目只交付 review_key 与 ai_input_policy，其余条目包含：', '每项包含：')
            return ast.copy_location(ast.Constant(text), node)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == '_SUMMARY_SAMPLE_AUDIT_SYSTEM_PROMPT' for t in node.targets):
            node.value = PromptCleanup().visit(node.value)


MERGED_REVIEW_RULES = '''
【同次请求中的摘要核对】
candidate_description_ai 是候选全文的内部摘要。只有 summary_review_required=true 时检查摘要。
全文与摘要分别裁决：全文 decision 只取决于全文事实与变化边界；摘要失真不能拒绝已成立的全文变化。
摘要必须保留全文每项当前有效的身份、数量、形态、材质、状态、识别点、用法、能力、用途与负面效果，
以及条件、对象、范围、顺序、时机、时长、次数、数值、消耗、代价和限制；不得遗漏、增添、强化、弱化或反转。
等价重排和合并重复事实可以成立；只有不承载事实的气氛、反应和修辞可以省略。
审核对象、判断依据和处理出口保持独立，不能用冻结旧原文代替候选全文判断新摘要。
摘要忠实且完整时 summary_review.decision=approved、issues=[]；存在已确认失真或无法确认忠实度时为 rejected，issues 说明具体原因。
summary_review_required=false 时固定 not_applicable、issues=[]，后端使用完整新描述。
摘要 rejected 或其合同不完整时，只回退已通过审核的新全文，不丢弃已通过的全文变化，也不重开模型请求。
每条 reviews 额外交付 summary_review 对象，只含 decision 与 issues。decision 仅为 approved/rejected/not_applicable；
issues 沿用全文审核相同的条数和字符边界。全文和摘要的额外未消费字段由后端丢弃。
'''


MERGED_SUMMARIES = '''
def _review_generated_summaries(items, ai_results, *, expedition_log_id):
    """Consume the same review response; never dispatch a second reviewer."""
    reviewed = {}
    fallback_count = 0
    for target in items:
        key = target['update_ref']
        result = ai_results.get(key)
        if not isinstance(result, dict):
            continue
        full = str(result.get('full_description') or '').strip()
        summary = str(result.get('description_ai') or '').strip()
        if not _generated_full_description_is_valid(target, full):
            continue
        if summary == full or target['asset_kind'] == 'trait':
            reviewed[key] = {'full_description': full, 'description_ai': full, 'summary_source': 'full_projection'}
            continue
        axis = result.get('_summary_review')
        if _summary_is_valid(full, summary) and isinstance(axis, dict) and axis.get('decision') == 'approved':
            reviewed[key] = {'full_description': full, 'description_ai': summary, 'summary_source': 'ai_reviewed'}
        else:
            reviewed[key] = {'full_description': full, 'description_ai': full, 'summary_source': 'full_fallback'}
            fallback_count += 1
    return (reviewed, fallback_count)
'''


def project_asset_update(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {getattr(t, 'id', '') for t in node.targets}
            if '_ASSET_DESCRIPTION_UPDATE_REVIEW_SYSTEM_PROMPT' in names:
                class MergeReviewContract(ast.NodeTransformer):
                    def visit_Constant(self, part):
                        if not isinstance(part.value, str): return part
                        text = part.value.replace('你只审核候选全文能否替换冻结原文',
                            '你审核候选全文能否替换冻结原文，并独立核对候选摘要是否忠实于该新全文')
                        text = text.replace('"issues":["<仅在拒绝时列出具体问题>"]}',
                            '"issues":["<仅在拒绝时列出具体问题>"],"summary_review":{"decision":"approved|rejected|not_applicable","issues":["<摘要问题；无问题为空数组>"]}}')
                        return ast.Constant(text)
                node.value = MergeReviewContract().visit(node.value)
                node.value = ast.BinOp(node.value, ast.Add(), ast.Constant(MERGED_REVIEW_RULES))
            if 'S1_ASSET_DESCRIPTION_UPDATE_REVIEW_PROTOCOL_VERSION' in names:
                node.value = ast.Constant('s1-existing-asset-description-update-review-v4')
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.module and n.module.endswith('.s1_description_summary_review_ai_service'))]
    def prompt(source):
        return replace_once(source, "'candidate_full_description': ai_result['full_description']",
            "'candidate_full_description': ai_result['full_description'], 'candidate_description_ai': ai_result['description_ai'], 'summary_review_required': target['asset_kind'] != 'trait' and ai_result['description_ai'] != ai_result['full_description']")
    rewrite_function(tree, '_review_prompt_item', prompt)
    def parser(source):
        return replace_once(source, "        results[update_ref] = {'decision': decision, 'issues': normalized_issues}", '''        raw_summary = row.get('summary_review')
        summary_review = None
        if isinstance(raw_summary, dict):
            summary_decision = raw_summary.get('decision')
            summary_issues = raw_summary.get('issues')
            if (isinstance(summary_decision, str)
                    and summary_decision in {'approved', 'rejected', 'not_applicable'}
                    and isinstance(summary_issues, list)
                    and len(summary_issues) <= _ASSET_DESCRIPTION_UPDATE_REVIEW_MAX_ISSUES
                    and all(isinstance(issue, str) and 0 < len(issue.strip()) <= _ASSET_DESCRIPTION_UPDATE_REVIEW_MAX_ISSUE_CHARS for issue in summary_issues)
                    and (bool(summary_issues) if summary_decision == 'rejected' else not summary_issues)):
                summary_review = {'decision': summary_decision, 'issues': [issue.strip() for issue in summary_issues]}
        results[update_ref] = {'decision': decision, 'issues': normalized_issues, 'summary_review': summary_review}''')
    rewrite_function(tree, '_parse_update_review_results_with_status', parser)
    rewrite_function(tree, '_review_generated_descriptions', lambda s: replace_once(s,
        'approved[update_ref] = ai_results[update_ref]',
        "approved[update_ref] = {**ai_results[update_ref], '_summary_review': decision.get('summary_review')}"))
    rewrite_function(tree, '_review_generated_summaries', lambda _: MERGED_SUMMARIES)
    # Old v3 receipts already passed both independent checks, so replay them
    # without regenerating/editing previously accepted player state.
    for name in ('_receipt_covers_targets',):
        rewrite_function(tree, name, lambda s: replace_once(s,
            "receipt.get('review_protocol_version') != S1_ASSET_DESCRIPTION_UPDATE_REVIEW_PROTOCOL_VERSION",
            "receipt.get('review_protocol_version') not in {S1_ASSET_DESCRIPTION_UPDATE_REVIEW_PROTOCOL_VERSION, 's1-existing-asset-description-update-review-v3'}"))


TARGETS = {SERVICES + name + '.py' for name in (
    's1_building_ai_service', 's1_item_use_condition_service', 's1_ai_input_policy_service',
    's1_description_ai_service', 's1_description_summary_review_ai_service',
    's1_asset_description_update_ai_service', 's1_ai_runtime_config_service',
    'adventurer_s1_expedition_runner', 'adventurer_s1_expedition_observer_ai_service',
)} | {ROOT + 'plugin.py'}


def project_docs(relative, raw):
    source = raw.decode('utf-8-sig').replace('\r\n', '\n')
    if relative == 'docs/s1_prompt_authoring_rules.md':
        replacements = {
            '属性与他人资产写入边界由 ': '属性与他人资产写入边界由 `s1_ai_input_policy_service.ASSET_STATE_AUTHORITY_RULES` 统一提供给生成职责。世界内能力机制可以成立；持久变化仍通过当前玩法字段、准确身份、来源、数量与归属进入后端事务。导师生成承担授艺来源、精确进阶目标与恢复时间一致性，不再追加内容复核。',
            '现有输入资格审核支持 ': '自建版资产输入资格由后端确定为 allow，不调用 AI 补判。历史审核记录保留，但旧 exclude 不再过滤当前玩法输入。缺少资格记录不阻塞已验收摘要或原文，也不触发后台资格任务。',
            '构装生成继续拥有材料因果': '构装生成拥有材料因果、方向收束和成败，成功候选通过结构校验后直接进入最终事务，不再调用转职／血脉资格审核。结算仍复核会话、冻结投入、数量、名称、容量和消耗；正式邀请函与特殊门票继续使用原结构化玩法合同。',
            '主剧情、客观大纲、能力取材、Observer 与 Guard 共用服务端资产过滤': '旧构装资格回执不再排除物品输入；保留历史原文与记录。物品机制不会自行修改职业、血脉或数据库身份，实际变化仍由相应玩法及其后端合同消费，不以关键词替代语义判断。',
            '现有描述审核同次输出绑定原文版本的内部资格': '描述审核只负责摘要保真与适用的特质路由、效果性质；输入资格由自建规则确定，不交给模型。主剧情和配对审核继续使用完整合法资产及已验收摘要。',
            '资格审核复用 `S1_ECONOMIC_RESOURCE_SOURCE_BOUNDARY`': '资源生成与跨探险保留遵守自建版规则。实际取得仍绑定本次触发、数量、归属及当前结算字段；固定资格不授予数据库写入权限，不改写生命状态、身份或酒馆隔离。',
            '仅补判资格的已验收缓存使用后端冻结的': '历史资格专用任务本地完成；已验收的同源摘要、路由与效果性质继续复用。短非特质及 companion_policy 待办直接退出队列；真正待验收的摘要与特质仍走原语义审核。',
            '- `ecology_identity_diagnostic` 只服务': '- 不再生成生态身份诊断；历史日志保持原样。资源生态关系、地点现状与真实后继仍由原资源生态审核裁决，主题辨识度不取得重生成或世界状态权限。',
        }
        source = source.replace('装备与技能的更短摘要必须再通过既有摘要激活审核才可写入缓存',
            '装备与技能的更短摘要由同一次全文复核独立输出 summary_review，批准后才可写入缓存；摘要缺失、无效或拒绝时保留已批准的新全文，不再追加审核调用')
    else:
        replacements = {
            '- 正常量身授艺在第一次外部模型调用前': '- 正常量身授艺在第一次调用前冻结 operation、direction、精确目标、白名单 player_data、费用、课堂时长、建筑与会话。生成后直接进入结构校验及最终事务，不再追加内容复核或修订复审。进阶成品的完整描述与 recovery_minutes 都未变化时仍退款并保留补课，不以改名或叙事代替提升；恢复时间、同名、精确目标和版本校验保留。成功写入与使用日志在同一事务消费补课凭据并计算冷却；生成、合同、目标竞态或落库技术失败退款并保留凭据。凭据存在时不能换方向或换技能占用该机会。',
            '- 纯输入资格专审批次使用请求内短': '- 自建版输入资格由后端确定为 allow，不再提交资格专审。旧待办本地收尾，已审核缓存原样复用；摘要和特质路由的真实语义审核继续绑定精确来源。',
        }
        source = source.replace('生成与构装/授艺复核读取同一版本', '各建筑生成读取同一版本')
        source = source.replace('成功候选另经下述专用资格判定；', '')
        source = source.replace('未来 AI 只从通过服务端资格投影的产物获得能力事实', '未来 AI 按实际成品机制与当前玩法合同使用产物')
        source = source.replace('成功候选须完成专用资格判定后才进入最终行锁事务', '成功候选直接进入最终行锁事务')
        source = source.replace('成功候选仍须补做专用资格判定，失败候选直接继续原结算', '成功与失败候选均直接继续原结算')
        source = source.replace('既有转职/血脉资格判定不扩权，', '')
        source = source.replace('，成功候选资格判定使用 `item_assembly_authority_review_ai`', '')
        source = source.replace('JSON/schema、首次/终审复核调用或合同、非法修订、目标竞态', 'JSON/schema、目标竞态')
        source = source.replace('首次内容硬边界或终审明确 `reject` 时全退、消费补课并按内部终局标记计算 24 小时冷却，成功写回时同样消费补课并按成功日志计算冷却', '成功写回时消费补课并按成功日志计算冷却')
        source = source.replace('经既有独立复核和摘要激活检查后随结算写回', '经同一次独立复核分别判断全文与摘要后随结算写回，摘要失败只回退已批准的新全文')
        source = source.replace('在摘要、输入资格及适用的特质路由/效果性质复核完成后', '在摘要及适用的特质路由/效果性质复核完成后')
        source = source.replace('资格漏审、过期协议、实际调用失败', '过期协议、实际调用失败')
    lines = source.splitlines()
    for prefix, replacement in replacements.items():
        matches = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f'AI pipeline documentation drift: {prefix}')
        lines[matches[0]] = replacement
    return ('\n'.join(lines) + '\n').encode('utf-8')


def project_ai_calls(relative, raw):
    if relative in {'docs/s1_prompt_authoring_rules.md', 'docs/adventurer_design.md'}:
        return project_docs(relative, raw)
    if relative not in TARGETS:
        return raw
    source = raw.decode('utf-8-sig')
    original = ast.parse(source)
    tree = copy.deepcopy(original)
    filename = Path(relative).stem
    if filename == 's1_building_ai_service':
        project_building(tree)
    elif filename == 's1_item_use_condition_service':
        class RemoveAuthorityFilter(ast.NodeTransformer):
            def visit_ImportFrom(self, node):
                if node.module and node.module.endswith('.s1_item_authority_contract'): return None
                return node
            def visit_If(self, node):
                if isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Name) and node.test.func.id == 'item_authority_excludes_ai':
                    return None
                return self.generic_visit(node)
            def visit_BoolOp(self, node):
                node.values = [v for v in node.values if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == 'item_authority_excludes_ai')]
                return node.values[0] if len(node.values) == 1 else self.generic_visit(node)
        tree = RemoveAuthorityFilter().visit(tree)
    elif filename == 's1_ai_input_policy_service':
        tree.body.extend(ast.parse(DEFAULT_POLICY).body)
    elif filename == 's1_description_ai_service':
        tree = project_description(tree)
    elif filename == 's1_description_summary_review_ai_service':
        project_summary_audit(tree)
    elif filename == 's1_asset_description_update_ai_service':
        project_asset_update(tree)
    elif filename == 's1_ai_runtime_config_service':
        tree = CatalogCleanup().visit(tree)
        # A rejected credential cannot recover by resending the same request.
        # Keep _http_backup_requirement unchanged: distinct configured routes
        # still get their existing chance before returning the terminal error.
        def same_credential_retry(source):
            return replace_once(source,
                '    return _is_retryable_ai_http_status(status_code)',
                '    return status_code not in (401, 403) and _is_retryable_ai_http_status(status_code)')
        rewrite_function(tree, '_is_retryable_ai_http_response', same_credential_retry)
        def caller_retry(source):
            return replace_once(source,
                '    if _is_retryable_ai_request_exception(exc):',
                "    if isinstance(exc, requests.HTTPError) and getattr(getattr(exc, 'response', None), 'status_code', None) in (401, 403):\n        return False\n    if _is_retryable_ai_request_exception(exc):")
        rewrite_function(tree, 'is_s1_ai_retryable_request_exception', caller_retry)
    elif filename == 'adventurer_s1_expedition_runner':
        drop_definitions(tree, {'_build_s1_ecology_identity_review_binding'})
        class RemoveDiagnosticDispatch(ast.NodeTransformer):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == '_build_s1_ecology_identity_review_binding':
                    return ast.Tuple(elts=[ast.Constant(None), ast.Constant(False)], ctx=ast.Load())
                return self.generic_visit(node)
        tree = RemoveDiagnosticDispatch().visit(tree)
    elif filename == 'adventurer_s1_expedition_observer_ai_service':
        # Retain legacy record readers, but retire new diagnostic production.
        review = function(tree, 'review_expedition_result_for_gate')
        review.body.insert(1, ast.parse('ecology_identity_review_context = None').body[0])
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == '_OBSERVER_ECOLOGY_IDENTITY_RECEIPT_RULES' for t in node.targets):
                node.value = ast.Constant('')
            if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == '_RESOURCE_ECOLOGY_OBSERVER_SYSTEM_PROMPT' for t in node.targets):
                class CleanDiagnosticPrompt(ast.NodeTransformer):
                    def visit_Constant(self, part):
                        if not isinstance(part.value, str): return part
                        value = part.value.replace('，\\n以及同一次请求中可选的生态身份诊断', '')
                        value = value.replace('，\n以及同一次请求中可选的生态身份诊断', '')
                        start = value.find('当 `review_scope.ecology_identity_review.required=true`')
                        if start >= 0:
                            end = value.index('顶层固定输出', start)
                            value = value[:start] + value[end:]
                        value = value.replace('，并在身份诊断激活时加入\n`ecology_identity_diagnostic`', '')
                        value = value.replace('身份诊断无论内容如何都不改变该结果。', '')
                        value = value.replace('主题辨识度只允许进入内部身份诊断，不能取得重生成权限。', '主题辨识度不构成重生成依据。')
                        return ast.Constant(value)
                node.value = CleanDiagnosticPrompt().visit(node.value)
    else:
        tree = CatalogCleanup().visit(tree)
    ast.fix_missing_locations(tree)
    tree = ast.parse(ast.unparse(tree))
    render = runpy.run_path(str(Path(__file__).with_name('s1_public_source.py')))['render_changed_statements']
    result = render(source, original, tree)
    compile(result, relative, 'exec')
    return result.encode('utf-8')
