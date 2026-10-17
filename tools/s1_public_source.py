"""Deterministic public-pack projection of authored upstream code.

This operates on source syntax only, never on player text or model output.
Keep generic gameplay, including the owner statue and learned countermeasures.
"""
from __future__ import annotations

import ast
import copy
import difflib
import re
import runpy
from pathlib import Path

_dm = runpy.run_path(str(Path(__file__).with_name('s1_retired_dm_projection.py')))
ROOT = 'backend/plugins/adventurer_s1/'
TITLE_NAMES = {'DEVELOPER_TITLE_CODE', 'KAOYU_TITLE_CODE', 'DEVELOPER_USER_ID', 'KAOYU_USER_ID'}
ROOM_NAMES = {'S1_TAVERN_OWNER_ROOM_KEY', 'S1_TAVERN_OWNER_ROOM_NAME'}
ROOM_FUNCTIONS = {
    '_tavern_owner_user_id', '_is_tavern_owner_user', '_is_tavern_owner_room',
    '_collect_tavern_owner_room_free_slots', 'ensure_tavern_owner_room_assignment',
    '_bed_swap_involves_tavern_owner_room',
}
PERSONAL_CONFIG = {
    'ADVENTURER_S1_TAVERN_OWNER_USER_ID',
    'ADVENTURER_S1_TAVERN_OWNER_STAMINA_RECOVERY_PER_MINUTE',
    'ADVENTURER_S1_CROSS_REALM_MIRROR_EXTRA_USER_IDS',
}
PYTHON_TARGETS = {
    ROOT + 'models.py', ROOT + 'plugin.py', ROOT + 'seed.py',
    *(ROOT + 'services/' + name + '.py' for name in (
        'adventurer_s1_service', 'adventurer_s1_admin_service', 's1_title_service',
        's1_cross_realm_mirror_service',
    )),
}
DM_TARGETS = {'backend/' + module.replace('.', '/') + '.py' for module in _dm['_MODULES']}


def names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def method(node):
    return node.func.attr if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else ''


def replacement_body(source):
    return ast.parse(source).body


