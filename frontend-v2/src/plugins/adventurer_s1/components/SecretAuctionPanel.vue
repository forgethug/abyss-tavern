<template>
  <div class="secret-auction-panel">
    <div class="tab-row">
      <button
        type="button"
        class="tab-btn"
        :class="{ active: activeTab === 'open' }"
        @click="switchTab('open')"
      >进行中</button>
      <button
        type="button"
        class="tab-btn"
        :class="{ active: activeTab === 'history' }"
        @click="switchTab('history')"
      >已结束</button>
    </div>

    <p v-if="activeTab === 'open'" class="hint">
      密封出价：他人看不到你的出价，仅显示参与人数。开奖后最高者得，同价则流拍全员退钱。
    </p>
    <p v-else class="hint">
      历史记录仅展示拍品结果与中标冒险者，不公开成交价格。
    </p>

    <!-- 进行中 -->
    <template v-if="activeTab === 'open'">
      <div v-if="store.secretAuctionLoading && store.secretAuctionLots.length === 0" class="empty">加载中…</div>
      <div v-else-if="store.secretAuctionLots.length === 0" class="empty">暂无进行中的拍品</div>
      <div v-else class="lot-grid">
        <div v-for="lot in store.secretAuctionLots" :key="lot.id" class="lot-card" tabindex="0">
          <div class="lot-head">
            <strong class="lot-name" :title="lot.item_name" :style="{ color: rarityColor(lot.rarity) }">{{ lot.item_name }}</strong>
            <span class="rarity-pill" :style="{ color: rarityColor(lot.rarity), borderColor: rarityColor(lot.rarity) }">
              {{ rarityLabel(lot.rarity) }}
            </span>
            <span v-if="lot.is_immutable" class="immutable-pill">不可变</span>
            <span v-if="(lot.quantity ?? 1) > 1" class="meta">×{{ lot.quantity }}</span>
          </div>
          <ItemDescription class="lot-desc" :text="lot.desc" />
          <div class="lot-stats">
            <span>👥 {{ lot.bid_count ?? 0 }} 人出价</span>
            <span v-if="(lot.reserve_price ?? 0) > 0">📌 底价 {{ lot.reserve_price!.toFixed(2) }}💰</span>
            <span>⏰ {{ formatAuctionEnd(lot.ends_at) }}</span>
          </div>
          <div v-if="lot.my_bid_amount != null" class="my-bid">
            我的出价：<strong>{{ lot.my_bid_amount.toFixed(2) }}💰</strong>
          </div>
          <div class="bid-actions">
            <button
              type="button"
              class="btn-primary btn-sm"
              :disabled="busy || !myAdv"
              @click="openBidDialog(lot)"
            >
              {{ lot.my_bid_amount != null ? '修改出价' : '出价' }}
            </button>
            <button
              v-if="lot.my_bid_amount != null"
              type="button"
              class="btn-danger btn-sm"
              :disabled="busy || !myAdv"
              @click="cancelBid(lot)"
            >
              取消出价
            </button>
          </div>
        </div>
      </div>
      <Pagination v-if="store.secretAuctionPagination" :pagination="store.secretAuctionPagination" @go="goOpenPage" />
    </template>

    <!-- 已结束 -->
    <template v-else>
      <div v-if="store.secretAuctionHistoryLoading && store.secretAuctionHistory.length === 0" class="empty">加载中…</div>
      <div v-else-if="store.secretAuctionHistory.length === 0" class="empty">暂无历史记录</div>
      <div v-else class="lot-grid">
        <div v-for="lot in store.secretAuctionHistory" :key="lot.id" class="lot-card history-card" tabindex="0">
          <div class="lot-head">
            <strong class="lot-name" :style="{ color: rarityColor(lot.rarity) }">{{ lot.item_name }}</strong>
            <span class="rarity-pill" :style="{ color: rarityColor(lot.rarity), borderColor: rarityColor(lot.rarity) }">
              {{ rarityLabel(lot.rarity) }}
            </span>
            <span v-if="lot.is_immutable" class="immutable-pill">不可变</span>
            <span class="status-tag">{{ historyStatusLabel(lot.status) }}</span>
          </div>
          <ItemDescription class="lot-desc" :text="lot.desc" />
          <div class="lot-stats">
            <span>👥 {{ lot.bid_count ?? 0 }} 人参与</span>
            <span>⏰ {{ formatAuctionEnd(lot.ends_at) }}</span>
          </div>
          <div v-if="lot.status === 'settled_won' && lot.winner_name" class="winner-line">
            🏆 中标：<strong>{{ lot.winner_name }}</strong>
          </div>
          <div v-if="lot.my_result === 'won'" class="result-tag won">你已中标</div>
          <div v-else-if="lot.my_result === 'lost'" class="result-tag lost">未中标（已退钱）</div>
        </div>
      </div>
      <Pagination v-if="store.secretAuctionHistoryPagination" :pagination="store.secretAuctionHistoryPagination" @go="goHistoryPage" />
    </template>

    <!-- 出价弹窗 -->
    <div v-if="bidTarget" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="bidTarget = null">
      <div class="modal-card s1-mobile-dialog">
        <header class="modal-head s1-mobile-dialog-header">
          <h3 class="modal-title">出价「{{ bidTarget.item_name }}」</h3>
          <button type="button" class="modal-close" @click="bidTarget = null">✕</button>
        </header>
        <div class="modal-body s1-mobile-dialog-body">
          <p class="muted small">
            金币将预先扣除；他人看不到你的出价。改价会先退还旧出价再扣新价。
          </p>
          <p v-if="myAdv" class="muted small">当前金币：{{ (myAdv.gold ?? 0).toFixed(2) }}💰</p>
          <p v-if="bidTarget && (bidTarget.reserve_price ?? 0) > 0" class="muted small">
            底价 {{ bidTarget.reserve_price!.toFixed(2) }}💰，出价须不低于底价
          </p>
          <label class="bid-label">
            出价金额（💰）
            <input
              v-model.number="bidAmount"
              type="number"
              min="0.01"
              step="0.01"
              class="bid-input"
              placeholder="输入期望价格"
            />
          </label>
          <div class="modal-actions">
            <button type="button" class="btn-secondary btn-sm" @click="bidTarget = null">取消</button>
            <button type="button" class="btn-primary btn-sm" :disabled="busy" @click="submitBid">确认出价</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import ItemDescription from './ItemDescription.vue'
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useAdventurerS1Store, isPlayableAdventurer, type SecretAuctionLot, type SecretAuctionLotHistory } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useSocket } from '@/composables/useSocket'
import Pagination from '@/components/Pagination.vue'
import { formatDateTime } from '@/utils/time'
import { rarityColor, rarityLabel } from '../utils'

