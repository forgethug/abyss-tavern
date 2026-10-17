<template>
  <div class="admin-party-match-panel">
    <div class="filter-bar">
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="fetchOverview">
        {{ loading ? '刷新中…' : '🔄 刷新' }}
      </button>
      <button type="button" class="btn-secondary btn-sm" :disabled="ghostPreviewing" @click="doGhostPreview">
        {{ ghostPreviewing ? '查询中…' : '🔍 预览幽灵组队' }}
      </button>
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="ghostRepairing || !ghostInfo || ghostInfo.count === 0"
        @click="doGhostRepair"
      >
        {{ ghostRepairing ? '修复中…' : '🔧 一键修复幽灵组队' }}
      </button>
      <label class="repair-days-label">
        单排漏券回看
        <input v-model.number="dailySoloRepairDays" type="number" min="1" :max="dailySoloMaxDays">
        天
      </label>
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="dailySoloPreviewing || dailySoloRepairing"
        @click="doDailySoloPreview"
      >
        {{ dailySoloPreviewing ? '查询中…' : '🎟️ 预览漏券' }}
      </button>
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="dailySoloRepairing || !dailySoloInfo || dailySoloInfo.candidate_count === 0"
        @click="doDailySoloRepair"
      >
        {{ dailySoloRepairing ? '补发中…' : '🎁 一键补发' }}
      </button>
      <span class="scope-hint">仅显示匹配中 / 集结中（未进本）</span>
    </div>

    <div v-if="ghostInfo" class="ghost-info">
      <div class="ghost-line">
        幽灵组队（显示组队中但不在任何活跃小队/匹配队列）：
        <b>{{ ghostInfo.count }}</b> 人
      </div>
      <div v-if="ghostInfo.count > 0" class="ghost-list">
        <div v-for="g in ghostInfo.ghosts" :key="g.adventurer_id" class="ghost-line">
          #{{ g.adventurer_id }} {{ g.name }}
          <small v-if="g.stale_member_rows"> · 历史成员行 {{ g.stale_member_rows }}</small>
        </div>
      </div>
    </div>

    <div v-if="dailySoloInfo" class="ghost-info">
      <div class="ghost-line">
        最近 {{ dailySoloInfo.days }} 天每日随机单排漏券候选：
        <b>{{ dailySoloInfo.candidate_count }}</b> 人
        <small>
          （需补发 {{ dailySoloInfo.grant_count }}，仅补任务状态 {{ dailySoloInfo.mark_completed_count }}）
        </small>
      </div>
      <div v-if="dailySoloInfo.truncated" class="error-text">
        候选超过单次安全上限，仅展示并处理当前批次；完成后请再次预览。
      </div>
      <div v-if="dailySoloInfo.items?.length" class="ghost-list">
        <div
          v-for="item in dailySoloInfo.items"
          :key="item.task_instance_id"
          class="ghost-line"
        >
          日志 #{{ item.expedition_log_id }} · {{ item.owner_nickname }} / #{{ item.adventurer_id }}
          {{ item.adventurer_name }} · {{ item.target_dungeon }}
          <small>
            · 当前 {{ item.current_ticket_quantity }} 张
            · {{ item.action === 'grant_reward' ? `补 ${item.reward_quantity} 张` : '券已入账，仅补完成状态' }}
          </small>
        </div>
      </div>
    </div>

    <div v-if="summary" class="summary-row">
      <span>集结中小队 {{ summary.party_count }}</span>
      <span>匹配单 {{ summary.match_ticket_count }}（排队 {{ summary.queued_ticket_count }} / 组建 {{ summary.forming_ticket_count }} / 待发车 {{ summary.matched_ticket_count ?? 0 }}）</span>
    </div>

    <div v-if="loading && !overview" class="empty">加载中…</div>
    <template v-else-if="overview">
      <section class="section-block">
        <h6 class="block-title">⚔️ 集结中（组队中，未进本）</h6>
        <p class="hint-text">已成团、尚未出发的小队；可在此强制出发 / 解散 / 移除成员。</p>
        <p v-if="partyTotalHint" class="hint-text">{{ partyTotalHint }}</p>
        <p v-if="overview.parties.length === 0" class="empty hint">当前没有集结中的小队</p>
        <div v-else class="card-list">
          <div v-for="party in overview.parties" :key="'p-' + party.id" class="item-card">
            <div class="item-head">
              <div class="item-head-left">
                <span class="item-title">{{ party.name }} <small>#{{ party.id }}</small></span>
                <span class="status-tag" :class="'st-' + party.status">{{ partyStatusLabel(party.status) }}</span>
              </div>
              <div v-if="isPartyGmEligible(party)" class="gm-actions">
                <button
                  v-if="party.status === 'forming'"
                  type="button"
                  class="btn-gm btn-gm-warning"
                  :disabled="actionLoading"
                  @click="doForceDispatch(party)"
                >⚡ 强制出发</button>
                <button
                  type="button"
                  class="btn-gm btn-gm-danger"
                  :disabled="actionLoading"
                  @click="doForceDisband(party)"
                >💥 强制解散</button>
              </div>
            </div>
            <div class="item-meta">
              <span>{{ party.kind === 'special' ? '✨ 特殊机缘' : '🎯 普通匹配' }}</span>
              <span>👥 {{ party.member_count }}/{{ party.target_size }}</span>
              <span v-if="party.target_dungeon">
                🗺️ {{ party.target_dungeon }}
                <small v-if="party.target_dungeon_level_desc">({{ party.target_dungeon_level_desc }})</small>
              </span>
              <span v-if="party.departure_time">⏰ {{ formatDateTime(party.departure_time) }} 出发</span>
              <span v-if="party.departure_vote_count != null">🗳️ 投票 {{ party.departure_vote_count }}/{{ party.member_count }}</span>
            </div>
            <div v-if="party.members?.length" class="member-list">
              <span
                v-for="m in party.members"
                :key="party.id + '-' + m.id"
                class="member-chip member-chip-action"
              >
                {{ m.name }}<small> HP {{ m.hp }}/{{ m.max_hp }}</small>
                <small v-if="m.owner_nickname"> · {{ m.owner_nickname }}</small>
                <button
                  v-if="isPartyGmEligible(party)"
                  type="button"
                  class="btn-remove"
                  :disabled="removingAdvId === m.id"
                  title="移除成员"
                  @click.stop="doRemoveMember(party, m)"
                >⊖</button>
              </span>
            </div>
          </div>
        </div>
      </section>

      <section class="section-block">
        <h6 class="block-title">🎯 匹配中</h6>
        <p v-if="overview.match_tickets.length === 0" class="empty hint">当前没有进行中的匹配</p>
        <div v-else class="card-list">
          <div v-for="ticket in overview.match_tickets" :key="'t-' + ticket.id" class="item-card">
            <div class="item-head">
              <div class="item-head-left">
                <span class="item-title">
                  #{{ ticket.id }}
                  <small v-if="ticket.invite_code">· 邀请码 {{ ticket.invite_code }}</small>
                </span>
                <span class="status-tag" :class="'st-' + ticket.status">{{ ticketStatusLabel(ticket.status) }}</span>
              </div>
              <div class="gm-actions">
                <template v-if="ticketMatchedPartyGmTarget(ticket)">
                  <button
                    v-if="ticketMatchedPartyGmTarget(ticket)?.status === 'forming'"
                    type="button"
                    class="btn-gm btn-gm-warning"
                    :disabled="actionLoading"
                    @click="doForceDispatch(ticketMatchedPartyGmTarget(ticket)!)"
                  >⚡ 强制出发</button>
                  <button
                    type="button"
                    class="btn-gm btn-gm-danger"
                    :disabled="actionLoading"
                    @click="doForceDisband(ticketMatchedPartyGmTarget(ticket)!)"
                  >💥 强制解散</button>
                </template>
                <button
                  v-else-if="ticket.status === 'forming' || ticket.status === 'queued'"
                  type="button"
                  class="btn-gm btn-gm-secondary"
                  :disabled="actionLoading"
                  @click="doCancelTicket(ticket)"
                >取消匹配</button>
              </div>
            </div>
            <div class="item-meta">
              <span>{{ ticket.queue_type === 'solo' ? '单人' : '小队' }}</span>
              <span>{{ ticket.access_mode === 'invite' ? '邀请制' : '公开' }}</span>
              <span>👥 {{ ticket.member_count }}</span>
              <span v-if="ticket.status === 'queued'">前面还有 {{ ticket.queue_parties_ahead ?? 0 }} 个排队</span>
              <span v-if="ticket.preferred_dungeons?.length">🗺️ {{ ticket.preferred_dungeons.join(' / ') }}</span>
              <span v-else-if="ticket.preferred_dungeon">🗺️ {{ ticket.preferred_dungeon }}</span>
              <span v-else>🗺️ 随机地图</span>
              <span v-if="ticket.owner_nickname">👤 队长 {{ ticket.owner_nickname }}</span>
              <span v-if="ticket.matched_party_id">🔗 已关联小队 #{{ ticket.matched_party_id }} {{ ticket.matched_party_name || '' }}</span>
              <span v-if="ticket.queued_at">⏳ 入队 {{ formatDateTime(ticket.queued_at) }}</span>
              <span v-else-if="ticket.created_at">⏳ 创建 {{ formatDateTime(ticket.created_at) }}</span>
            </div>
            <p v-if="ticket.match_error" class="error-text">{{ ticket.match_error }}</p>
            <div v-if="ticket.members?.length" class="member-list">
              <span v-for="m in ticket.members" :key="ticket.id + '-' + m.adventurer_id" class="member-chip">
                {{ m.name }}<small> Lv.{{ m.level }}</small>
                <small v-if="m.owner_nickname"> · {{ m.owner_nickname }}</small>
              </span>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { formatDateTime } from '@/utils/time'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { partyStatusLabel } from '../utils'