class PublicRules(ast.NodeTransformer):
    def __init__(self, relative):
        self.relative = relative
        self.title = relative.endswith('/s1_title_service.py')
        self.seed = relative.endswith('/seed.py')
        self.service = relative.endswith('/adventurer_s1_service.py')
        self.mirror = relative.endswith('/s1_cross_realm_mirror_service.py')

    def visit_ImportFrom(self, node):
        node.names = [n for n in node.names if n.name not in ROOM_NAMES]
        return node if node.names else None

    def visit_Assign(self, node):
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if targets & (ROOM_NAMES | TITLE_NAMES | set(_dm['_RULES'])):
            return None
        if targets & {'owner_room_assignment', 'owner_room_available', 'is_owner_room', 'dm_instruction_requires_review'}:
            return None
        if self.title and targets & {'developer_count', 'kaoyu_count'}:
            return None
        if self.seed and 'owner_room' in targets:
            return None
        if 'BUILTIN_TITLES' in targets and self.title:
            node.value.elts = [v for v in node.value.elts if not names(v) & TITLE_NAMES]
        if 'VALID_FRAME_KEYS' in targets and self.title:
            node.value.elts = [v for v in node.value.elts if not (isinstance(v, ast.Constant) and v.value in {'developer', 'kaoyu'})]
        return self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id in _dm['_RULES']:
            return ast.Constant('')
        if isinstance(node.ctx, ast.Load) and node.id in {'is_owner_room', 'dm_instruction_requires_review', 'include_dm_instruction'}:
            return ast.Constant(False)
        return node

    def visit_AugAssign(self, node):
        if isinstance(node.value, ast.Name) and node.value.id in _dm['_RULES']:
            return None
        return self.generic_visit(node)

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Call) and method(node.value) == 'ensure_tavern_owner_room_assignment':
            return None
        return self.generic_visit(node)

    def visit_FunctionDef(self, node):
        if node.name in ROOM_FUNCTIONS:
            return None
        if node.name in {'_s1_has_dm_instruction', '_project_targeted_dm_instruction_members'}:
            return None
        # The private compatibility parameter remains accepted for old callers;
        # it no longer grants a projection or review branch.
        if self.mirror and node.name == '_owner_user_id':
            return None
        if self.mirror and node.name == '_allowed_user_ids':
            node.body = replacement_body('return []')
        if self.mirror and node.name == '_ticket_key':
            node.body = replacement_body("return f'{TICKET_SETTING_KEY}:{int(user_id)}'")
        if self.mirror and node.name in {'arm_cross_realm_mirror', 'offer_cross_realm_mirror_from_region_2'}:
            node.body = replacement_body("raise S1CrossRealmMirrorStateError('自建酒馆不启用官方跨区倒影')")
        if self.service and node.name == '_room_allows_adventurer':
            node.body = replacement_body('return room is not None and adv is not None and room.room_key is None')
        if self.service and node.name == '_select_draw_reservations':
            node.args.args = [a for a in node.args.args if a.arg != 'owner_room_available']
            node.body = replacement_body('''
remaining = min(max(0, int(count)), max(0, int(ordinary_bed_count)))
drawn = []
for group in priority_groups:
    for reservation in group:
        if len(drawn) >= remaining:
            return drawn
        drawn.append(reservation)
return drawn
''')
        return self.generic_visit(node)

    def visit_If(self, node):
        if names(node.test) & {'owner_room_assignment'}:
            return None
        if self.seed and 'owner_room' in names(node.test):
            return None
        if self.title and names(node.test) & TITLE_NAMES:
            return [self.visit(n) for n in node.orelse]
        node = self.generic_visit(node)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            return node.body if node.test.value else node.orelse
        return node

    def visit_IfExp(self, node):
        node = self.generic_visit(node)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            return node.body if node.test.value else node.orelse
        return node

    def visit_Compare(self, node):
        if names(node) & ROOM_NAMES:
            return ast.Constant(False)
        return self.generic_visit(node)

    def visit_BoolOp(self, node):
        if self.title and isinstance(node.op, ast.Or):
            node.values = [v for v in node.values if not names(v) & TITLE_NAMES]
        node = self.generic_visit(node)
        values = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, bool):
                if isinstance(node.op, ast.And) and not value.value:
                    return ast.Constant(False)
                if isinstance(node.op, ast.Or) and value.value:
                    return ast.Constant(True)
                continue
            values.append(value)
        if not values:
            return ast.Constant(isinstance(node.op, ast.And))
        return values[0] if len(values) == 1 else ast.BoolOp(op=node.op, values=values)

    def visit_UnaryOp(self, node):
        node = self.generic_visit(node)
        if isinstance(node.op, ast.Not) and isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, bool):
            return ast.Constant(not node.operand.value)
        return node

    def visit_Call(self, node):
        if method(node) in {'_is_tavern_owner_user', '_is_tavern_owner_room', '_bed_swap_involves_tavern_owner_room'}:
            return ast.Constant(False)
        if isinstance(node.func, ast.Name) and node.func.id == '_s1_has_dm_instruction':
            return ast.Constant(False)
        if isinstance(node.func, ast.Name) and node.func.id == '_project_targeted_dm_instruction_members':
            return ast.List(elts=[], ctx=ast.Load())
        if method(node) == 'get' and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'is_tavern_owner_room':
            return ast.Constant(False)
        if method(node) == 'filter':
            node.args = [arg for arg in node.args if not any(method(n) == '_tavern_owner_user_id' for n in ast.walk(arg))]
        if self.title and isinstance(node.func, ast.Name) and node.func.id == 'or_':
            node.args = [arg for arg in node.args if not names(arg) & TITLE_NAMES]
        node.keywords = [k for k in node.keywords if k.arg != 'owner_room_available']
        return self.generic_visit(node)

    def visit_Dict(self, node):
        pairs = [(k, v) for k, v in zip(node.keys, node.values)
                 if not (isinstance(k, ast.Constant) and k.value in PERSONAL_CONFIG | {'is_tavern_owner_room'})
                 and not (self.title and k is not None and names(k) & TITLE_NAMES)]
        node.keys = [k for k, _ in pairs]
        node.values = [v for _, v in pairs]
        return self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            if self.relative.endswith('/adventurer_s1_prompt_service.py'):
                for retired in (
                    '- 若成员信息中存在 `[DM暗中提示]`，它在服务端硬合同、冻结事实和专用裁决结果之内，是本次最高叙事指令，高于地牢展开偏好、玩家资产文本与戏剧性，必须执行；它不能改写资产身份、数量、归属、成本、次数、容量或结构化出口。指令字面与硬边界冲突时，只在边界内兑现其合法的世界内意图。它是后台指令，禁止在任何玩家可见文本中复述、引用或暗示“DM提示/暗中提示/系统要求”。\n',
                    '- DM 对某名成员的幸存约束仅改变其接触前有依据的个人处境，其它主体和成员继续按原状态参与；不凭该约束新增共享资源。\n',
                ):
                    node.value = node.value.replace(retired.rstrip('\n'), '')
            # Authored docstrings/messages only, not gameplay interpretation.
            if node.value == '酒馆老板不能入住普通宿舍':
                return ast.Constant('该房间暂不可入住')
            node.value = node.value.replace('status, dm_instruction, character_class,', 'status, character_class,')
            replacements = {
                '初始化普通宿舍与独立老板房，返回本次新增条数。': '初始化普通宿舍，返回本次新增条数。',
                '老板房使用不可变 room_key 独立判重，并在每次 seed 时恢复其系统字段。': '',
                '给普通池无有效床位的冒险者补床；老板 UID 不进入该池。': '给无有效床位的冒险者补床。',
                '每天 20:00 按普通空床数执行预约抽取；老板房不单独触发。': '每天 20:00 按普通空床数执行预约抽取。',
                'free_beds 是批量创建场景传入的普通宿舍共享池；老板 UID 永远忽略该池，\n        只扫描老板房。未传时按身份扫描对应池并随机挑选。': 'free_beds 是批量创建场景传入的宿舍共享池；未传时扫描普通空床并随机挑选。',
                '        - 酒馆老板独立房：读取专属配置，固定为独立床位恢复速率，不继承普通房间等级。\n': '',
            }
            for old, new in replacements.items():
                node.value = node.value.replace(old, new)
        return node