const store = useAdventurerS1Store()
const message = useGlobalMessage()

type PanelTab = 'open' | 'history'

const activeTab = ref<PanelTab>('open')
const busy = ref(false)
const bidTarget = ref<SecretAuctionLot | null>(null)
const bidAmount = ref<number | null>(null)

const myAdv = computed(() => store.adventurers.find(isPlayableAdventurer) ?? null)

function historyStatusLabel(status: SecretAuctionLotHistory['status']): string {
  return { settled_won: '已成交', settled_pass: '流拍', cancelled: '已取消' }[status] ?? status
}

function formatAuctionEnd(endsAt: string | null): string {
  return endsAt ? formatDateTime(endsAt) : '时间待定'
}

async function refreshOpen(page = store.secretAuctionPagination?.current_page ?? 1) {
  await store.fetchSecretAuctionLots(page, myAdv.value?.id)
  void store.refreshSecretAuctionOpenCount(true)
}

async function refreshHistory(page = store.secretAuctionHistoryPagination?.current_page ?? 1) {
  await store.fetchSecretAuctionHistory(page, myAdv.value?.id)
}

function goOpenPage(page: number) {
  void refreshOpen(page)
}

function goHistoryPage(page: number) {
  void refreshHistory(page)
}

async function switchTab(tab: PanelTab) {
  activeTab.value = tab
  if (tab === 'open') {
    await refreshOpen(1)
  } else {
    await refreshHistory(1)
  }
}

function openBidDialog(lot: SecretAuctionLot) {
  bidTarget.value = lot
  bidAmount.value = lot.my_bid_amount ?? null
}

