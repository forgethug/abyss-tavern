<template>
  <div class="commission-panel">
    <div class="commission-toolbar">
      <span class="contribution-badge">🏅 当前贡献：<b>{{ contribution }}</b> <span class="contribution-total">· 累计 {{ contributionTotal }}</span></span>
      <div v-if="isAdmin" class="admin-btns">
        <button type="button" class="btn-task-admin" :disabled="refilling" @click="adminRefill">
          {{ refilling ? '生成中…' : '🔄 补满委托' }}
        </button>
        <button type="button" class="btn-task-admin" :disabled="refreshing" @click="adminRefresh">
          {{ refreshing ? '刷新中…' : '🗑️ 刷新任务' }}
        </button>
      </div>
    </div>

    <div class="commission-tips">
      <div class="tip-item">💡 悬赏任务会定期补充，冒险者们可以挑选心仪的任务，不必着急。</div>
      <div class="tip-item">💡 贡献值来源于对酒馆的建设——例如向建筑物捐赠材料、参与建造等。</div>
    </div>

    <!-- 进行中 / 待领取的委托 -->
    <div v-if="mine" class="active-task-section">
      <h5 class="section-title">我的委托</h5>
      <div class="active-task-card" :class="`tier-${mine.tier}`">
        <div class="task-header">
          <span class="task-icon">{{ tierIcon(mine.tier) }}</span>
          <div>
            <div class="task-name">{{ mine.title }}</div>
            <div class="task-adv-name">{{ tierLabel(mine.tier) }}委托 · {{ taskTypeLabel(mine.task_type) }}</div>
          </div>
        </div>
        <div v-if="mine.background" class="task-active-desc">{{ mine.background }}</div>
        <div class="task-req-line">📌 委托需求：{{ mine.requirement_text }}</div>
        <div class="task-req-line progress-line">进度：{{ progressText(mine) }}</div>
        <div v-if="mine.boss_name" class="task-req-line">👑 讨伐目标：{{ mine.boss_name }}</div>

        <div v-if="mine.status === 'fulfilled'" class="task-expedition-hint">
          <div class="remaining-text">✅ 委托已完成，可领取奖励</div>
          <div class="task-rewards center">
            <span v-if="mine.reward_gold">💰{{ mine.reward_gold }}</span>
            <span v-for="(it, idx) in mine.reward_items" :key="idx" class="task-reward-item">
              🎁<span
                class="task-reward-item-name task-reward-preview-trigger"
                :style="it.rarity ? { color: rarityColor(it.rarity) } : undefined"
                role="button"
                tabindex="0"
                @click.stop="openRewardPreview(it)"
                @keydown.enter.stop="openRewardPreview(it)"
                @keydown.space.prevent.stop="openRewardPreview(it)"
              >{{ rewardItemLabel(it) }}</span><span
                v-if="it.rarity"
                class="rarity-pill"
                :style="{ color: rarityColor(it.rarity), borderColor: rarityColor(it.rarity) }"
              >{{ rarityLabel(it.rarity) }}</span>
            </span>
          </div>
          <button type="button" class="advs1-submit task-complete-btn" :disabled="claiming" @click="claim">
            {{ claiming ? '领取中…' : '🎁 领取奖励' }}
          </button>
        </div>
        <div v-else class="task-expedition-hint">
          <div class="remaining-text">
            🗺️ 前往【{{ (mine.target_dungeons || []).join(' / ') }}】触发委托事件
          </div>
          <p class="expedition-note">
            派遣绑定冒险者前往上述副本探险，有概率触发委托事件并由探险剧情判定完成；完成后回此处领奖。
          </p>
          <button type="button" class="btn-cancel-commission" :disabled="cancelling" @click="cancel">
            {{ cancelling ? '取消中…' : '❌ 取消委托' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 可接取委托：三档 -->
    <div v-else class="task-list-section">
      <div v-if="loading" class="empty-helpers">加载中…</div>
      <template v-else>
        <div v-for="tier in TIER_ORDER" :key="tier" class="tier-block">
          <h5 class="section-title">
            {{ tierIcon(tier) }} {{ tierLabel(tier) }}委托
            <span class="tier-sub">（接取需 {{ acceptCostOf(tier) }} 贡献）</span>
          </h5>
          <div v-if="(openByTier[tier] || []).length === 0" class="empty-helpers">暂无{{ tierLabel(tier) }}委托</div>
          <div v-else class="task-list">
            <div
              v-for="c in openByTier[tier]"
              :key="c.id"
              class="task-card"
              :class="[`tier-${c.tier}`, { disabled: !c.can_accept }]"
              @click="accept(c)"
            >
              <div class="task-card-header">
                <span class="task-icon">{{ tierIcon(c.tier) }}</span>
                <div class="task-card-info">
                  <div class="task-name">{{ c.title }}</div>
                  <div class="task-category">{{ taskTypeLabel(c.task_type) }} · 🗺️ {{ (c.target_dungeons || []).join(' / ') }}</div>
                </div>
                <div class="task-duration">接取消耗 {{ c.accept_cost }} 贡献</div>
                <button
                  v-if="isAdmin"
                  type="button"
                  class="btn-card-refresh"
                  :disabled="refreshingId === c.id"
                  title="精准刷新此委托"
                  @click.stop="adminRefreshOne(c)"
                >
                  {{ refreshingId === c.id ? '…' : '🔄' }}
                </button>
              </div>
              <div class="task-desc">{{ c.background }}</div>
              <div class="task-req-line">📌 {{ c.requirement_text }}</div>
              <div class="task-req-line progress-line">目标数量：{{ progressText(c) }}</div>
              <div v-if="c.boss_name" class="task-req-line">👑 讨伐：{{ c.boss_name }}</div>
              <div class="task-meta">
                <div class="task-rewards">
                  <span>完成奖励：</span>
                  <span v-if="c.reward_gold">💰{{ c.reward_gold }}</span>
                  <span v-for="(it, idx) in c.reward_items" :key="idx" class="task-reward-item">
                    🎁<span
                      class="task-reward-item-name task-reward-preview-trigger"
                      :style="it.rarity ? { color: rarityColor(it.rarity) } : undefined"
                      role="button"
                      tabindex="0"
                      @click.stop="openRewardPreview(it)"
                      @keydown.enter.stop="openRewardPreview(it)"
                      @keydown.space.prevent.stop="openRewardPreview(it)"
                    >{{ rewardItemLabel(it) }}</span><span
                      v-if="it.rarity"
                      class="rarity-pill"
                      :style="{ color: rarityColor(it.rarity), borderColor: rarityColor(it.rarity) }"
                    >{{ rarityLabel(it.rarity) }}</span>
                  </span>
                </div>
              </div>
              <div v-if="!c.can_accept && c.reject_reason" class="task-reject-reason">{{ c.reject_reason }}</div>
            </div>
          </div>
        </div>
      </template>
    </div>
    <RewardItemPreviewModal
      :item="rewardPreview"
      kicker="悬赏奖励预览"
      @close="closeRewardPreview"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useAdventurerS1Store, isPlayableAdventurer } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useSocket } from '@/composables/useSocket'