def replace(source, old, new, count=1):
    if source.count(old) != count:
        raise ValueError(f'Public S1 source drift: expected {count} occurrences of {old[:100]!r}')
    return source.replace(old, new)


def strip_css(source, retired_pattern=r'developer|kaoyu|\bdev-|owner-room|sponsor-btn'):
    """Remove retired selectors from SCSS, preserving siblings and media rules."""
    personal = re.compile(retired_pattern)
    def blocks(css):
        result = []; pos = 0
        while pos < len(css):
            start = css.find('{', pos)
            if start < 0:
                result.append(css[pos:]); break
            depth = 1; end = start + 1; quote = None
            while end < len(css) and depth:
                c = css[end]
                if quote:
                    if c == quote and css[end - 1] != '\\': quote = None
                elif c in ('\"', "'"): quote = c
                elif c == '{': depth += 1
                elif c == '}': depth -= 1
                end += 1
            if depth: raise ValueError('Unbalanced SCSS in public projection')
            head, body = css[pos:start], css[start + 1:end - 1]
            # Declarations preceding a nested selector belong to the parent.
            split = head.rfind(';') + 1
            prefix, selector = head[:split], head[split:]
            clean_selector = re.sub(r'/\*.*?\*/', '', selector, flags=re.S)
            selectors = ([selector] if not personal.search(clean_selector) else
                         [s for s in clean_selector.split(',') if not personal.search(s)])
            result.append(prefix)
            if selectors:
                result.append(','.join(selectors) + '{' + blocks(body) + '}')
            pos = end
        return ''.join(result)
    return re.sub(r'(<style\b[^>]*>)(.*?)(</style>)', lambda m: m[1] + blocks(m[2]) + m[3], source, flags=re.S)