import { useAdventurerS1Store } from '../stores/adventurerS1'

type GmParty = { id: number; name: string; status?: string }

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const loading = ref(false)
const actionLoading = ref(false)
const removingAdvId = ref<number | null>(null)
const overview = ref<any>(null)
const ghostInfo = ref<any>(null)
const ghostPreviewing = ref(false)
const ghostRepairing = ref(false)
const dailySoloInfo = ref<any>(null)
const dailySoloRepairDays = ref(7)
const dailySoloMaxDays = ref(30)
const dailySoloPreviewing = ref(false)
const dailySoloRepairing = ref(false)

const summary = computed(() => overview.value?.summary ?? null)
const partyTotalHint = computed(() => {
  if (!overview.value) return ''
  const total = overview.value.party_total ?? overview.value.parties?.length ?? 0
  const shown = overview.value.parties?.length ?? 0
  const limit = overview.value.party_limit ?? shown
  if (total > shown) return `共 ${total} 支，当前展示前 ${shown} 支（上限 ${limit}）`
  return ''
})

const partyById = computed(() => {
  const map = new Map<number, GmParty>()
  for (const p of overview.value?.parties ?? []) {
    map.set(p.id, p)
  }
  return map
})

const MATCH_TICKET_STATUS_LABELS: Record<string, string> = {
  forming: '组建中',
  queued: '排队中',
  matched: '已匹配',
  cancelled: '已取消',
}

