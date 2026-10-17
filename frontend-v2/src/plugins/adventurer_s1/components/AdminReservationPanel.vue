<template>
  <section class="advs1-card">
    <!-- 抽取区 -->
    <div class="draw-section">
      <div class="draw-settings-row">
        <label class="draw-setting-toggle">
          <input
            v-model="autoDrawEnabled"
            type="checkbox"
            :disabled="savingAutoDraw"
            @change="onAutoDrawToggle"
          />
          <span>宿舍完工后自动抽取</span>
        </label>
        <span class="draw-setting-hint">开启后，新宿舍建造完毕将按当前普通空床数自动执行抽取</span>
      </div>
      <div class="draw-settings-row">
        <label class="draw-setting-toggle">
          <input
            v-model="dailyDrawEnabled"
            type="checkbox"
            :disabled="savingDailyDraw"
            @change="onDailyDrawToggle"
          />
          <span>每天 20:00 自动抽取</span>
        </label>
        <span class="draw-setting-hint">开启后，每晚北京时间 20:00 按当前普通空床数自动执行抽取</span>
      </div>
      <div class="draw-settings-row">
        <label class="draw-setting-toggle">
          <input
            v-model="waitingNpcEnabled"
            type="checkbox"
            :disabled="savingWaitingNpc"
            @change="onWaitingNpcToggle"
          />
          <span>允许待抽玩家成为路人 NPC</span>
        </label>
        <span class="draw-setting-hint">开启后，每场只向剧情 AI 提供少量可选候选；冒险者仍是主角，候选不强制出场，也可能按剧情受伤、失踪或死亡</span>
      </div>
      <div class="draw-settings-row">
        <label class="draw-setting-toggle">
          <input
            v-model="autoAltPriorityEnabled"
            type="checkbox"
            :disabled="savingAutoAltPriority"
            @change="onAutoAltPriorityToggle"
          />
          <span>同伙预约自动降为低优先级</span>
        </label>
        <span class="draw-setting-hint">只处理临时提升预约；沿用同伙检测的注册/登录 IP 与 /24 网段关联，开启时立即检查，此后每分钟、玩家提升时和抽取前检查</span>
      </div>
      <p class="draw-rule-hint">抽取时：24 小时内登录过可参与；或玩家在排队页手动「临时提高优先级」（6 小时有效）亦视为活跃。下方统计普通宿舍的可用床位。</p>
      <div class="draw-row">
        <label class="draw-label">抽取数量</label>
        <input v-model.number="drawCount" type="number" min="1" max="50" class="advs1-input draw-input" />
        <button type="button" class="advs1-submit" :disabled="drawCount < 1 || drawing" @click="draw">
          {{ drawing ? '抽取中…' : '🎲 执行抽取' }}
        </button>
        <span
          v-if="store.reservationsCapacity"
          class="draw-capacity"
          :class="capacityWarnClass"
          :title="capacityTitle"
        >
          普通编制 {{ store.reservationsCapacity.alive_count }} / 普通床位 {{ store.reservationsCapacity.total_beds }}
          <span class="draw-capacity-sub">（普通空床 {{ store.reservationsCapacity.empty_beds }}）</span>
          <span
            v-if="(store.reservationsCapacity.duplicate_beds ?? 0) > 0"
            class="draw-capacity-dup"
          >
            · 重复占床 {{ store.reservationsCapacity.duplicate_beds }}
          </span>
        </span>
        <button
          type="button"
          class="btn-repair-beds"
          :disabled="repairingBeds"
          :title="repairBedsTitle"
          @click="repairDuplicateBeds"
        >
          {{ repairingBeds ? '修复中…' : '🔧 修复重复占床' }}
        </button>
      </div>
    </div>

    <!-- 解封申请审核 -->
    <div class="appeal-section">
      <div class="appeal-section-head">
        <h4 class="appeal-section-title">📦 解封申请</h4>
        <span class="filter-count">共 {{ store.archiveAppealsPagination?.total ?? 0 }} 条</span>
      </div>
      <div class="filter-row appeal-filter-row">
        <input
          v-model="appealSearchQuery"
          type="text"
          class="advs1-input search-input"
          placeholder="搜索冒险者名 / 玩家名 / 玩家ID…"
          @input="onAppealSearchInput"
        />
        <select v-model="appealStatusFilter" class="advs1-input" @change="loadAppeals(1)">
          <option value="">全部状态</option>
          <option value="pending">待审核</option>
          <option value="approved">已通过</option>
          <option value="rejected">已拒绝</option>
        </select>
      </div>
      <div v-if="store.archiveAppealsLoading" class="advs1-state">加载中…</div>
      <div v-else-if="store.archiveAppeals.length === 0" class="advs1-state">暂无解封申请</div>
      <div v-else class="appeal-list">
        <div v-for="a in store.archiveAppeals" :key="a.id" class="appeal-card">
          <div class="appeal-card-head">
            <span class="appeal-user">{{ a.nickname || a.username }} · {{ a.adventurer_name }}</span>
            <span :class="['appeal-status', 'st-' + a.status]">{{ appealStatusLabel(a.status) }}</span>
          </div>
          <div class="appeal-card-body">
            <p class="appeal-reason"><strong>申请理由：</strong>{{ a.reason }}</p>
            <p v-if="a.archive_reason" class="appeal-meta">封存原因：{{ a.archive_reason }}</p>
            <p v-if="a.reject_reason && a.status === 'rejected'" class="appeal-reject">拒绝原因：{{ a.reject_reason }}</p>
            <p v-if="a.reservation_id && a.status === 'approved'" class="appeal-meta">已入队 · 预约 #{{ a.reservation_id }}</p>
          </div>
          <div v-if="a.status === 'pending'" class="appeal-card-actions">
            <button type="button" class="advs1-submit btn-sm" :disabled="appealActionId === a.id" @click="approveAppeal(a.id)">
              {{ appealActionId === a.id ? '处理中…' : '通过并入队' }}
            </button>
            <button type="button" class="btn-reject" @click="openAppealReject(a.id)">拒绝</button>
          </div>
        </div>
      </div>
      <Pagination
        v-if="store.archiveAppealsPagination"
        :pagination="store.archiveAppealsPagination"
        @go="loadAppeals"
      />
    </div>

    <!-- 筛选 -->
    <div class="filter-row">
      <input
        v-model="searchQuery"
        type="text"
        class="advs1-input search-input"
        placeholder="搜索冒险者名 / 玩家名 / 玩家ID…"
        @input="onSearchInput"
      />
      <select v-model="statusFilter" class="advs1-input" @change="onListFilterChange">
        <option value="">全部状态</option>
        <option value="pending">等待中</option>
        <option value="drawn">已抽中</option>
        <option value="rejected">已拒绝</option>
      </select>
      <select v-model="priorityFilter" class="advs1-input" @change="onListFilterChange">
        <option value="">全部优先级</option>
        <option value="high">高优先级</option>
        <option value="elevated">临时提升</option>
        <option value="normal">普通</option>
        <option value="low">低优先级</option>
      </select>
      <button type="button" class="btn-detect-alt" :disabled="detectingAlts" @click="detectAlts">
        {{ detectingAlts ? '检测中…' : '🔍 检测同伙预约' }}
      </button>
      <span v-if="altDetectSummary" class="filter-count alt-summary">{{ altDetectSummary }}</span>
      <span v-else class="filter-count">共 {{ store.reservationsPagination?.total ?? 0 }} 人</span>
    </div>
    <p class="alt-detect-hint">同伙检测会扫描「等待中」与「已抽中」预约，找出疑似同一人多号（含小号排队 + 大号已抽中）的关联组。</p>

    <!-- 同伙预约筛选（检测后显示） -->
    <div v-if="altGroups.length > 0" class="alt-filter-panel">
      <div class="alt-legend">
        <span class="alt-legend-item alt-legend-item--drawn">已抽中 · 已入驻</span>
        <span class="alt-legend-item alt-legend-item--pending">待抽 · 排队中</span>
      </div>
      <div class="alt-filter-row">
        <label class="alt-filter-label">同伙视图</label>
        <select v-model="viewMode" class="advs1-input alt-view-select">
          <option value="groups">关联组列表（推荐）</option>
          <option value="list">普通预约列表</option>
        </select>
        <template v-if="viewMode === 'groups'">
          <select v-model="altStatusFilter" class="advs1-input alt-view-select">
            <option value="">组内全部状态</option>
            <option value="drawn">组内仅已抽中</option>
            <option value="pending">组内仅待抽</option>
          </select>
          <select v-model="selectedGroupId" class="advs1-input alt-view-select">
            <option value="">全部关联组</option>
            <option v-for="g in altGroups" :key="g.group_id" :value="String(g.group_id)">
              关联组 #{{ g.group_id }} · {{ g.link_summary }}
            </option>
          </select>
        </template>
        <template v-else-if="viewMode === 'list'">
          <select v-model="altListFilter" class="advs1-input alt-view-select">
            <option value="">全部预约</option>
            <option value="alts">仅同伙预约账号</option>
            <option v-for="g in altGroups" :key="g.group_id" :value="String(g.group_id)">
              关联组 #{{ g.group_id }}（{{ g.member_count }} 人）
            </option>
          </select>
        </template>
        <button type="button" class="btn-clear-alt" @click="clearAltDetect">清除标记</button>
      </div>
    </div>

    <!-- 关联组视图 -->
    <div v-if="viewMode === 'groups' && altGroups.length > 0">
      <div v-if="displayGroups.length === 0" class="advs1-state">没有匹配的关联组</div>
      <div v-else class="alt-group-list">
        <section
          v-for="g in displayGroups"
          :key="g.group_id"
          class="alt-group-card"
        >
          <div class="alt-group-head">
            <span class="alt-group-title">关联组 #{{ g.group_id }}</span>
            <span class="alt-group-link">{{ g.link_summary }}</span>
            <span class="alt-group-count">
              <span v-if="(g.pending_count ?? 0) > 0" class="alt-count-pill alt-count-pill--pending">{{ g.pending_count }} 待抽</span>
              <span v-if="(g.drawn_count ?? 0) > 0" class="alt-count-pill alt-count-pill--drawn">{{ g.drawn_count }} 已抽中</span>
              <span class="alt-group-count-total">共 {{ g.member_count }} 账号</span>
            </span>
          </div>
          <div class="alt-group-members">
            <div
              v-for="m in membersForGroup(g)"
              :key="m.user_id"
              class="alt-member-row"
              :class="'alt-member-row--' + m.status"
            >
              <div class="alt-member-status-col">
                <span :class="['alt-draw-status', 'alt-draw-status--' + m.status]">
                  {{ m.status === 'drawn' ? '已抽中' : '待抽' }}
                </span>
                <span class="alt-draw-status-sub">{{ m.status === 'drawn' ? '已入驻' : '排队中' }}</span>
              </div>
              <div class="alt-member-main">
                <span class="alt-member-player">
                  {{ m.nickname }}
                  <span v-if="m.is_frozen" class="alt-frozen-badge">已冻结</span>
                  <span class="alt-member-id">ID {{ m.user_id }} · {{ m.username }}</span>
                </span>
                <span class="alt-member-adv">
                  预约名：<strong>{{ m.adventurer_name }}</strong>
                </span>
                <span v-if="m.status === 'drawn'" class="alt-member-live">
                  <span class="alt-live-label">在册冒险者</span>
                  <template v-if="m.adventurers?.length">
                    <strong v-for="(adv, idx) in m.adventurers" :key="adv.id">
                      {{ idx > 0 ? '、' : '' }}{{ adv.name }}（{{ advStatusLabel(adv.status) }} Lv.{{ adv.level }}）
                    </strong>
                  </template>
                  <span v-else class="alt-live-missing">暂无在册记录（可能已死亡/封存）</span>
                </span>
                <span v-else class="alt-member-queue">
                  尚未抽取 · 当前优先级：<strong>{{ priorityLabel(m.priority) }}</strong>
                </span>
                <span class="alt-member-ip">注册 {{ m.register_ip || '-' }} · 登录 {{ m.last_login_ip || '-' }}</span>
              </div>
              <div v-if="m.status === 'pending'" class="alt-member-actions">
                <label class="action-label">优先级</label>
                <select
                  :value="m.priority"
                  class="advs1-input priority-select"
                  @change="setPriority(m.reservation_id, ($event.target as HTMLSelectElement).value)"
                >
                  <option value="high">高</option>
                  <option value="elevated">临时提升</option>
                  <option value="normal">普通</option>
                  <option value="low">低</option>
                </select>
                <button type="button" class="btn-reject" @click="openReject(m.reservation_id)">拒绝</button>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>

    <!-- 普通列表 -->
    <template v-else>
      <div v-if="store.reservationsLoading" class="advs1-state">加载中…</div>
      <div v-else-if="displayReservations.length === 0" class="advs1-state">
        {{ altListFilter ? '当前筛选下暂无预约' : '暂无预约记录' }}
      </div>
      <div v-else class="res-list">
        <div
          v-for="r in displayReservations"
          :key="r.id"
          class="res-card"
          :class="{
            'alt-flagged': isLinkedUser(r.user_id),
            'res-card--drawn': isLinkedUser(r.user_id) && r.status === 'drawn',
            'res-card--pending': isLinkedUser(r.user_id) && r.status === 'pending',
          }"
        >
          <div class="res-card-head">
            <span class="res-user">
              <span
                v-if="isLinkedUser(r.user_id)"
                :class="['alt-draw-status', 'alt-draw-status--' + r.status, 'alt-draw-status--inline']"
              >
                {{ r.status === 'drawn' ? '已抽中' : r.status === 'pending' ? '待抽' : statusLabel(r.status) }}
              </span>
              {{ r.nickname || r.username }}
              <span v-if="isLinkedUser(r.user_id)" class="alt-badge">
                同伙组 #{{ groupIdForUser(r.user_id) }}
              </span>
            </span>
            <span :class="['res-status', 'st-' + r.status]">{{ statusLabel(r.status) }}</span>
            <span
              v-if="r.status === 'pending' && r.priority_boost_active"
              class="res-boost-badge"
              title="玩家 6 小时内手动临时提升仍有效，可参与抽取"
            >临时提升中</span>
            <span
              v-else-if="r.status === 'pending' && r.draw_eligible === false"
              class="res-inactive-badge"
              title="24 小时内未登录且临时提升已过期，不会被抽取"
            >不可抽取</span>
          </div>
          <div v-if="groupSummaryForUser(r.user_id)" class="alt-reasons">
            <span class="alt-reason-tag">{{ groupSummaryForUser(r.user_id) }}</span>
          </div>
          <div class="res-card-body">
            <span>姓名：<strong>{{ r.name }}</strong></span>
            <span v-if="r.is_unarchive_restore" class="unarchive-tag">解封排队</span>
            <span>性别：{{ r.gender }}</span>
            <span>性格：{{ r.personality }}</span>
            <span v-if="isLinkedUser(r.user_id)">玩家ID：{{ r.user_id }}</span>
          </div>
          <div v-if="r.reject_reason && r.status === 'rejected'" class="res-reject-reason">
            拒绝原因：{{ r.reject_reason }}
          </div>
          <div v-if="r.status === 'pending'" class="res-card-actions">
            <label class="action-label">优先级</label>
            <select
              :value="r.priority"
              class="advs1-input priority-select"
              @change="setPriority(r.id, ($event.target as HTMLSelectElement).value)"
            >
              <option value="high">高</option>
              <option value="elevated">临时提升</option>
              <option value="normal">普通</option>
              <option value="low">低</option>
            </select>
            <button type="button" class="btn-reject" @click="openReject(r.id)">拒绝</button>
          </div>
          <div v-else-if="r.status === 'rejected'" class="res-card-actions">
            <button
              type="button"
              class="advs1-submit btn-sm"
              :disabled="restoringId === r.id"
              @click="restore(r.id)"
            >
              {{ restoringId === r.id ? '恢复中…' : '恢复排队' }}
            </button>
          </div>
        </div>
      </div>

      <Pagination v-if="store.reservationsPagination && !altListFilter" :pagination="store.reservationsPagination" @go="loadData" />
    </template>

    <!-- 拒绝预约弹窗 -->
    <div v-if="rejectTargetId != null" class="modal-mask s1-mobile-surface s1-mobile-overlay" @click.self="rejectTargetId = null">
      <div class="modal-box s1-mobile-dialog">
        <h4 class="modal-title">拒绝预约</h4>
        <textarea v-model="rejectReason" class="advs1-input reject-textarea" rows="3" placeholder="填写拒绝原因（可选）"></textarea>
        <div class="modal-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-cancel" @click="rejectTargetId = null">取消</button>
          <button type="button" class="btn-reject" :disabled="rejecting" @click="confirmReject">
            {{ rejecting ? '提交中…' : '确认拒绝' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 拒绝解封申请弹窗 -->
    <div v-if="appealRejectId != null" class="modal-mask s1-mobile-surface s1-mobile-overlay" @click.self="appealRejectId = null">
      <div class="modal-box s1-mobile-dialog">
        <h4 class="modal-title">拒绝解封申请</h4>
        <textarea v-model="appealRejectReason" class="advs1-input reject-textarea" rows="3" placeholder="填写拒绝原因（可选）"></textarea>
        <div class="modal-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-cancel" @click="appealRejectId = null">取消</button>
          <button type="button" class="btn-reject" :disabled="appealRejecting" @click="confirmAppealReject">
            {{ appealRejecting ? '提交中…' : '确认拒绝' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import type { ReservationAltGroup } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import Pagination from '@/components/Pagination.vue'

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const drawCount = ref(1)
const drawing = ref(false)
const autoDrawEnabled = ref(false)
const savingAutoDraw = ref(false)
const dailyDrawEnabled = ref(false)
const savingDailyDraw = ref(false)
const waitingNpcEnabled = ref(false)
const savingWaitingNpc = ref(false)
const autoAltPriorityEnabled = ref(false)
const savingAutoAltPriority = ref(false)
const statusFilter = ref('')
const priorityFilter = ref('')
const searchQuery = ref('')
const rejectTargetId = ref<number | null>(null)
const rejectReason = ref('')
const rejecting = ref(false)
const restoringId = ref<number | null>(null)
const detectingAlts = ref(false)
const repairingBeds = ref(false)
const altGroups = ref<ReservationAltGroup[]>([])
const userGroupMap = ref<Record<number, number>>({})
const viewMode = ref<'list' | 'groups'>('list')
const altListFilter = ref('')
const selectedGroupId = ref('')
const altStatusFilter = ref('')
const appealStatusFilter = ref('pending')
const appealSearchQuery = ref('')
const appealActionId = ref<number | null>(null)
const appealRejectId = ref<number | null>(null)
const appealRejectReason = ref('')
const appealRejecting = ref(false)
let appealSearchTimer: ReturnType<typeof setTimeout> | null = null

const altDetectSummary = computed(() => {
  if (!altGroups.value.length) return ''
  const memberTotal = altGroups.value.reduce((s, g) => s + g.member_count, 0)
  const pendingTotal = altGroups.value.reduce((s, g) => s + (g.pending_count ?? 0), 0)
  const drawnTotal = altGroups.value.reduce((s, g) => s + (g.drawn_count ?? 0), 0)
  return `${altGroups.value.length} 个关联组 · ${memberTotal} 个同伙账号（${pendingTotal} 待抽 / ${drawnTotal} 已抽中）`
})

const displayGroups = computed(() => {
  let groups = altGroups.value
  if (selectedGroupId.value) {
    const gid = Number(selectedGroupId.value)
    groups = groups.filter((g) => g.group_id === gid)
  }
  if (!altStatusFilter.value) return groups
  return groups
    .map((g) => ({
      ...g,
      members: g.members.filter((m) => m.status === altStatusFilter.value),
    }))
    .filter((g) => g.members.length > 0)
})

function membersForGroup(g: ReservationAltGroup) {
  const statusOrder = { drawn: 0, pending: 1 }
  return [...g.members].sort((a, b) => {
    const sa = statusOrder[a.status as keyof typeof statusOrder] ?? 9
    const sb = statusOrder[b.status as keyof typeof statusOrder] ?? 9
    if (sa !== sb) return sa - sb
    return a.user_id - b.user_id
  })
}

function priorityLabel(priority: string): string {
  const map: Record<string, string> = {
    high: '高',
    elevated: '临时提升',
    normal: '普通',
    low: '低',
  }
  return map[priority] || priority
}

const displayReservations = computed(() => {
  let rows = store.reservations
  if (altListFilter.value === 'alts') {
    rows = rows.filter((r) => isLinkedUser(r.user_id))
  } else if (altListFilter.value) {
    const gid = Number(altListFilter.value)
    const group = altGroups.value.find((g) => g.group_id === gid)
    const uidSet = new Set(group?.members.map((m) => m.user_id) ?? [])
    rows = rows.filter((r) => r.user_id != null && uidSet.has(r.user_id))
  }
  return rows
})

function normalizeUserGroupMap(raw: Record<string, number> | undefined): Record<number, number> {
  const out: Record<number, number> = {}
  if (!raw) return out
  for (const [k, v] of Object.entries(raw)) {
    out[Number(k)] = Number(v)
  }
  return out
}

function groupIdForUser(userId: number | undefined): number | undefined {
  return userId == null ? undefined : userGroupMap.value[userId]
}

function isLinkedUser(userId: number | undefined): boolean {
  return groupIdForUser(userId) != null
}

function groupSummaryForUser(userId: number | undefined): string {
  const gid = groupIdForUser(userId)
  if (!gid) return ''
  return altGroups.value.find((g) => g.group_id === gid)?.link_summary ?? ''
}

function clearAltDetect() {
  altGroups.value = []
  userGroupMap.value = {}
  viewMode.value = 'list'
  altListFilter.value = ''
  selectedGroupId.value = ''
  altStatusFilter.value = ''
}

async function detectAlts() {
  detectingAlts.value = true
  try {
    const res = await store.detectReservationAlts(24)
    if (res.code === 200 && res.data) {
      altGroups.value = res.data.groups ?? []
      userGroupMap.value = normalizeUserGroupMap(res.data.user_group_map)
      viewMode.value = altGroups.value.length > 0 ? 'groups' : 'list'
      altListFilter.value = ''
      selectedGroupId.value = ''
      const totalScanned = res.data.total_scanned ?? res.data.total_pending ?? 0
      const totalPending = res.data.total_pending ?? 0
      const totalDrawn = res.data.total_drawn ?? 0
      if ((res.data.group_count ?? 0) === 0) {
        message.success(`已扫描 ${totalScanned} 个预约（${totalPending} 待抽 / ${totalDrawn} 已抽中），未发现同伙多号`)
      } else {
        message.warning(`发现 ${res.data.group_count} 个关联组，共 ${res.data.flagged_count} 个同伙账号`)
      }
    } else {
      message.error(res.message || '检测失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '检测失败')
  } finally {
    detectingAlts.value = false
  }
}

async function repairDuplicateBeds() {
  const dup = store.reservationsCapacity?.duplicate_beds ?? 0
  const confirmMsg = dup > 0
    ? `检测到 ${dup} 处重复占床。将保留每组最早入驻的冒险者，其余挪到空床。确认修复？`
    : '当前未检测到重复占床，仍要扫描并尝试修复吗？'
  if (!window.confirm(confirmMsg)) return

  repairingBeds.value = true
  try {
    const res = await store.repairDuplicateBeds()
    if (res.code === 200 && res.data) {
      store.reservationsCapacity = res.data.capacity ?? store.reservationsCapacity
      const n = res.data.repaired_count ?? 0
      if (n > 0) {
        const names = (res.data.details ?? [])
          .map((d: { adventurer_name: string }) => d.adventurer_name)
          .join('、')
        message.success(`已修复 ${n} 处重复占床：${names}`)
      } else {
        message.success('未发现重复占床，无需修复')
      }
    } else {
      message.error(res.message || '修复失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '修复失败')
  } finally {
    repairingBeds.value = false
  }
}

const capacityWarnClass = computed(() => {
  const c = store.reservationsCapacity
  if (!c) return ''
  if ((c.duplicate_beds ?? 0) > 0) return 'warn-dup'
  if (c.alive_count >= c.total_beds || c.empty_beds === 0) return 'warn-full'
  if (drawCount.value > c.empty_beds) return 'warn-draw'
  return ''
})

const repairBedsTitle = computed(() => {
  const dup = store.reservationsCapacity?.duplicate_beds ?? 0
  if (dup > 0) {
    return `同一物理床位被 ${dup} 名额外冒险者占用；点击后将保留最早入驻者并挪走其余`
  }
  return '扫描并修复重复占床（同床位多人时保留最早入驻者）'
})

const capacityTitle = computed(() => {
  const c = store.reservationsCapacity
  if (!c) return ''
  if ((c.duplicate_beds ?? 0) > 0) {
    return `存在 ${c.duplicate_beds} 处重复占床，存活/床位/空床数字可能看起来不一致，请先修复`
  }
  if (c.alive_count >= c.total_beds) {
    return '普通编制已达或超过普通床位总数'
  }
  if (drawCount.value > c.empty_beds) {
    return `当前普通空床 ${c.empty_beds}；超过部分通常无法分配`
  }
  return `普通宿舍还可安全抽取约 ${c.empty_beds} 人`
})

let searchTimer: ReturnType<typeof setTimeout> | null = null
function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => loadData(1), 300)
}

function onListFilterChange() {
  altListFilter.value = ''
  loadData(1)
}

function statusLabel(s: string): string {
  const map: Record<string, string> = { pending: '等待中', drawn: '已抽中', rejected: '已拒绝' }
  return map[s] || s
}

function appealStatusLabel(status: string): string {
  const map: Record<string, string> = {
    pending: '待审核',
    approved: '已通过',
    rejected: '已拒绝',
    cancelled: '已取消',
  }
  return map[status] || status
}

async function loadAppeals(page = 1) {
  await store.fetchArchiveAppeals({
    page,
    status: appealStatusFilter.value || undefined,
    search: appealSearchQuery.value.trim() || undefined,
  })
}

function onAppealSearchInput() {
  if (appealSearchTimer) clearTimeout(appealSearchTimer)
  appealSearchTimer = setTimeout(() => loadAppeals(1), 300)
}

async function approveAppeal(appealId: number) {
  if (!window.confirm('确认通过该解封申请？玩家将进入预约抽取队列，抽中后解封原冒险者。')) return
  appealActionId.value = appealId
  try {
    const res = await store.approveArchiveAppeal(appealId)
    if (res.code === 200) {
      message.success('已通过解封申请，玩家已进入抽取队列')
      await loadAppeals(store.archiveAppealsPagination?.current_page ?? 1)
      loadData(store.reservationsPagination?.current_page ?? 1)
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    appealActionId.value = null
  }
}

function openAppealReject(appealId: number) {
  appealRejectId.value = appealId
  appealRejectReason.value = ''
}

async function confirmAppealReject() {
  if (appealRejectId.value == null) return
  appealRejecting.value = true
  try {
    const res = await store.rejectArchiveAppeal(appealRejectId.value, appealRejectReason.value)
    if (res.code === 200) {
      message.success('已拒绝解封申请')
      appealRejectId.value = null
      loadAppeals(store.archiveAppealsPagination?.current_page ?? 1)
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    appealRejecting.value = false
  }
}

function advStatusLabel(status: string): string {
  const map: Record<string, string> = {
    resting: '休息中',
    in_party: '组队中',
    exploring: '探险中',
    skill_school_queue: '等候导师指点',
    building_queue: '等候设施空位',
    building: '建造中',
    dead: '已死亡',
    archived: '已封存',
  }
  return map[status] || status
}

function loadData(page: number = 1) {
  store.fetchReservations({
    page,
    per_page: 20,
    status: statusFilter.value || undefined,
    priority: priorityFilter.value || undefined,
    search: searchQuery.value.trim() || undefined,
  })
}

async function loadAutoDrawSetting() {
  try {
    const res = await store.fetchDormitorySettings('selected')
    if (res.code === 200 && res.data?.settings) {
      const item = res.data.settings.find((s: { key: string }) => s.key === 'auto_draw_on_dormitory_complete')
      autoDrawEnabled.value = !!item?.value
      const dailyItem = res.data.settings.find((s: { key: string }) => s.key === 'daily_reservation_draw_enabled')
      dailyDrawEnabled.value = !!dailyItem?.value
      const waitingNpcItem = res.data.settings.find((s: { key: string }) => s.key === 'waiting_player_npc_enabled')
      waitingNpcEnabled.value = !!waitingNpcItem?.value
      const autoAltItem = res.data.settings.find((s: { key: string }) => s.key === 'reservation_alt_auto_low_priority')
      autoAltPriorityEnabled.value = !!autoAltItem?.value
    }
  } catch {
    // 设置读取失败不阻塞主列表
  }
}

async function onAutoAltPriorityToggle() {
  const next = autoAltPriorityEnabled.value
  savingAutoAltPriority.value = true
  let saved = false
  try {
    const res = await store.updateDormitorySetting('reservation_alt_auto_low_priority', next, 'selected')
    if (res.code !== 200) throw new Error(res.message || '保存设置失败')
    saved = true
    if (next) {
      const check = await store.autoLowerReservationAlts()
      if (check.code !== 200) throw new Error(check.message || '立即检查失败')
      message.success(`已开启自动降优先级，本次降低 ${check.data?.lowered_count ?? 0} 个临时提升预约`)
      clearAltDetect()
      loadData(store.reservationsPagination?.current_page ?? 1)
    } else {
      message.success('已关闭同伙预约自动降优先级')
    }
  } catch (e: any) {
    if (!saved) autoAltPriorityEnabled.value = !next
    const detail = e?.response?.data?.message || e?.message || '操作失败'
    if (saved) message.warning(`设置已保存，立即检查未完成：${detail}；后台将继续检查`)
    else message.error(detail)
  } finally {
    savingAutoAltPriority.value = false
  }
}

async function onWaitingNpcToggle() {
  const next = waitingNpcEnabled.value
  savingWaitingNpc.value = true
  try {
    const res = await store.updateDormitorySetting(
      'waiting_player_npc_enabled',
      next,
      'selected',
    )
    if (res.code === 200) {
      message.success(next ? '已允许待抽玩家成为可选路人 NPC' : '已关闭待抽玩家路人 NPC')
    } else {
      waitingNpcEnabled.value = !next
      message.error(res.message || '保存设置失败')
    }
  } catch (e: any) {
    waitingNpcEnabled.value = !next
    message.error(e?.response?.data?.message || '保存设置失败')
  } finally {
    savingWaitingNpc.value = false
  }
}

async function onDailyDrawToggle() {
  const next = dailyDrawEnabled.value
  savingDailyDraw.value = true
  try {
    const res = await store.updateDormitorySetting(
      'daily_reservation_draw_enabled',
      next,
      'selected',
    )
    if (res.code === 200) {
      message.success(next ? '已开启每天 20:00 自动抽取' : '已关闭每天 20:00 自动抽取')
    } else {
      dailyDrawEnabled.value = !next
      message.error(res.message || '保存设置失败')
    }
  } catch (e: any) {
    dailyDrawEnabled.value = !next
    message.error(e?.response?.data?.message || '保存设置失败')
  } finally {
    savingDailyDraw.value = false
  }
}

async function onAutoDrawToggle() {
  const next = autoDrawEnabled.value
  savingAutoDraw.value = true
  try {
    const res = await store.updateDormitorySetting(
      'auto_draw_on_dormitory_complete',
      next,
      'selected',
    )
    if (res.code === 200) {
      message.success(next ? '已开启宿舍完工自动抽取' : '已关闭宿舍完工自动抽取')
    } else {
      autoDrawEnabled.value = !next
      message.error(res.message || '保存设置失败')
    }
  } catch (e: any) {
    autoDrawEnabled.value = !next
    message.error(e?.response?.data?.message || '保存设置失败')
  } finally {
    savingAutoDraw.value = false
  }
}

async function draw() {
  if (drawCount.value < 1) return
  drawing.value = true
  try {
    const res = await store.drawAdventurers(drawCount.value)
    if (res.code === 200) {
      const count = res.data?.drawn_count ?? 0
      const temporaryNameCount = res.data?.temporary_name_count ?? 0
      const errors: string[] = res.data?.errors ?? []
      if (count > 0) {
        const skipped = res.data?.skipped_inactive ?? 0
        const suffix = skipped > 0 ? `（${skipped} 人因未满足活跃条件已跳过）` : ''
        const renameSuffix = temporaryNameCount > 0
          ? `，其中 ${temporaryNameCount} 人因姓名冲突已用“未命名+编号”入驻，可由玩家命名一次`
          : ''
        message.success(`抽取完成，成功创建 ${count} 名冒险者${renameSuffix}${suffix}`)
      } else {
        const detail = errors.length > 0 ? `：${errors.join('；')}` : '（无 errors 明细）'
        message.error(`抽取 0 个${detail}`)
      }
      clearAltDetect()
      loadData(1)
    } else {
      message.error(res.message || '抽取失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '抽取失败')
  } finally {
    drawing.value = false
  }
}

async function setPriority(resId: number, priority: string) {
  try {
    const res = await store.setReservationPriority(resId, priority)
    if (res.code === 200) {
      message.success('优先级已更新')
      for (const g of altGroups.value) {
        for (const m of g.members) {
          if (m.reservation_id === resId) m.priority = priority
        }
      }
      loadData(store.reservationsPagination?.current_page ?? 1)
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  }
}

function openReject(resId: number) {
  rejectTargetId.value = resId
  rejectReason.value = ''
}

async function confirmReject() {
  if (rejectTargetId.value == null) return
  rejecting.value = true
  try {
    const res = await store.rejectReservation(rejectTargetId.value, rejectReason.value)
    if (res.code === 200) {
      message.success('已拒绝')
      rejectTargetId.value = null
      clearAltDetect()
      loadData(1)
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    rejecting.value = false
  }
}

async function restore(resId: number) {
  if (!window.confirm('确认恢复该预约？将重新放入抽取队列。')) return
  restoringId.value = resId
  try {
    const res = await store.restoreReservation(resId)
    if (res.code === 200) {
      message.success('已恢复排队')
      clearAltDetect()
      loadData(store.reservationsPagination?.current_page ?? 1)
      if (statusFilter.value === 'rejected') {
        statusFilter.value = ''
        loadData(1)
      }
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    restoringId.value = null
  }
}

onMounted(() => {
  loadAutoDrawSetting()
  loadData(1)
  loadAppeals(1)
})
</script>

<style scoped lang="scss">
.advs1-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 20px;
}

.advs1-state {
  padding: 32px 0;
  text-align: center;
  color: var(--color-text-secondary);
}

.advs1-input {
  width: 100%;
  padding: 8px 10px;
  font-size: 14px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  outline: none;
  transition: border-color 0.15s;

  &:focus {
    border-color: var(--color-primary);
  }
}

.advs1-submit {
  padding: 8px 24px;
  font-size: 14px;
  font-weight: 700;
  color: #fff;
  background: var(--color-primary);
  border: none;
  border-radius: 6px;
  cursor: pointer;
  transition: opacity 0.15s;

  &:hover:not(:disabled) {
    opacity: 0.88;
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.draw-section {
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--color-border);
}

.draw-settings-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.draw-setting-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  cursor: pointer;
  white-space: nowrap;

  input {
    width: auto;
    margin: 0;
    cursor: pointer;
  }
}

.draw-setting-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.4;
}

.draw-rule-hint {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.4;
}

.draw-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 8px;
}

.draw-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
}

.draw-input {
  width: 80px;
  flex: none;
}

.draw-capacity {
  font-size: 12px;
  color: var(--color-text-secondary);
  white-space: nowrap;

  &.warn-draw {
    color: var(--color-warning, #d97706);
  }

  &.warn-full {
    color: var(--color-danger);
    font-weight: 600;
  }
}

.draw-capacity-sub {
  opacity: 0.85;
}

.draw-capacity-dup {
  color: var(--color-danger);
  font-weight: 600;
}

.btn-repair-beds {
  padding: 8px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;

  &:hover:not(:disabled) {
    border-color: var(--color-warning, #d97706);
    color: var(--color-warning, #d97706);
  }

  &:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
}

.draw-capacity.warn-dup {
  color: var(--color-danger);
  font-weight: 600;
}

.filter-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;

  select {
    width: auto;
    min-width: 120px;
  }
}

.btn-detect-alt {
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
  transition: border-color 0.15s, opacity 0.15s;

  &:hover:not(:disabled) {
    border-color: var(--color-warning);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.filter-count {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.alt-summary {
  color: var(--color-warning);
  font-weight: 600;
}

.alt-detect-hint {
  margin: -6px 0 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.alt-frozen-badge {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 8px;
  font-size: 10px;
  font-weight: 600;
  color: var(--color-danger);
  border: 1px solid var(--color-danger);
}

.search-input {
  flex: 1;
  min-width: 160px;
  max-width: 280px;
}

.alt-filter-panel {
  margin-bottom: 14px;
  padding: 12px;
  border: 1px solid var(--color-warning);
  border-radius: 8px;
  background: color-mix(in srgb, var(--color-warning) 6%, var(--color-bg));
}

.alt-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px dashed var(--color-border);
}

.alt-legend-item {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
}

.alt-legend-item--drawn {
  color: #0f5132;
  background: color-mix(in srgb, var(--color-success) 18%, var(--color-bg));
  border: 1px solid var(--color-success);
}

.alt-legend-item--pending {
  color: #084298;
  background: color-mix(in srgb, var(--color-primary) 14%, var(--color-bg));
  border: 1px solid var(--color-primary);
}

.alt-count-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
}

.alt-count-pill--drawn {
  color: #0f5132;
  background: color-mix(in srgb, var(--color-success) 18%, transparent);
  border: 1px solid var(--color-success);
}

.alt-count-pill--pending {
  color: #084298;
  background: color-mix(in srgb, var(--color-primary) 14%, transparent);
  border: 1px solid var(--color-primary);
}

.alt-group-count {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.alt-group-count-total {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
}

.alt-draw-status {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 52px;
  padding: 4px 8px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.02em;
  line-height: 1.2;
}

.alt-draw-status--drawn {
  color: #0f5132;
  background: color-mix(in srgb, var(--color-success) 22%, var(--color-bg));
  border: 1px solid var(--color-success);
}

.alt-draw-status--pending {
  color: #084298;
  background: color-mix(in srgb, var(--color-primary) 16%, var(--color-bg));
  border: 1px solid var(--color-primary);
}

.alt-draw-status--inline {
  min-width: auto;
  margin-right: 6px;
  padding: 2px 8px;
  font-size: 11px;
  vertical-align: middle;
}

.alt-draw-status-sub {
  font-size: 10px;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-align: center;
}

.alt-member-status-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
  width: 64px;
  padding-top: 2px;
}

.alt-member-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--color-border);

  &:last-child {
    border-bottom: none;
  }
}

.alt-member-row--drawn {
  background: color-mix(in srgb, var(--color-success) 8%, var(--color-bg));
  border-left: 4px solid var(--color-success);
}

.alt-member-row--pending {
  background: color-mix(in srgb, var(--color-primary) 6%, var(--color-bg));
  border-left: 4px solid var(--color-primary);
}

.alt-member-live {
  font-size: 12px;
  color: var(--color-text);
}

.alt-live-label {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 6px;
  border-radius: 6px;
  font-size: 10px;
  font-weight: 700;
  color: #0f5132;
  background: color-mix(in srgb, var(--color-success) 16%, transparent);
  border: 1px solid var(--color-success);
}

.alt-live-missing {
  color: var(--color-text-secondary);
  font-style: italic;
}

.alt-member-queue {
  font-size: 12px;
  color: var(--color-primary);
}

.res-card--drawn {
  border-left: 4px solid var(--color-success);
  background: color-mix(in srgb, var(--color-success) 6%, var(--color-bg));
}

.res-card--pending {
  border-left: 4px solid var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 5%, var(--color-bg));
}

.alt-filter-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.alt-filter-label {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
  white-space: nowrap;
}

.alt-view-select {
  width: auto;
  min-width: 180px;
  flex: none;
}

.btn-clear-alt {
  padding: 6px 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;

  &:hover {
    border-color: var(--color-text-secondary);
  }
}

.alt-group-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.alt-group-card {
  border: 1px solid var(--color-warning);
  border-radius: 10px;
  overflow: hidden;
  background: var(--color-bg);
}

.alt-group-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: color-mix(in srgb, var(--color-warning) 10%, var(--color-surface));
  border-bottom: 1px dashed var(--color-warning);
}

.alt-group-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
}

.alt-group-link {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.alt-group-members {
  display: flex;
  flex-direction: column;
}

.alt-member-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  margin-left: auto;
}

.alt-member-main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}

.alt-member-player {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
}

.alt-member-id {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 400;
  color: var(--color-text-secondary);
}

.alt-member-adv {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.alt-member-ip {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-family: ui-monospace, monospace;
}

.res-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.res-card {
  padding: 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;

  &.alt-flagged {
    border-color: var(--color-warning);
    background: color-mix(in srgb, var(--color-warning) 6%, var(--color-bg));
  }
}

.res-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.res-user {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.alt-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 8px;
  color: var(--color-warning);
  border: 1px solid var(--color-warning);
  background: color-mix(in srgb, var(--color-warning) 12%, transparent);
}

.alt-reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.alt-reason-tag {
  font-size: 11px;
  color: var(--color-text-secondary);
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--color-surface);
  border: 1px dashed var(--color-warning);
}

.res-status {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);

  &.st-pending {
    color: var(--color-primary);
    border-color: var(--color-primary);
  }

  &.st-drawn {
    color: var(--color-success);
    border-color: var(--color-success);
  }

  &.st-rejected {
    color: var(--color-danger);
    border-color: var(--color-danger);
  }
}

.res-inactive-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--color-warning);
  color: var(--color-warning);
}

.res-boost-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--color-success);
  color: var(--color-success);
  font-weight: 600;
}

.res-card-body {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 8px;
}

.res-reject-reason {
  font-size: 12px;
  color: var(--color-danger);
  margin-bottom: 8px;
}

.res-card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.action-label {
  font-size: 12px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.priority-select {
  width: 90px;
  flex: none;
  padding: 4px 8px;
  font-size: 12px;
}

.btn-reject {
  padding: 4px 14px;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  background: var(--color-danger);
  border: none;
  border-radius: 6px;
  cursor: pointer;
  transition: opacity 0.15s;

  &:hover:not(:disabled) {
    opacity: 0.85;
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-box {
  width: 360px;
  max-width: 90vw;
  padding: 20px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
}

.modal-title {
  margin: 0 0 12px;
  font-size: 15px;
  color: var(--color-text);
}

.reject-textarea {
  resize: vertical;
  min-height: 60px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}

.btn-cancel {
  padding: 6px 16px;
  font-size: 13px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
}

.appeal-section {
  margin: 20px 0;
  padding-top: 16px;
  border-top: 1px solid var(--color-border);
}

.appeal-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.appeal-section-title {
  margin: 0;
  font-size: 15px;
}

.appeal-filter-row {
  margin-bottom: 12px;
}

.appeal-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.appeal-card {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 12px;
  background: var(--color-bg);
}

.appeal-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.appeal-user {
  font-weight: 600;
}

.appeal-status {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
}

.appeal-status.st-pending { background: #fff3cd; color: #664d03; }
.appeal-status.st-approved { background: #cfe2ff; color: #084298; }
.appeal-status.st-rejected { background: #f8d7da; color: #842029; }

.appeal-reason {
  margin: 0 0 6px;
  white-space: pre-wrap;
  font-size: 13px;
}

.appeal-meta {
  margin: 0 0 4px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.appeal-reject {
  margin: 0;
  font-size: 12px;
  color: #842029;
}

.appeal-card-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.btn-sm {
  padding: 4px 14px;
  font-size: 12px;
}

.unarchive-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #e7f1ff;
  color: #084298;
}
</style>