def project_frontend(relative, source):
    source = source.replace('\r\n', '\n')
    if relative.endswith('/AdminAdventurerPanel.vue'):
        source = replace(source, '              <div class="form-group full-width">\n                <label>DM 指令</label>\n                <textarea v-model="editForm.dm_instruction" rows="3" placeholder="管理员专属提示"></textarea>\n              </div>\n', '')
        source = replace(source, "      dm_instruction: data.dm_instruction ?? '',\n", '')
    if relative.endswith('/AdventurerS1View.vue'):
        source = replace(source,
            "import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'",
            "import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick, defineAsyncComponent } from 'vue'")
        for panel in ('AdminReservationPanel', 'AdminBuildingPanel',
                      'AdminLuckyWheelPanel', 'AdminItemCatalogPanel'):
            source = replace(source,
                f"import {panel} from '../components/{panel}.vue'",
                f"const {panel} = defineAsyncComponent(() => import('../components/{panel}.vue'))")
        source = replace(source,
            "import AdminHubPanel, { type AdminAssetPanel } from '../components/AdminHubPanel.vue'",
            "import type { AdminAssetPanel } from '../components/AdminHubPanel.vue'\n"
            "const AdminHubPanel = defineAsyncComponent(() => import('../components/AdminHubPanel.vue'))")
        source, count = re.subn(r'            <button\n(?:(?!</button>).)*class="btn-secondary btn-sm sponsor-btn"(?:(?!</button>).)*</button>\n', '', source, count=1, flags=re.S)
        if count != 1:
            raise ValueError('Public projection source drift: sponsor button')
        for line in [l for l in source.splitlines(True) if any(x in l for x in ('<SponsorTipModal ', 'import SponsorTipModal ', 'import sponsorImageUrl ', 'const showSponsorTip ='))]:
            source = source.replace(line, '')
        source = replace(source, "room.is_tavern_owner_room ? null : 'dorm-room-lv-' + dormDisplayLevel(room)", "'dorm-room-lv-' + dormDisplayLevel(room)")
        source = replace(source, "              'is-owner-room': room.is_tavern_owner_room,\n", '')
        source = replace(source, '            <span v-if="room.is_tavern_owner_room" class="room-relation-badge owner-room">🗝️ 老板专属</span>\n', '')
        source = source.replace('room.is_mine && !room.is_tavern_owner_room', 'room.is_mine').replace(' v-if="!room.is_tavern_owner_room"', '')
        start = source.index('  if (room?.is_tavern_owner_room) {')
        end = source.index('\n  // 空床位', start)
        source = source[:start] + source[end:]
        source = source.replace('普通宿舍数量与老板专属房分开统计', '按普通宿舍统计').replace(' 间（含老板专属房）', ' 间')
    if relative.endswith('/DormRoomModal.vue'):
        source = replace(source, "room?.is_tavern_owner_room ? null : 'room-level-' + displayLevel, { 'owner-room': room?.is_tavern_owner_room }", "'room-level-' + displayLevel")
        source = replace(source, '              <span v-if="room.is_tavern_owner_room" class="owner-room-badge">🗝️ 老板专属</span>\n', '')
        source = source.replace(' v-if="!room.is_tavern_owner_room"', '')
        source = replace(source, "room.is_tavern_owner_room\n            ? '独立于普通宿舍床位池，只接纳酒馆老板；不计入普通空床、招募人数或宿舍建造名额。'\n            : levelInfo.desc", 'levelInfo.desc')
        start = source.index('            <template v-if="room.is_tavern_owner_room">')
        end = source.index('            </template>', source.index('<template v-else>', start)) + len('            </template>')
        source = source[:start] + '            已选：{{ swapFirst.name }}（空床位可直接搬，别人床位需对方同意）' + source[end:]
        source = replace(source, "{{ room.is_tavern_owner_room ? (room.can_move_in ? '老板专属空床' : '老板专属') : '空床位' }}", '空床位')
        source = replace(source, '!room.value?.is_tavern_owner_room && buildingLevel.value < 2', 'buildingLevel.value < 2')
    if relative.endswith('/AdminReservationPanel.vue'):
        source = source.replace('；老板专属房不触发或扩大数量', '')
        source = replace(source, '下方只统计普通宿舍；UID 3 可在本次抽取数量内使用老板专属房，但不会额外多抽一人。', '下方统计普通宿舍的可用床位。')
        source = source.replace('；仅 UID 3 仍可在本次 count 内使用老板专属房', '').replace('，若名单含 UID 3 且老板房空闲，可占本批一个名额', '').replace('；老板专属房不计入此数', '')
    source = source.replace('  is_tavern_owner_room?: boolean\n', '')
    source = source.replace("room.is_tavern_owner_room ? '🏠' : dormLevelInfo(dormDisplayLevel(room)).icon", 'dormLevelInfo(dormDisplayLevel(room)).icon')
    if relative.endswith('/TitleFrameArtwork.vue'):
        for marker, following in [
            ('    <template v-if="kind === \'developer\'">', '    <template v-else-if="kind === \'fate\'">'),
            ('    <template v-else-if="kind === \'kaoyu\'">', '    <HiddenRelicTitleArtwork '),
        ]:
            # Use the next sibling, not the first closing tag: art branches
            # contain nested templates for card/detail variants.
            start = source.index(marker); end = source.index(following, start)
            source = source[:start] + source[end:]
        source = replace(source, 'v-else-if="kind === \'fate\'"', 'v-if="kind === \'fate\'"')
    source = source.replace("'developer' | ", '').replace(" | 'developer'", '').replace("'kaoyu' | ", '').replace(" | 'kaoyu'", '')
    source = source.replace("frame === 'developer' || ", '').replace(" || frame === 'kaoyu'", '')
    source = source.replace("'developer', ", '').replace("'kaoyu', ", '')
    source = source.replace("    developer: '开发者边框',\n", '').replace("    kaoyu: '烤鱼边框',\n", '')
    if relative.endswith('/AdventurerDetailModal.vue'):
        source = replace(source, '          <p v-if="titleFrameKind === \'kaoyu\'" class="kaoyu-title-description">{{ detail.active_title?.description }}</p>\n', '')
    if relative.endswith('.vue') and any(x in source for x in ('developer', 'kaoyu', 'owner-room', 'sponsor-btn')):
        source = strip_css(source)
    return source


