"""Remove fixed public-server control surfaces from the portable S1 export.

The platform owns tavern selection, membership and process lifecycle. Legacy
realm fields in saved gameplay objects remain compatible; no Prompt is changed.
"""
from __future__ import annotations

import ast
import re


ROOT = 'backend/plugins/adventurer_s1/'
UI = 'frontend-v2/src/plugins/adventurer_s1/'
OMIT = {
    *(ROOT + 'services/' + name + '.py' for name in (
        's1_region2_runtime_host_service', 's1_region2_runtime_contract_service',
        's1_region2_runtime_relay_service', 's1_runtime_socket_relay_service',
        's1_admin_database_read_service',
        's1_region2_runtime_scheduler_service',
    )),
    UI + 'components/PublicRealmLogFeed.vue',
    UI + 'components/PublicRealmLogReader.vue',
    UI + 'api/publicRealmLogs.ts',
    UI + 'composables/s1AiBalanceRecovery.ts',
}
RETIRED_ROUTE_FUNCTIONS = {
    's1_admin_realm_targetable', 'receive_region_2_runtime_socket_relay',
    '_region_2_runtime_status', 'get_realm_admission', 'update_realm_admission',
    'restart_region_2_runtime', '_match_original_s1_view', '_dispatch_local_s1_view',
    '_s1_identity_snapshot', '_execute_s1_gateway_request',
    'route_s1_gateway_options', 'route_s1_gateway',
    'cross_realm_mirror_status', 'arm_cross_realm_mirror_route',
    'admin_update_ai_config', 'admin_refresh_ai_balance', 'admin_resume_ai_credential',
    '_admin_sync_ai_config_from_region_1', 'admin_preview_ai_config_region_1_sync',
    'admin_apply_ai_config_region_1_sync',
}


def replace(source, old, new='', count=1):
    if source.count(old) != count:
        raise ValueError(f'Platform projection drift: expected {count} of {old[:90]!r}')
    return source.replace(old, new)


def cut(source, start, stop, replacement=''):
    if source.count(start) != 1 or source.count(stop) != 1:
        raise ValueError(f'Platform projection boundaries drifted: {start!r}, {stop!r}')
    first = source.index(start)
    last = source.index(stop, first)
    return source[:first] + replacement + source[last:]


def project_routes(source):
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    ranges = []
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in RETIRED_ROUTE_FUNCTIONS:
            found.add(node.name)
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            ranges.append((start - 1, node.end_lineno))
        elif isinstance(node, ast.ImportFrom) and node.module == 'plugins.adventurer_s1.services.s1_admin_database_read_service':
            ranges.append((node.lineno - 1, node.end_lineno))
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Name)
              and node.value.func.id == 'register_s1_database_read_routes'):
            ranges.append((node.lineno - 1, node.end_lineno))
    if found != RETIRED_ROUTE_FUNCTIONS:
        raise ValueError(f'Fixed-server routes drifted: {RETIRED_ROUTE_FUNCTIONS - found}')
    for start, end in sorted(ranges, reverse=True):
        del lines[start:end]
    result = ''.join(lines).replace('@s1_admin_realm_targetable\n', '')
    result = replace(result, "        return error('二区准入必须通过独立 Runtime 预检接口修改')",
                     "        return error('平台酒馆不支持旧区服准入设置')")
    ast.parse(result)
    return result


def project_admin_hub(source):
    source = replace(source, "secondaryAuthScope.value === 'read'", 'false')
    source = cut(source, '      <label class="admin-realm-target">', '    </header>')
    source = cut(source, '        <div class="admin-setting-block admin-setting-block-separated">',
                 '      <div v-if="opsSection === \'level-cap\'"', '      </div>\n\n')
    source = cut(source, 'interface Region2SupervisorStatus {', 'const ADMIN_TABS:')
    source = cut(source, 'const region2Enabled = ref(false)', 'const levelCapResetLoading')
    source = cut(source, 'let region2RestartPollRevision = 0', 'async function saveNotice()')
    source = cut(source, 'function formatRegion2RuntimePressure(', 'function toDatetimeLocal(')
    source = cut(source, 'onMounted(() => {\n  const storedPlayerRealm', 'async function loadNoticeDraft()',
                 'onMounted(() => { panelMounted.value = true })\n\n')
    source = replace(source, '      void loadRegion2Setting()\n')
    source = replace(source, 'const adminRealmTarget = ref<S1AdminRealmTarget>(getS1AdminRealmTarget())\n')
    for line in ('  getS1AdminRealmTarget,\n', '  getS1PlayerRealmView,\n',
                 '  setS1AdminRealmTarget,\n', '  setS1PlayerRealmView,\n', '  type S1AdminRealmTarget,\n'):
        source = replace(source, line)
    source = replace(source, 'computed, onMounted, onUnmounted, ref, watch', 'computed, onMounted, ref, watch')
    source = replace(source, "label: '收益监控'", "label: '收益记录'")
    source = replace(source, "shortDescription: '金币与物品异常'", "shortDescription: '历史与人工查询'")
    source = replace(source, "description: '独立审阅金币与物品异常增长，记录来源、证据与处理状态。'",
                     "description: '查看历史收益记录或人工回查；后台不再自动扫描。'")
    # These routes exist only on the original stock site, not this platform.
    source = cut(source, '          <router-link to="/admin/token-usage"', '        </div>\n      </div>\n\n      <div v-if="assetsSection')
    source = cut(source, '      <div v-if="assetsSection === \'legacy\'"', '    </div>\n  </div>\n  <div v-else class="admin-auth-loading"')
    source = replace(source, "type AssetsSection = 'links' | 'legacy'", "type AssetsSection = 'links'")
    source = replace(source, "  { id: 'legacy', label: '旧版回溯', icon: '📦' },\n")
    return source