import { useAdminSecondaryAccess } from '@/composables/useAdminSecondaryAccess'
import { rarityColor, rarityLabel } from '../utils'
import { type RewardPreviewItem } from '../itemTypes'
import RewardItemPreviewModal from './RewardItemPreviewModal.vue'

type Tier = 'junior' | 'intermediate' | 'advanced'

interface RewardItem extends RewardPreviewItem { name: string; quantity: number }
interface CommissionItem {
  id: number
  tier: Tier
  task_type: string
  title: string
  background: string
  requirement_text: string
  target_dungeons: string[]
  accept_cost: number
  reward_gold: number
  reward_items: RewardItem[]
  status: string
  required_count?: number
  progress_count?: number
  boss_name?: string
  boss_background?: string
  can_accept?: boolean
  reject_reason?: string | null
}
interface CommissionBoard {
  open: Record<Tier, CommissionItem[]>
  mine: CommissionItem | null
  contribution: number
  contribution_total: number
  slots: Record<string, number>
}

const props = defineProps<{ adventurerId?: number | null }>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const socket = useSocket()

const TIER_ORDER: Tier[] = ['junior', 'intermediate', 'advanced']
const { canUseFullAdminUi: isAdmin } = useAdminSecondaryAccess()

const loading = ref(false)
const accepting = ref(false)
const claiming = ref(false)
const cancelling = ref(false)
const refilling = ref(false)
const refreshing = ref(false)
const refreshingId = ref<number | null>(null)
const board = ref<CommissionBoard | null>(null)
const rewardPreview = ref<RewardItem | null>(null)