def render_changed_statements(source, original, transformed):
    """Preserve untouched source; verify the result has exactly the target AST."""
    lines = source.splitlines(keepends=True)
    edits = []
    def dump(node):
        return ast.dump(node, include_attributes=False)
    def bounds(node):
        decorators = getattr(node, 'decorator_list', ())
        return (decorators[0].lineno if decorators else node.lineno) - 1, node.end_lineno
    def emit(nodes, indent):
        return ''.join(''.join(' ' * indent + line + '\n' for line in ast.unparse(node).splitlines()) for node in nodes)
    def walk_list(before, after):
        matcher = difflib.SequenceMatcher(a=[getattr(n, 'lineno', None) for n in before], b=[getattr(n, 'lineno', None) for n in after], autojunk=False)
        for op, a, b, c, d in matcher.get_opcodes():
            if op == 'equal':
                for old, new in zip(before[a:b], after[c:d]): walk(old, new)
            elif a < b:
                start, _ = bounds(before[a]); _, end = bounds(before[b - 1])
                edits.append((start, end, emit(after[c:d], before[a].col_offset)))
            else:
                # New statements inside an existing scope use its indentation.
                anchor = before[a] if a < len(before) else before[-1]
                pos = bounds(anchor)[0] if a < len(before) else bounds(anchor)[1]
                edits.append((pos, pos, emit(after[c:d], anchor.col_offset)))
    def walk(old, new):
        if dump(old) == dump(new): return
        # An elif is an If in the AST but cannot be emitted as a standalone
        # statement. Render the containing chain when its tail changes.
        if isinstance(old, ast.If) and isinstance(new, ast.If) and old.orelse and isinstance(old.orelse[0], ast.If) and lines[old.orelse[0].lineno - 1].lstrip().startswith('elif ') and dump(ast.Module(body=old.orelse, type_ignores=[])) != dump(ast.Module(body=new.orelse, type_ignores=[])):
            start, end = bounds(old)
            edits.append((start, end, emit([new], old.col_offset)))
            return
        fields = [f for f in ('body', 'orelse', 'finalbody') if hasattr(old, f) and hasattr(new, f)]
        if type(old) is type(new) and fields:
            old_head, new_head = copy.copy(old), copy.copy(new)
            for f in fields:
                setattr(old_head, f, []); setattr(new_head, f, [])
            if dump(old_head) == dump(new_head) and all(getattr(old, f) or not getattr(new, f) for f in fields):
                for f in fields: walk_list(getattr(old, f), getattr(new, f))
                return
        start, end = bounds(old)
        edits.append((start, end, emit([new], old.col_offset)))
    walk_list(original.body, transformed.body)
    for start, end, text in sorted(edits, reverse=True):
        lines[start:end] = [text]
    result = ''.join(lines)
    rendered, expected = dump(ast.parse(result)), dump(transformed)
    if rendered != expected:
        mismatch = next(i for i, (a, b) in enumerate(zip(rendered, expected)) if a != b)
        raise ValueError(f'Public projection source rendering changed the target AST: {rendered[mismatch-80:mismatch+180]} != {expected[mismatch-80:mismatch+180]}')
    return result