async function submitBid() {
  if (!myAdv.value || !bidTarget.value || busy.value) return
  const amount = bidAmount.value
  if (amount == null || amount <= 0) {
    message.warning('请输入有效出价')
    return
  }
  busy.value = true
  try {
    const res = await store.placeSecretAuctionBid(bidTarget.value.id, myAdv.value.id, amount)
    if (res.code === 200) {
      message.success('出价成功，金币已预先扣除')
      bidTarget.value = null
      await Promise.all([refreshOpen(), store.fetchAdventurers()])
    } else {
      message.error(res.message || '出价失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '出价失败')
  } finally {
    busy.value = false
  }
}

async function cancelBid(lot: SecretAuctionLot) {
  if (!myAdv.value || busy.value) return
  if (!confirm(`确认取消「${lot.item_name}」的出价？金币将全额退还。`)) return
  busy.value = true
  try {
    const res = await store.cancelSecretAuctionBid(lot.id, myAdv.value.id)
    if (res.code === 200) {
      message.success('出价已取消，金币已退还')
      await Promise.all([refreshOpen(), store.fetchAdventurers()])
    } else {
      message.error(res.message || '取消失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消失败')
  } finally {
    busy.value = false
  }
}

const socket = useSocket()

onMounted(async () => {
  if (store.adventurers.length === 0) {
    await store.fetchAdventurers()
  }
  await refreshOpen(1)

  socket.on('adventurer_s1_secret_auction_changed', onAuctionChanged)
})

onUnmounted(() => {
  socket.off('adventurer_s1_secret_auction_changed', onAuctionChanged)
})

function onAuctionChanged() {
  refreshOpen()
}
</script>

<style scoped lang="scss">
.secret-auction-panel {
  margin-top: 12px;
}

.tab-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.tab-btn {
  font-size: 13px;
  padding: 4px 14px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;

  &.active {
    border-color: var(--color-primary);
    color: var(--color-primary);
    font-weight: 600;
  }
}

.hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.empty {
  padding: 24px 0;
  text-align: center;
  color: var(--color-text-secondary);
}

.lot-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 250px), 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.lot-card {
  padding: 10px 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;

  &.history-card {
    opacity: 0.95;
  }
}

.lot-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.lot-name {
  font-size: 14px;
}

.lot-desc {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.lot-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.my-bid {
  margin-top: 6px;
  font-size: 12px;
  color: var(--color-text);
}

.winner-line {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-text);
}

.status-tag {
  font-size: 10px;
  padding: 0 6px;
  border-radius: 10px;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
}

.result-tag {
  margin-top: 6px;
  font-size: 11px;
  font-weight: 600;

  &.won {
    color: var(--color-primary);
  }

  &.lost {
    color: var(--color-text-secondary);
  }
}

.bid-actions {
  display: flex;
  gap: 6px;
  margin-top: 8px;

  button {
    flex: 1;
  }
}

.btn-danger {
  font-size: 13px;
  padding: 4px 12px;
  border: 1px solid var(--color-danger, #e74c3c);
  border-radius: 8px;
  background: transparent;
  color: var(--color-danger, #e74c3c);
  cursor: pointer;

  &:hover:not(:disabled) {
    background: var(--color-danger, #e74c3c);
    color: #fff;
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.rarity-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 0 6px;
  border: 1px solid currentColor;
  border-radius: 10px;
}

.immutable-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 0 6px;
  border: 1px solid color-mix(in srgb, var(--auction-violet) 55%, var(--color-border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--auction-violet) 12%, var(--color-bg));
  color: var(--auction-violet);
}

.meta {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(0, 0, 0, 0.55);
}

.modal-card {
  width: 100%;
  max-width: 400px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  overflow: hidden;
}

.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-border);
}

.modal-title {
  margin: 0;
  font-size: 15px;
  color: var(--color-text);
}

.modal-close {
  border: none;
  background: transparent;
  font-size: 16px;
  color: var(--color-text-secondary);
  cursor: pointer;
}

.modal-body {
  padding: 14px 16px;
}

.muted {
  color: var(--color-text-secondary);

  &.small {
    font-size: 12px;
  }
}

.bid-label {
  display: block;
  margin-top: 12px;
  font-size: 13px;
  color: var(--color-text);
}

.bid-input {
  display: block;
  width: 100%;
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}
/* 隐秘拍卖：冷紫蓝聚光、密封竞价与克制的状态脉冲。 */
.secret-auction-panel {
  --auction-violet: color-mix(in srgb, #766fae 78%, var(--color-primary));
  position: relative;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--auction-violet) 24%, var(--color-border));
  border-radius: 15px;
  background:
    radial-gradient(circle at 85% 0%, color-mix(in srgb, var(--auction-violet) 13%, transparent), transparent 30%),
    linear-gradient(145deg, color-mix(in srgb, var(--auction-violet) 5%, var(--color-surface)), var(--color-surface));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent);
}
.tab-row {
  padding: 5px;
  border: 1px solid color-mix(in srgb, var(--auction-violet) 18%, var(--color-border));
  border-radius: 11px;
  background: var(--color-bg);
}
.tab-btn { flex: 1; border: 0; border-radius: 7px; }
.tab-btn.active {
  border-color: transparent;
  background: color-mix(in srgb, var(--auction-violet) 14%, var(--color-surface));
  color: var(--auction-violet);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--auction-violet) 26%, transparent);
}
.hint {
  padding: 10px 12px;
  border-left: 3px solid var(--auction-violet);
  border-radius: 0 8px 8px 0;
  background: color-mix(in srgb, var(--auction-violet) 6%, var(--color-bg));
}
.lot-grid { gap: 11px; }
.lot-card {
  min-width: 0;
  position: relative;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--auction-violet) 20%, var(--color-border));
  border-radius: 12px;
  background:
    radial-gradient(circle at 100% 0%, color-mix(in srgb, var(--auction-violet) 10%, transparent), transparent 34%),
    var(--color-bg);
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
  display: flex;
  flex-direction: column;
}
.lot-card:not(.history-card) { padding: 12px; }
.lot-card::before {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: linear-gradient(180deg, var(--auction-violet), transparent 78%);
}
.lot-card:hover {
  transform: translateY(-2px);
  border-color: color-mix(in srgb, var(--auction-violet) 50%, var(--color-border));
  box-shadow: 0 10px 26px color-mix(in srgb, var(--auction-violet) 14%, transparent);
}
.lot-head { align-content: flex-start; }
.lot-name { min-width: 0; max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
.lot-desc { margin: 8px 0; }
.lot-card:focus {
  outline: 2px solid color-mix(in srgb, var(--auction-violet) 36%, transparent);
  outline-offset: 2px;
}
.lot-stats { align-content: flex-start; gap: 6px 10px; margin-bottom: 8px; }
.my-bid {
  position: relative;
  border: 1px solid color-mix(in srgb, var(--auction-violet) 28%, var(--color-border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--auction-violet) 9%, var(--color-bg));
  color: var(--auction-violet);
}
.my-bid::after {
  content: '';
  position: absolute;
  inset: -1px;
  border-radius: inherit;
  box-shadow: 0 0 13px color-mix(in srgb, var(--auction-violet) 14%, transparent);
  opacity: 0;
  pointer-events: none;
  animation: sealed-bid-pulse 3s ease-in-out infinite;
}
.bid-actions .btn-primary { background: var(--auction-violet); border-color: var(--auction-violet); }
.bid-actions {
  min-height: 34px;
  margin-top: auto;
  padding-top: 9px;
  border-top: 1px dashed color-mix(in srgb, var(--auction-violet) 18%, var(--color-border));
}
.bid-actions button {
  min-width: 92px;
  min-height: 34px;
  padding: 5px 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  white-space: nowrap;
}
.modal-card { border-color: color-mix(in srgb, var(--auction-violet) 28%, var(--color-border)); }
.bid-input:focus { border-color: var(--auction-violet); box-shadow: 0 0 0 3px color-mix(in srgb, var(--auction-violet) 10%, transparent); }
@keyframes sealed-bid-pulse { 50% { opacity: 1; } }

@media (max-width: 640px) {
  .secret-auction-panel { padding: 12px; }
  .lot-card:hover { transform: none; box-shadow: none; }
  .lot-card:active { transform: scale(0.99); }
  .lot-grid { grid-template-columns: minmax(0, 1fr); }
  .lot-card:not(.history-card) { padding: 12px; }
  .bid-actions { align-items: stretch; flex-wrap: wrap; gap: 8px; }
  .bid-actions button { flex: 1; white-space: normal; }
  .bid-actions button { min-height: 38px; }
}
@media (prefers-reduced-motion: reduce) {
  .lot-card { transition: none; }
  .my-bid::after { animation: none; }
}
</style>