def project_game_view(source):
    source = replace(source, "import { useAdventurerS1Store", "import { isS1BuildingVisible, isS1DungeonVisible } from '@/runtime/s1FeatureVisibility'\nimport { useAdventurerS1Store")
    source = replace(source, 'const specialDungeons = ref<SpecialDungeonInfo[]>([])',
        'const specialDungeons = ref<SpecialDungeonInfo[]>([])\nconst visibleSpecialDungeons = computed(() => specialDungeons.value.filter(isS1DungeonVisible))')
    source = replace(source, 'v-if="specialDungeons.length === 0"', 'v-if="visibleSpecialDungeons.length === 0"')
    source = replace(source, 'v-for="d in specialDungeons"', 'v-for="d in visibleSpecialDungeons"')
    source = replace(source, '转职、顿悟与祛厄门票可由寻宝获得；裂隙通行证的获取途径暂未开放。',
        '转职、顿悟与祛厄门票可由寻宝获得。')
    source = replace(source, '  for (const b of directoryBuildings) {',
        '  for (const b of directoryBuildings) {\n    if (!isS1BuildingVisible(b)) continue')
    source = replace(source, ':readonly="adminSecondaryScope === \'read\'"', ':readonly="false"')
    source = replace(source, "!isAdmin.value && !(adminSecondaryScope.value === 'read' && kind === 'catalog')", '!isAdmin.value')
    source = cut(source, '          <span\n            v-if="viewedRealmCode"', '        </h2>')
    source = cut(source, '        <p v-if="playerRealmNoticeText"', '      <div class="advs1-beacon"', '      </div>\n')
    source = cut(source, '    <div\n      v-if="store.crossRealmMirrorVisible', '    <S1BetaNotice')
    source = cut(source, '      <div class="log-toolbar" role="group" aria-label="日志区服范围">', '      <div class="log-toolbar">')
    source = replace(source, '      <Pagination v-if="store.logsPagination" :pagination="store.logsPagination" @go="goLogPage" />\n      </template>',
                     '      <Pagination v-if="store.logsPagination" :pagination="store.logsPagination" @go="goLogPage" />')
    source = replace(source, "import PublicRealmLogFeed from '../components/PublicRealmLogFeed.vue'\n")
    source = replace(source, 'const showBothRealmLogs = ref(false)\n')
    source = cut(source, 'let adminRealmAlignmentRequested = false', 'const TABS = computed(')
    source = cut(source, 'const playerRealmAssignment = computed(', 'const isRealmVisitor = computed(',
                 "const viewedRealmCode = computed<S1PlayerRealmView>(() => store.realmViewCode ?? 'region_1')\n")
    source = cut(source, 'const crossRealmMirrorDescription = computed(', 'const playableMyAdventurers = computed(')
    source = cut(source, 'async function refreshCrossRealmMirrorStatus(', 'async function loadMine()')
    source = replace(source, '        ...(store.crossRealmMirrorVisible ? [() => refreshCrossRealmMirrorStatus(false)] : []),\n')
    source = replace(source, 'const playerRealmViewPreference = ref<S1PlayerRealmView | null>(requestedPlayerRealmView)\n')
    for line in ('  getS1AdminRealmTarget,\n', '  setS1AdminRealmTarget,\n', '  setS1PlayerRealmView,\n'):
        source = replace(source, line)
    source = cut(source, 'async function syncS1AdminSocketRealmSubscription(', 'const bufferedAdventurerEvents:')
    source = cut(source, "  socket.on('s1_admin_realm_subscription_revoked'", "  socket.on('socket_connected'")
    source = replace(source, 'let adminSocketSubscriptionRevision = 0\n')
    source = replace(source, '  if (canViewAdmin.value) {\n    void syncS1AdminSocketRealmSubscription()\n  }\n')
    source = replace(source, '  adminSocketSubscriptionRevision += 1\n')
    source = replace(source, '  socket.unsubscribeS1AdminRealm()\n')
    source = cut(source, '    const adminObserverExpected = canViewAdmin.value',
                 '    if (realtimeHealthy) return', '    const realtimeHealthy = socket.isSocketHealthy()\n')
    source = replace(source, "    } else if (activeTab.value === 'logs') {\n      void refreshCurrentLogs(true).then((refreshed) => {\n        if (refreshed) markTabFresh('logs')\n      })\n    }\n  }, FALLBACK_POLL_INTERVAL_MS)",
        "    } else {\n      void refreshActiveTabAfterReconnect().catch(() => markTabDirty(activeTab.value))\n    }\n  }, FALLBACK_POLL_INTERVAL_MS)")
    return source