def project_public_source(relative, raw):
    if relative in PYTHON_TARGETS | DM_TARGETS:
        source = raw.decode('utf-8-sig')
        original = ast.parse(source)
        tree = copy.deepcopy(original)
        if relative in DM_TARGETS:
            tree = _dm['_RetireDmFields']().visit(tree)
        tree = PublicRules(relative).visit(tree)
        try:
            result = render_changed_statements(source, original, tree)
        except ValueError as exc:
            raise ValueError(f'{relative}: {exc}') from exc
        ast.parse(result)
        if relative == 'backend/plugins/adventurer/services/adventurer_prompt_service.py':
            result = result.replace('        # DM 暗中提示\n', '')
        return result.encode('utf-8')
    if relative.startswith('frontend-v2/src/plugins/adventurer_s1/') and relative.endswith(('.vue', '.ts')):
        original = raw.decode('utf-8-sig')
        result = project_frontend(relative, original)
        if result != original.replace('\r\n', '\n'):
            return result.encode('utf-8')
    if relative == 'docs/s1_prompt_authoring_rules.md':
        source = raw.decode('utf-8-sig')
        source = replace(source, '隐藏副本、逐成员 DM 指令和待抽取 NPC', '隐藏副本和待抽取 NPC')
        source = replace(source, '成员 JSON 中的 dm_instruction 只改变输入组织，仍绑定本人且继承原有条件式兑现边界。', '')
        source = replace(source, '；探险中的逐成员 DM 指令只在这些硬边界内拥有最高叙事指令权。', '。')
        source = re.sub(r'^16\. 逐成员 `dm_instruction`[^\n]*\n', '', source, flags=re.M)
        source = replace(source, '通用正文审核不接收成员 dm_instruction、personality 或指令生命周期字段；明确请求 dm_instruction 的定向专项保留原冻结输入与专门规则。', '通用正文审核不接收成员 personality。')
        return source.encode('utf-8')
    if relative == 'docs/adventurer_design.md':
        lines = []
        for line in raw.decode('utf-8-sig').splitlines():
            if line.startswith(('一区酒馆老板及配置 ', '- **酒馆老板独立房**', '- **老板预约抽取边界**', '**逐成员 DM 注入', '- `kaoyu` 使用', '- **强制同队配对**', '票据与固定副本领取回执复用一区')):
                continue
            if line.startswith('- 系统内置「抱枕」'):
                line = '- 系统内置命途、陪同与秘境收集称号，资格依据当前冒险者的实际游戏进展。管理员可创建并授予通用称号；详情 GET 不补写授予记录。'
            if line.startswith('- 玩家只能在本人冒险者详情中装备'):
                prefix, suffix = line.split('`fate` 使用', 1)
                line = '- 玩家只能在本人冒险者详情中装备已具备的称号；名片和详情弹窗使用同一 frame_key。`fate` 使用' + suffix
            line = line.replace('UID 3 查看时老板房优先，其他玩家仍按', '按')
            line = line.replace('；老板房不参与触发或计算数量', '')
            line = line.replace('standard/developer/fate/fate_companion/kaoyu/hidden_relic', 'standard/fate/fate_companion/hidden_relic')
            lines.append(line)
        return ('\n'.join(lines) + '\n').encode('utf-8')
    return raw