const contribution = computed(() => board.value?.contribution ?? 0)
const contributionTotal = computed(() => board.value?.contribution_total ?? 0)
const mine = computed(() => board.value?.mine ?? null)
const openByTier = computed<Record<Tier, CommissionItem[]>>(() =>
  board.value?.open ?? { junior: [], intermediate: [], advanced: [] },
)

const myAdventurer = computed(() => store.adventurers.find(isPlayableAdventurer) ?? null)
const resolvedAdvId = computed(() => props.adventurerId ?? myAdventurer.value?.id ?? undefined)

function tierLabel(t: Tier): string {
  return ({ junior: '初级', intermediate: '中级', advanced: '高级' } as Record<Tier, string>)[t] ?? t
}
function tierIcon(t: Tier): string {
  return ({ junior: '🟢', intermediate: '🔵', advanced: '🟣' } as Record<Tier, string>)[t] ?? '📋'
}
function taskTypeLabel(t: string): string {
  const map: Record<string, string> = {
    gather: '采集', rescue: '救援', escort: '护送', combat: '讨伐', subjugation: '讨伐Boss',
  }
  return map[t] ?? t
}
function acceptCostOf(t: Tier): number {
  const first = (openByTier.value[t] || [])[0]
  if (first) return first.accept_cost
  return ({ junior: 100, intermediate: 500, advanced: 1000 } as Record<Tier, number>)[t]
}
function rewardItemLabel(it: RewardItem): string {
  return (it.quantity ?? 1) > 1 ? `${it.name}×${it.quantity}` : it.name
}
function openRewardPreview(item: RewardItem) {
  rewardPreview.value = item
}
function closeRewardPreview() {
  rewardPreview.value = null
}
function progressText(c: CommissionItem): string {
  const required = Math.max(1, c.required_count ?? 1)
  const progress = Math.max(0, Math.min(required, c.progress_count ?? 0))
  return `${progress.toLocaleString()} / ${required.toLocaleString()}`
}

async function loadData() {
  loading.value = true
  try {
    await store.fetchAdventurers()
    const res = await store.fetchCommissionBoard(resolvedAdvId.value)
    board.value = res.code === 200 ? (res.data as CommissionBoard) : null
  } finally {
    loading.value = false
  }
}