def project_create_panel(source):
    source = replace(source, '        <span v-if="reservationRealmLabel">所在区：<strong>{{ reservationRealmLabel }}</strong></span>\n')
    source = cut(source, '      <div class="realm-notice" role="note">', '      <div class="form-grid">',
                 '      <p class="panel-tip">预约与角色属于当前酒馆；其他酒馆的角色和进度独立保存。</p>\n\n')
    source = cut(source, 'const reservationRealmLabel = computed', '// 仅中文（CJK 基本区）')
    source = cut(source, 'function realmLabel(', 'function toggleItem(')
    return source


def project_store(source):
    source = cut(source, '    async fetchRealmAdmission() {', '    // ── 建筑使用流程')
    # Match whole named actions up to the next sibling action, never their nested braces.
    for name in ('fetchCrossRealmMirrorStatus', 'armCrossRealmMirror'):
        pattern = rf'^    async {name}\(.*?(?=^    (?:async |//))'
        source, count = re.subn(pattern, '', source, count=1, flags=re.M | re.S)
        if count != 1:
            raise ValueError(f'Missing retired store action: {name}')
    source, count = re.subn(r'^export interface CrossRealmMirrorStatus \{.*?^}\n', '', source, count=1, flags=re.M | re.S)
    if count != 1:
        raise ValueError('Missing retired mirror status interface')
    source = re.sub(r'^    crossRealmMirror(?:Visible|Status|Loading):[^\n]*\n', '', source, flags=re.M)
    source = replace(source,
        '            this.crossRealmMirrorVisible = d.cross_realm_mirror?.visible === true\n'
        '            if (!this.crossRealmMirrorVisible) {\n'
        '              this.crossRealmMirrorStatus = null\n'
        '              this.crossRealmMirrorLoading = false\n'
        '            }\n')
    source = re.sub(r'^      this\.crossRealmMirror(?:Visible|Status|Loading) =[^\n]*\n', '', source, flags=re.M)
    source = replace(source, "realm_label: membershipRealm === 'region_2' ? '二区' : '一区'", "realm_label: '当前酒馆'")
    return source