function ticketStatusLabel(status?: string): string {
  return MATCH_TICKET_STATUS_LABELS[status ?? ''] ?? (status ?? '')
}

function isPartyGmEligible(party: { status?: string }): boolean {
  return party.status === 'forming'
}

function ticketMatchedPartyGmTarget(ticket: {
  matched_party_id?: number
  matched_party_name?: string
  matched_party_status?: string
}): GmParty | null {
  if (!ticket.matched_party_id) return null
  const linked = partyById.value.get(ticket.matched_party_id)
  if (linked && isPartyGmEligible(linked)) return linked
  const status = ticket.matched_party_status
  if (status === 'forming') {
    return {
      id: ticket.matched_party_id,
      name: ticket.matched_party_name || `小队 #${ticket.matched_party_id}`,
      status,
    }
  }
  return null
}

async function fetchOverview() {
  loading.value = true
  try {
    const res = await store.adminFetchPartyMatchOverview({
      party_status: 'forming',
      ticket_status: 'active',
    })
    if (res.code === 200 && res.data) {
      overview.value = res.data
    } else {
      message.error(res.message || '加载失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function doGhostPreview() {
  if (ghostPreviewing.value) return
  ghostPreviewing.value = true
  try {
    const res = await store.adminPreviewGhostInParty()
    if (res.code === 200) {
      ghostInfo.value = res.data
    } else {
      message.error(res.message || '预览失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    ghostPreviewing.value = false
  }
}

async function doGhostRepair() {
  if (ghostRepairing.value || !ghostInfo.value || ghostInfo.value.count === 0) return
  if (!window.confirm(`将修复 ${ghostInfo.value.count} 名幽灵组队冒险者（改回休息中并清理历史成员行）。确定吗？`)) return
  ghostRepairing.value = true
  try {
    const res = await store.adminRepairGhostInParty()
    if (res.code === 200) {
      message.success(`已修复 ${res.data?.count ?? 0} 人，清理历史成员行 ${res.data?.stale_members_deleted ?? 0} 条`)
      await doGhostPreview()
      await fetchOverview()
    } else {
      message.error(res.message || '修复失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '修复失败')
  } finally {
    ghostRepairing.value = false
  }
}

async function doDailySoloPreview() {
  if (dailySoloPreviewing.value || dailySoloRepairing.value) return
  dailySoloPreviewing.value = true
  try {
    const res = await store.adminPreviewDailySoloTicketRepair(dailySoloRepairDays.value)
    if (res.code === 200) {
      dailySoloInfo.value = res.data
      dailySoloMaxDays.value = res.data?.max_days ?? 30
    } else {
      message.error(res.message || '漏券预览失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '漏券预览失败')
  } finally {
    dailySoloPreviewing.value = false
  }
}

async function doDailySoloRepair() {
  if (dailySoloRepairing.value || !dailySoloInfo.value?.candidate_count) return
  const count = dailySoloInfo.value.candidate_count
  if (!window.confirm(`将重新校验并处理 ${count} 条每日随机单排漏券记录。确定继续吗？`)) return
  dailySoloRepairing.value = true
  try {
    const res = await store.adminRepairDailySoloTickets(dailySoloRepairDays.value)
    if (res.code === 200) {
      message.success(
        `已处理 ${res.data?.repaired_count ?? 0} 条，跳过 ${res.data?.skipped_count ?? 0} 条，失败 ${res.data?.error_count ?? 0} 条`,
      )
      dailySoloRepairing.value = false
      await doDailySoloPreview()
    } else {
      message.error(res.message || '漏券补发失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '漏券补发失败')
  } finally {
    dailySoloRepairing.value = false
  }
}

async function doForceDispatch(party: GmParty) {
  if (actionLoading.value) return
  if (!window.confirm(`确认强制出发「${party.name}」？将跳过等待时间和满员要求。`)) return
  actionLoading.value = true
  try {
    const res = await store.forceDispatchParty(party.id, 'selected')
    if (res.code === 200) {
      message.success(`「${party.name}」已强制出发`)
      await fetchOverview()
    } else {
      message.error(res.message || '强制出发失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '强制出发失败')
  } finally {
    actionLoading.value = false
  }
}

async function doForceDisband(party: GmParty) {
  if (actionLoading.value) return
  if (!window.confirm(`确认强制解散「${party.name}」？`)) return
  actionLoading.value = true
  try {
    const res = await store.forceDisbandParty(party.id, 'selected')
    if (res.code === 200) {
      message.success(`「${party.name}」已强制解散`)
      await fetchOverview()
    } else {
      message.error(res.message || '强制解散失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '强制解散失败')
  } finally {
    actionLoading.value = false
  }
}

async function doRemoveMember(party: { id: number; name: string }, member: { id: number; name: string }) {
  if (removingAdvId.value) return
  if (!window.confirm(`确认将「${member.name}」从「${party.name}」中移除？`)) return
  removingAdvId.value = member.id
  try {
    const res = await store.adminRemoveMember(party.id, member.id, 'selected')
    if (res.code === 200) {
      message.success(`已将「${member.name}」从「${party.name}」中移除`)
      await fetchOverview()
    } else {
      message.error(res.message || '移除失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '移除失败')
  } finally {
    removingAdvId.value = null
  }
}

async function doCancelTicket(ticket: { id: number }) {
  if (actionLoading.value) return
  if (!window.confirm(`确认取消匹配单 #${ticket.id}？`)) return
  actionLoading.value = true
  try {
    const res = await store.adminCancelMatchmaking(ticket.id)
    if (res.code === 200) {
      message.success('已取消匹配')
      await fetchOverview()
    } else {
      message.error(res.message || '取消失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消失败')
  } finally {
    actionLoading.value = false
  }
}

onMounted(() => {
  void fetchOverview()
})
</script>

<style scoped lang="scss">
.admin-party-match-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.filter-bar select {
  padding: 6px 10px;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.scope-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.summary-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.section-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.block-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.hint-text,
.empty {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.empty.hint {
  margin: 0;
}

.card-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ghost-info {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface, var(--color-bg));
  font-size: 13px;
}

.ghost-line {
  line-height: 1.5;
  color: var(--color-text);

  small {
    color: var(--color-text-secondary);
  }
}

.ghost-list {
  max-height: 160px;
  overflow-y: auto;
  margin-top: 6px;
}

.repair-days-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--color-text-secondary);

  input {
    width: 58px;
    padding: 4px 6px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    color: var(--color-text);
    background: var(--color-bg);
  }
}

.item-card {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 12px;
  background: var(--color-bg);
}

.item-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.item-head-left {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.item-title {
  font-weight: 600;
  font-size: 14px;

  small {
    font-weight: 400;
    color: var(--color-text-secondary);
  }
}

.gm-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  flex-shrink: 0;
}

.btn-gm {
  padding: 5px 12px;
  font-size: 12px;
  font-weight: 700;
  border-radius: 6px;
  cursor: pointer;
  border: 2px solid transparent;
  white-space: nowrap;
}

.btn-gm:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.btn-gm-warning {
  color: #664d03;
  background: #ffc107;
  border-color: #e0a800;
}

.btn-gm-danger {
  color: #fff;
  background: #dc3545;
  border-color: #b02a37;
}

.btn-gm-secondary {
  color: var(--color-text);
  background: var(--color-surface, var(--color-bg));
  border-color: var(--color-border);
}

.btn-secondary {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  border-radius: 6px;
  cursor: pointer;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.status-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}

.st-forming { background: #cce5ff; color: #004085; }
.st-queued { background: #fff3cd; color: #856404; }
.st-exploring { background: #fff3cd; color: #856404; }
.st-matched { background: #d4edda; color: #155724; }
.st-cancelled { background: #e9ecef; color: #495057; }

.item-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.error-text {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--color-danger, #dc3545);
}

.member-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.member-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 4px 8px;
  border-radius: 999px;
  background: var(--color-surface, var(--color-bg));
  border: 1px solid var(--color-border);
  font-size: 12px;

  small {
    color: var(--color-text-secondary);
  }
}

.member-chip-action {
  padding-right: 4px;
}

.btn-remove {
  margin-left: 4px;
  padding: 0 5px;
  border: none;
  border-radius: 999px;
  background: rgba(220, 53, 69, 0.12);
  color: var(--color-danger, #dc3545);
  cursor: pointer;
  font-size: 12px;
  line-height: 1.4;

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}
</style>