async function accept(c: CommissionItem) {
  if (!c.can_accept) {
    message.error(c.reject_reason ?? '无法接取')
    return
  }
  if (accepting.value) return
  if (!confirm(`接取「${c.title}」将消耗 ${c.accept_cost} 贡献度，确定吗？`)) return
  accepting.value = true
  try {
    const res = await store.acceptCommission(c.id, resolvedAdvId.value)
    if (res.code === 200) {
      message.success('委托接取成功！')
      await loadData()
    } else {
      message.error(res.message || '接取失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '接取失败')
  } finally {
    accepting.value = false
  }
}

async function claim() {
  if (!mine.value || claiming.value) return
  claiming.value = true
  try {
    const res = await store.claimCommission(mine.value.id)
    if (res.code === 200) {
      const gold = res.data?.reward_gold ?? 0
      message.success(`领取成功！${gold ? '💰' + gold : ''}`)
      await Promise.all([loadData(), store.fetchAdventurers()])
    } else {
      message.error(res.message || '领取失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '领取失败')
  } finally {
    claiming.value = false
  }
}

async function cancel() {
  if (!mine.value || cancelling.value) return
  if (!confirm(`取消「${mine.value.title}」将退还 ${mine.value.accept_cost} 贡献度，进度清零，确定吗？`)) return
  cancelling.value = true
  try {
    const res = await store.cancelCommission(mine.value.id)
    if (res.code === 200) {
      message.success('委托已取消，贡献度已退还')
      await loadData()
    } else {
      message.error(res.message || '取消失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消失败')
  } finally {
    cancelling.value = false
  }
}

async function adminRefill() {
  if (refilling.value) return
  refilling.value = true
  try {
    const res = await store.adminRefillCommissions()
    if (res.code === 200) {
      message.success(`已补满，生成 ${res.data?.generated ?? 0} 个委托`)
      await loadData()
    } else {
      message.error(res.message || '补满失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '补满失败')
  } finally {
    refilling.value = false
  }
}

async function adminRefresh() {
  if (refreshing.value) return
  if (!confirm('将删除所有未接取的委托并重新生成，确定吗？')) return
  refreshing.value = true
  try {
    const res = await store.adminRefreshCommissions()
    if (res.code === 200) {
      message.success(`已刷新，删除 ${res.data?.deleted ?? 0} 个，生成 ${res.data?.generated ?? 0} 个`)
      await loadData()
    } else {
      message.error(res.message || '刷新失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '刷新失败')
  } finally {
    refreshing.value = false
  }
}

async function adminRefreshOne(c: CommissionItem) {
  if (refreshingId.value !== null) return
  if (!confirm(`将删除「${c.title}」并重新生成一个同档委托，确定吗？`)) return
  refreshingId.value = c.id
  try {
    const res = await store.adminRefreshCommission(c.id)
    if (res.code === 200) {
      message.success('已刷新该委托')
      await loadData()
    } else {
      message.error(res.message || '刷新失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '刷新失败')
  } finally {
    refreshingId.value = null
  }
}

function onCommissionChanged(payload: { action?: string }) {
  if (payload?.action === 'commission_changed') void loadData()
}

onMounted(() => {
  loadData()
  socket.on('adventurer_changed', onCommissionChanged)
})
onUnmounted(() => {
  socket.off('adventurer_changed', onCommissionChanged)
})
</script>

<style scoped>
.commission-panel {
  margin-bottom: 16px;
}
.commission-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.admin-btns {
  display: flex;
  gap: 6px;
}
.contribution-badge {
  font-size: 0.9em;
  color: var(--color-text);
}
.contribution-badge b {
  color: var(--color-primary);
}
.contribution-total {
  color: var(--color-text-secondary);
  font-size: 0.85em;
}
.btn-task-admin {
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-surface, var(--color-bg));
  color: var(--color-text);
  font-size: 0.82em;
  cursor: pointer;
}
.btn-task-admin:hover:not(:disabled) {
  border-color: var(--color-primary);
}
.btn-task-admin:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-card-refresh {
  grid-column: 3;
  justify-self: end;
  padding: 2px 7px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-surface, var(--color-bg));
  color: var(--color-text);
  font-size: 0.85em;
  line-height: 1.2;
  cursor: pointer;
  white-space: nowrap;
}
.btn-card-refresh:hover:not(:disabled) {
  border-color: var(--color-primary);
}
.btn-card-refresh:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.section-title {
  font-size: 0.9em;
  margin: 0 0 8px;
}
.tier-sub {
  font-size: 0.78em;
  color: var(--color-text-secondary);
  font-weight: 400;
}
.tier-block {
  margin-bottom: 16px;
}
.task-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
  gap: 8px;
}
.task-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 12px;
  cursor: pointer;
  transition: border-color 0.2s;
}
.task-card:hover:not(.disabled) {
  border-color: var(--color-primary);
}
.task-card.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.task-card-header {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto;
  align-items: start;
  gap: 10px;
  margin-bottom: 6px;
}
.task-icon {
  font-size: 1.4em;
}
.task-card-info {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.task-name {
  font-weight: 600;
  font-size: 0.95em;
}
.task-category {
  font-size: 0.75em;
  color: var(--color-text-secondary);
}
.task-duration {
  min-width: 0;
  max-width: 100px;
  text-align: right;
  overflow-wrap: anywhere;
  font-size: 0.8em;
  color: var(--color-warning);
  white-space: normal;
}
.task-desc {
  font-size: 0.85em;
  color: var(--color-text-secondary);
  margin-bottom: 6px;
  line-height: 1.4;
}
.task-req-line {
  font-size: 0.82em;
  color: var(--color-text);
  margin-bottom: 6px;
}
.progress-line {
  color: var(--color-primary);
  font-weight: 600;
}
.task-meta {
  display: grid;
  gap: 6px;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--color-border);
  font-size: 0.8em;
}
.task-requirements {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--color-warning);
}
.task-rewards {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--color-success);
}
.task-rewards.center {
  justify-content: center;
  margin: 8px 0;
}
.task-reward-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.task-reward-item-name {
  max-width: 12em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-reward-preview-trigger {
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}
.task-reward-preview-trigger:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
  border-radius: 3px;
}
.task-rewards .rarity-pill {
  padding: 0 4px;
  border: 1px solid;
  border-radius: 4px;
  font-size: 0.72em;
  line-height: 1.35;
}
.task-reject-reason {
  font-size: 0.8em;
  color: var(--color-danger);
  margin-top: 6px;
}
.active-task-section {
  margin-bottom: 16px;
}
.active-task-card {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 14px;
}
.task-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.task-adv-name {
  font-size: 0.85em;
  color: var(--color-text-secondary);
}
.task-active-desc {
  font-size: 0.9em;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin-bottom: 8px;
}
.task-expedition-hint {
  margin-top: 10px;
}
.expedition-note {
  font-size: 0.82em;
  color: var(--color-text-secondary);
  margin: 8px 0 0;
  text-align: center;
}
.remaining-text {
  font-size: 0.85em;
  color: var(--color-warning);
  text-align: center;
}
.empty-helpers {
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 0.85em;
  padding: 12px;
}
.commission-tips {
  margin-top: 16px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--color-surface, var(--color-bg));
  border: 1px dashed var(--color-border);
}
.tip-item {
  font-size: 0.8em;
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.tip-item + .tip-item {
  margin-top: 4px;
}
.btn-cancel-commission {
  display: block;
  width: 100%;
  margin-top: 10px;
  padding: 6px 12px;
  border-radius: 6px;
  border: 1px solid var(--color-danger, #dc3545);
  background: transparent;
  color: var(--color-danger, #dc3545);
  font-size: 0.82em;
  cursor: pointer;
  transition: background 0.2s, color 0.2s;
}
.btn-cancel-commission:hover:not(:disabled) {
  background: var(--color-danger, #dc3545);
  color: #fff;
}
.btn-cancel-commission:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.advs1-submit {
  background: var(--color-primary);
  color: #fff;
  border: none;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}
.advs1-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.task-complete-btn {
  display: block;
  width: 100%;
  margin-top: 8px;
}
/* 驻地悬赏墙：分级委托纸张、危险识别线与清晰奖励区。 */
.commission-panel { --commission-blue: color-mix(in srgb, #5d87b3 76%, var(--color-primary)); }
.commission-toolbar {
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--commission-blue) 22%, var(--color-border));
  border-radius: 13px;
  background: linear-gradient(120deg, color-mix(in srgb, var(--commission-blue) 7%, var(--color-surface)), var(--color-surface));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent);
}
.contribution-badge {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 5px;
}
.contribution-badge b {
  padding: 2px 8px;
  border-radius: 7px;
  background: color-mix(in srgb, var(--commission-blue) 12%, var(--color-bg));
  color: var(--commission-blue);
}
.commission-tips {
  border-color: color-mix(in srgb, var(--commission-blue) 24%, var(--color-border));
  background: color-mix(in srgb, var(--commission-blue) 4%, var(--color-bg));
}
.section-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  padding-bottom: 7px;
  border-bottom: 1px solid color-mix(in srgb, var(--commission-blue) 14%, var(--color-border));
  color: var(--color-text);
}
.tier-block { padding: 12px; border: 1px solid color-mix(in srgb, var(--commission-blue) 12%, var(--color-border)); border-radius: 13px; background: var(--color-surface); }
.task-list { gap: 10px; }
.task-card,
.active-task-card {
  --tier-color: var(--commission-blue);
  position: relative;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--tier-color) 22%, var(--color-border));
  border-radius: 12px;
  background:
    linear-gradient(105deg, color-mix(in srgb, var(--tier-color) 6%, transparent), transparent 44%),
    var(--color-bg);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 3%, transparent);
}
.task-card.tier-junior,
.active-task-card.tier-junior { --tier-color: color-mix(in srgb, #6f9abc 76%, var(--color-primary)); }
.task-card.tier-intermediate,
.active-task-card.tier-intermediate { --tier-color: color-mix(in srgb, #537eb4 80%, var(--color-primary)); }
.task-card.tier-advanced,
.active-task-card.tier-advanced { --tier-color: color-mix(in srgb, #726dad 78%, var(--color-primary)); }
.task-card::before,
.active-task-card::before {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: linear-gradient(180deg, var(--tier-color), transparent 82%);
}
.task-card {
  transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}
.task-card:hover:not(.disabled) {
  transform: translateX(4px);
  border-color: color-mix(in srgb, var(--tier-color) 58%, var(--color-border));
  box-shadow: 0 8px 20px color-mix(in srgb, var(--tier-color) 12%, transparent);
}
.task-card .task-icon,
.active-task-card .task-icon { filter: drop-shadow(0 4px 6px color-mix(in srgb, var(--tier-color) 25%, transparent)); }
.task-duration {
  padding: 2px 7px;
  border: 1px solid color-mix(in srgb, var(--tier-color) 24%, var(--color-border));
  border-radius: 7px;
  background: color-mix(in srgb, var(--tier-color) 7%, var(--color-bg));
  color: var(--tier-color);
}
.progress-line { color: var(--tier-color); }
.task-meta {
  gap: 10px;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px dashed color-mix(in srgb, var(--tier-color) 20%, var(--color-border));
}
.task-rewards { justify-content: flex-start; }
.active-task-card { box-shadow: inset 3px 0 var(--tier-color), 0 8px 22px color-mix(in srgb, var(--tier-color) 10%, transparent); }
.task-complete-btn { background: var(--commission-blue); }

@media (max-width: 640px) {
  .commission-toolbar { align-items: stretch; flex-direction: column; gap: 9px; }
  .admin-btns button { flex: 1; min-height: 36px; }
  .tier-block { padding: 10px; }
  .task-card:hover:not(.disabled) { transform: none; box-shadow: none; }
  .task-card:active:not(.disabled) { transform: scale(0.99); }
  .task-card-header { align-items: flex-start; flex-wrap: wrap; }
  .task-card-info { min-width: 0; }
  .task-duration { margin-left: 0; white-space: normal; }
  .task-meta { align-items: flex-start; flex-direction: column; }
  .task-rewards { justify-content: flex-start; }
}
@media (prefers-reduced-motion: reduce) { .task-card { transition: none; } }
@media (max-width: 640px) {
  .commission-toolbar { flex-wrap: wrap; gap: 8px; }
  .commission-toolbar > button { flex: 1; white-space: normal; }
  .task-card-header, .task-header { flex-wrap: wrap; gap: 8px; }
  .task-card-info { min-width: 0; }
  .task-meta { flex-direction: column; align-items: flex-start; gap: 10px; }
  .task-requirements, .task-req-line { flex-wrap: wrap; }
  .task-rewards { justify-content: flex-start; }
  .task-reward-preview-trigger { min-height: 32px; padding-block: 4px; }
  .task-reward-item-name { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
  .task-desc, .task-active-desc { font-size: 13px; line-height: 1.6; }
}
</style>