def _project_platform_source(relative, raw, *, strip_css):
    if relative in OMIT:
        return None
    if relative == ROOT + 'services/adventurer_s1_service.py':
        source = raw.decode('utf-8-sig')
        # Embedded workers recover persisted building sessions from each
        # tavern's own database; they do not claim Redis delivery durability.
        source = replace(source, 'if not AsyncTaskService.has_durable_queue():',
            'if not (AsyncTaskService.has_durable_queue() or AsyncTaskService.can_run_database_startup_recovery()):')
        return source.encode('utf-8')
    if relative == ROOT + 'services/adventurer_s1_settings_service.py':
        source = raw.decode('utf-8-sig')
        tree = ast.parse(source)
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'SETTING_KEYS' for t in n.targets))
        pairs = [(k, v) for k, v in zip(assignment.value.keys, assignment.value.values)
                 if not (isinstance(k, ast.Constant) and k.value == 'region_2_enabled')]
        if len(pairs) != len(assignment.value.keys) - 1:
            raise ValueError('Missing fixed-realm setting')
        assignment.value.keys = [k for k, _ in pairs]
        assignment.value.values = [v for _, v in pairs]
        lines = source.splitlines(keepends=True)
        lines[assignment.lineno - 1:assignment.end_lineno] = [ast.unparse(assignment) + '\n']
        source = ''.join(lines)
        source = cut(source, "        if current_app.config.get('ADVENTURER_S1_FIXED_REALM_CODE') == 'region_2':",
                     '        updated: list[str] = []',
                     '        from services.scheduler_service import SchedulerService as scheduler_service\n')
        return source.encode('utf-8')
    replacements = {
        UI + 'components/AdminLimitedShopPanel.vue': (
            ("{{ realm === 'region_2' ? '二区' : '一区' }}", '当前酒馆'),
            ('保存只影响当前区服。', '保存只影响当前酒馆。'),
        ),
        UI + 'utils/adventurerExport.ts': (
            ("    `区服：${adventurer.realm_code === 'region_2' ? '二区' : '一区'}`,\n", ''),
        ),
    }
    if relative in replacements:
        source = raw.decode('utf-8-sig').replace('\r\n', '\n')
        for old, new in replacements[relative]:
            source = replace(source, old, new)
        return source.encode('utf-8')
    targets = {
        ROOT + 'routes.py': project_routes,
        UI + 'components/AdminHubPanel.vue': project_admin_hub,
        UI + 'views/AdventurerS1View.vue': project_game_view,
        UI + 'components/CreateAdventurerPanel.vue': project_create_panel,
        UI + 'stores/adventurerS1.ts': project_store,
    }
    if relative in targets:
        source = targets[relative](raw.decode('utf-8-sig').replace('\r\n', '\n'))
        if relative.endswith('.vue'):
            source = strip_css(source, retired_pattern=r'admin-realm-target|advs1-realm-(?:browser|badge|note)|cross-realm-mirror|both-realm-logs|realm-notice|legacy-card')
        return source.encode('utf-8')
    if relative == UI + 'components/AdminGainAnomalyPanel.vue':
        source = raw.decode('utf-8-sig')
        source = replace(source, '<h6>📈 收益异常监控</h6>', '<h6>📈 收益记录与人工查询</h6>')
        source = replace(source, '从 S1 金币与物品权威账本识别异常增长。这里只做重点标记与审计，不会自动冻结、扣款或回收物品。',
                         '查看历史收益记录，或按需人工回查金币与物品账本。自建版不运行自动监控，也不会自动冻结、扣款或回收物品。')
        return source.encode('utf-8')
    if relative in {UI + 'components/AdminAdventurerPanel.vue', UI + 'components/TavernUpgradePanel.vue'}:
        source = raw.decode('utf-8-sig').replace('\r\n', '\n')
        source = source.replace("realm === 'region_2' ? '区服 2' : '区服 1'", "'当前酒馆'")
        source = source.replace("realm === 'region_2' ? '二区体验服' : '一区正式服'", "'当前酒馆'")
        if relative.endswith('/AdminAdventurerPanel.vue'):
            source = cut(source, '    <!-- 不活跃提醒 -->', '    <div class="batch-actions">',
                '    <button type="button" class="btn-secondary btn-xs" @click="toggleInactiveView">\n'
                "      {{ showInactiveOnly ? '返回全部列表' : '筛选不活跃角色' }}\n    </button>\n\n")
            source = cut(source, 'async function fetchInactiveCount() {', 'async function fetchList(')
            for line in ('const inactiveCount = ref(0)\n', 'const inactiveHours = ref(24)\n',
                         '      fetchInactiveCount()\n', '  fetchInactiveCount()\n',
                         '        inactiveCount.value = res.data.inactive_count ?? adventurers.value.length\n',
                         '        inactiveHours.value = res.data.inactive_hours ?? 24\n'):
                source = replace(source, line)
            source = replace(source, 'await Promise.all([fetchList(pagination.value?.page ?? 1), fetchInactiveCount()])',
                             'await fetchList(pagination.value?.page ?? 1)', count=2)
            source = strip_css(source, retired_pattern=r'inactive-alert')
        return source.encode('utf-8')
    return raw


def project_platform_source(relative, raw, *, strip_css):
    projected = _project_platform_source(relative, raw, strip_css=strip_css)
    # Persistent gameplay references survive document reloads. Namespace the
    # original keys (including storage events and Web Locks) by platform tavern.
    # Cosmetic preferences may remain global; no legacy operation is migrated
    # because its owning tavern cannot be established safely.
    storage_keys = {
        UI + 'composables/useAmbientNpcEncounter.ts': (
            's1:ambient-npc:pending-action:',
            's1:ambient-npc:pending-action:v1:',
            's1-ambient-npc-pending-action-v1',
        ),
        UI + 'components/AdminItemCatalogPanel.vue': (
            'advs1:last-companion-continuity-repair-run',
        ),
        UI + 'views/AdventurerS1View.vue': ('adventurer_s1_task_board_hint',),
    }
    if projected is None or relative not in storage_keys:
        return projected
    source = projected.decode('utf-8-sig').replace('\r\n', '\n')
    for key in storage_keys[relative]:
        source = replace(source, repr(key), f'tavernStorageKey({key!r})')
    declaration = "import { tavernStorageKey } from '@/runtime/activeTavern'\n"
    if relative.endswith('.vue'):
        source = replace(source, '<script setup lang="ts">\n', '<script setup lang="ts">\n' + declaration)
    else:
        source = declaration + source
    return source.encode('utf-8')
