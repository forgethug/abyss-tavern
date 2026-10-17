<template>
  <div class="lucky-wheel-panel">
    <div class="lucky-wheel-header">
      <div class="wheel-heading">
        <span class="wheel-heading-icon">🎡</span>
        <div>
          <h5 class="section-title">幸运大转盘</h5>
          <p>每次转动必得一件奖品</p>
        </div>
      </div>
      <div v-if="pool?.adventurer" class="lucky-wheel-wallet">
        <span class="wallet-owner">{{ pool.adventurer.name }}</span>
        <span class="wallet-chip">🎫 {{ pool.adventurer.ticket_count }}</span>
      </div>
    </div>
    <p class="lucky-wheel-hint">
      每次抽奖消耗 1 张「{{ pool?.ticket_item_name ?? '大转盘抽奖券' }}」，100% 中奖，无需派遣占位。
    </p>
    <p v-if="pool?.adventurer?.spin_block_reason" class="lucky-wheel-block-hint">
      ⚠️ {{ pool.adventurer.spin_block_reason }}
    </p>

    <div v-if="loading" class="empty-helpers">加载中…</div>
    <template v-else-if="pool">
      <div v-if="!pool.adventurer" class="empty-helpers no-adv-hint">
        你还没有可操作的冒险者，请先在「我的冒险者」创建
      </div>
      <template v-else>
      <div class="wheel-dashboard">
        <section class="spin-station">
          <div class="station-orbit">
            <span class="station-icon" :class="{ spinning }">🎡</span>
            <div
              class="desktop-wheel-stage"
              :class="[{ 'is-spinning': spinning, 'has-result': !!lastResult && !spinning }, lastResultRarityClass]"
              :style="{ '--wheel-angle': `${wheelAngle}deg`, '--wheel-gradient': wheelGradient }"
              aria-hidden="true"
            >
              <div class="wheel-aura"></div>
              <div class="wheel-sparkles">
                <i v-for="n in 12" :key="n" :style="{ '--spark-index': n }"></i>
              </div>
              <div class="wheel-pointer"><span></span></div>
              <div class="wheel-machine">
                <div class="wheel-lights">
                  <i v-for="n in 16" :key="n" :style="{ '--light-index': n }"></i>
                </div>
                <div class="wheel-disc">
                  <div class="wheel-hub">
                    <span>{{ spinning ? '命运' : '好运' }}</span>
                    <small>{{ spinning ? '转动中' : '必中奖' }}</small>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div class="station-copy">
            <strong>{{ spinning ? '命运正在转动…' : '使用抽奖券开始抽取' }}</strong>
            <span>每次消耗 1 张抽奖券</span>
          </div>
          <div class="lucky-wheel-actions">
            <button
              type="button"
              class="spin-button ticket"
              :disabled="spinning || !canSpin"
              @click="doSpin()"
            >
              <span>🎫</span>
              {{ spinning ? '抽奖中…' : '使用抽奖券' }}
            </button>
          </div>
          <div class="batch-spin-controls">
            <label>连抽次数
              <input v-model.number="batchCount" type="number" min="2" :max="pool.adventurer.ticket_count" step="1" :disabled="spinning">
            </label>
            <button type="button" class="btn-secondary" :disabled="spinning || !canBatchSpin" @click="pendingBatchCount = Number(batchCount)">
              开始连抽
            </button>
            <button v-if="spinning && requestedSpins > 1" type="button" class="btn-secondary" :disabled="stopRequested" @click="stopRequested = true">
              {{ stopRequested ? '本批完成后停止' : '停止连抽' }}
            </button>
          </div>
          <p v-if="requestedSpins > 1" class="batch-spin-progress" aria-live="polite">
            {{ spinning ? '连抽中' : '连抽结束' }}：已确认 {{ completedSpins }} / {{ requestedSpins }} 次
          </p>
          <p class="batch-spin-hint">连抽每批最多 {{ pool.batch_size }} 次；停止或关闭面板后，已发起的一批会正常完成，奖品可在历史中查看。</p>

          <div
            class="lucky-wheel-result"
            :class="lastResult ? lastResultRarityClass : 'result-placeholder'"
            aria-live="polite"
          >
            <template v-if="lastResult" :key="resultRevealKey">
              <div class="result-title">
                <span class="result-rarity-icon">{{ resultRarityIcon }}</span>
                {{ spinning ? '上次获得' : resultRevealTitle }}
              </div>
              <div class="result-item">
                <span class="result-name" :style="{ color: rarityColor(lastResult.item_rarity) }">
                  {{ lastResult.item_name }}
                </span>
                <span
                  class="result-meta"
                  :style="{ borderColor: rarityColor(lastResult.item_rarity), color: rarityColor(lastResult.item_rarity) }"
                >{{ rarityLabel(lastResult.item_rarity) }}</span>
              </div>
              <p v-if="lastResult.item_description" class="result-desc">{{ lastResult.item_description }}</p>
              <p v-if="lastResultDestinationHint" class="result-dest-hint">{{ lastResultDestinationHint }}</p>
            </template>
            <template v-else>
              <div class="result-title">
                <span class="result-rarity-icon">🎁</span>
                等待转动
              </div>
              <div class="result-item">
                <span class="result-name">奖品将在这里揭晓</span>
                <span class="result-meta">必中奖</span>
              </div>
              <p class="result-desc">使用一张抽奖券，看看今天的好运会落在哪里。</p>
            </template>
          </div>
        </section>

        <section class="spin-history">
        <div class="history-head">
          <h6 class="pool-title">抽取历史</h6>
          <span v-if="history" class="history-total">共 {{ history.total }} 次</span>
        </div>
        <div v-if="historyLoading" class="empty-helpers">加载中…</div>
        <div v-else-if="!history?.items.length" class="empty-helpers">还没有抽取记录</div>
        <ul v-else class="history-list">
          <li v-for="row in history.items" :key="row.id" class="history-item">
            <span class="history-name" :style="{ color: rarityColor(row.item_rarity) }">
              {{ row.item_name }}
            </span>
            <span class="history-rarity" :style="{ color: rarityColor(row.item_rarity) }">
              {{ rarityLabel(row.item_rarity) }}
            </span>
            <span class="history-payment">
              {{ row.payment === 'gold' ? `💰 ${row.gold_cost} 金币` : '🎫 抽奖券' }}
            </span>
            <span class="history-time">{{ row.created_at ? formatDateTime(row.created_at) : '' }}</span>
          </li>
        </ul>
        <div v-if="history && history.total > history.page_size" class="history-pagination">
          <button
            type="button"
            class="btn-secondary history-page-btn"
            :disabled="historyLoading || history.page <= 1"
            @click="loadHistory(history.page - 1)"
          >上一页</button>
          <span>第 {{ history.page }} / {{ Math.ceil(history.total / history.page_size) }} 页</span>
          <button
            type="button"
            class="btn-secondary history-page-btn"
            :disabled="historyLoading || !history.has_more"
            @click="loadHistory(history.page + 1)"
          >下一页</button>
        </div>
        </section>
      </div>
      </template>

      <details class="lucky-wheel-pool">
        <summary class="pool-summary">
          <span>查看完整奖池与概率</span>
          <span class="pool-summary-hint">共 {{ pool.tiers.reduce((sum, tier) => sum + tier.items.length, 0) }} 件</span>
        </summary>
        <div class="pool-content">
        <div v-if="pool.legendary_rotation?.next" class="next-legendary-banner">
          <div class="next-legendary-label">
            {{ pool.legendary_rotation.next.is_fallback
              ? '🔮 传说保底（轮换抽完后上架，可重复获得）'
              : '🔮 下一传说奖品（当前传说被抽走后上架）' }}
          </div>
          <div class="next-legendary-card">
            <div class="next-legendary-head">
              <span
                class="next-legendary-name"
                :style="{ color: rarityColor(pool.legendary_rotation.next.rarity) }"
              >
                {{ pool.legendary_rotation.next.name }}
              </span>
              <span v-if="pool.legendary_rotation.next.world_unique" class="world-unique-badge">全服唯一</span>
              <span v-if="pool.legendary_rotation.next.is_fallback" class="fallback-badge">保底</span>
              <span v-if="pool.legendary_rotation.next.text_locked" class="text-locked-badge">不可变</span>
              <span
                v-if="pool.legendary_rotation.next.item_kind === 'skill_book'"
                class="skill-book-badge"
              >
                技能书 · 一次性学习
              </span>
            </div>
            <p v-if="pool.legendary_rotation.next.grants_skill" class="next-legendary-skill">
              领悟技能：{{ pool.legendary_rotation.next.grants_skill }}
            </p>
            <p v-if="pool.legendary_rotation.next.description" class="next-legendary-desc">
              {{ pool.legendary_rotation.next.description }}
            </p>
          </div>
        </div>
        <div v-for="tier in pool.tiers" :key="tier.rarity" class="pool-tier">
          <div class="tier-head">
            <span class="tier-rarity" :style="{ color: rarityColor(tier.rarity) }">
              {{ rarityLabel(tier.rarity) }}
            </span>
            <span class="tier-prob">概率 {{ tier.probability_percent }}%</span>
          </div>
          <div class="tier-items">
            <div v-for="item in tier.items" :key="item.name" class="tier-item">
              <div class="tier-item-head">
                <span class="tier-item-name" :style="{ color: rarityColor(item.rarity) }">{{ item.name }}</span>
                <span v-if="item.world_unique" class="world-unique-badge">全服唯一</span>
                <span v-if="item.is_fallback" class="fallback-badge">保底</span>
                <span v-if="item.text_locked" class="text-locked-badge">不可变</span>
                <span v-if="item.item_kind === 'skill_book'" class="skill-book-badge">技能书</span>
              </div>
              <p v-if="item.grants_skill" class="tier-item-skill">领悟技能：{{ item.grants_skill }}</p>
              <span v-if="item.description" class="tier-item-desc">{{ item.description }}</span>
            </div>
          </div>
        </div>
        </div>
      </details>
    </template>
    <S1ConfirmDialog
      v-if="pendingBatchCount != null"
      title="连续抽奖"
      :message="`准备连续抽取 ${pendingBatchCount} 次，最多消耗 ${pendingBatchCount} 张抽奖券。每批最多 ${pool?.batch_size ?? 1} 次；可以中途停止，已发起的一批会正常完成。`"
      confirm-label="开始连抽"
      @confirm="confirmBatchSpin"
      @cancel="pendingBatchCount = null"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { prefetchChallengeToken } from '@/composables/useApi'
import {
  useAdventurerS1Store,
  isPlayableAdventurer,
  type LuckyWheelPool,
  type LuckyWheelHistoryPage,
  type LuckyWheelSpinResult,
  type LuckyWheelBatchResult,
} from '../stores/adventurerS1'
import { rarityColor, rarityLabel } from '../utils'
import { formatDateTime } from '@/utils/time'
import S1ConfirmDialog from './S1ConfirmDialog.vue'

const props = defineProps<{ building: { id: number } }>()

const store = useAdventurerS1Store()
const message = useMessage()

const loading = ref(false)
const spinning = ref(false)
const pool = ref<LuckyWheelPool | null>(null)
const lastResult = ref<LuckyWheelSpinResult | null>(null)
const history = ref<LuckyWheelHistoryPage | null>(null)
const historyLoading = ref(false)
const resultRevealKey = ref(0)
const wheelAngle = ref(0)
const batchCount = ref<number | string>(10)
const pendingBatchCount = ref<number | null>(null)
const requestedSpins = ref(0)
const completedSpins = ref(0)
const stopRequested = ref(false)
let disposed = false

const myAdventurer = computed(() => store.adventurers.find(isPlayableAdventurer) ?? null)

const wheelSegments = computed(() => {
  let cursor = 0
  const tiers = pool.value?.tiers ?? []
  const totalProbability = tiers.reduce(
    (sum, tier) => sum + Math.max(0, Number(tier.probability_percent)),
    0,
  ) || 100
  return tiers.map((tier) => {
    const size = Math.max(0, Number(tier.probability_percent)) / totalProbability * 360
    const segment = {
      rarity: tier.rarity,
      color: rarityColor(tier.rarity),
      startAngle: cursor,
      endAngle: cursor + size,
      midAngle: cursor + size / 2,
    }
    cursor += size
    return segment
  })
})

const wheelGradient = computed(() => {
  if (wheelSegments.value.length === 0) return '#7c3aed'
  const stops = wheelSegments.value
    .map((segment) => `${segment.color} ${segment.startAngle}deg ${segment.endAngle}deg`)
    .join(', ')
  return `conic-gradient(from 0deg, ${stops})`
})

const canPayTicket = computed(() => (pool.value?.adventurer?.ticket_count ?? 0) > 0)

const canSpin = computed(() => {
  const adv = pool.value?.adventurer
  if (!adv) return false
  if (adv.can_spin === false) return false
  return canPayTicket.value
})
const canBatchSpin = computed(() => {
  const count = Number(batchCount.value)
  return canSpin.value && Number.isSafeInteger(count) && count >= 2
    && count <= (pool.value?.adventurer?.ticket_count ?? 0)
    && Number.isSafeInteger(pool.value?.adventurer?.last_spin_id)
    && (pool.value?.batch_size ?? 0) > 0
})

function spinContextKey(): string {
  return JSON.stringify([store.realmViewCode, store.squadSessionUserId, props.building.id, myAdventurer.value?.id])
}

watch(spinContextKey, () => {
  stopRequested.value = true
  pendingBatchCount.value = null
}, { flush: 'sync' })

onBeforeUnmount(() => {
  disposed = true
  stopRequested.value = true
})

function confirmBatchSpin() {
  const count = pendingBatchCount.value
  pendingBatchCount.value = null
  if (count != null) void doSpin(count)
}

const lastResultDestinationHint = computed(() => {
  const dest = lastResult.value?.grant_destination
  if (dest === 'warehouse') return '奖品已存入仓库（随身栏已满时自动入仓）'
  if (dest === 'equipment') return '奖品已放入随身栏'
  return ''
})

const lastResultRarityClass = computed(() => `rarity-${lastResult.value?.item_rarity ?? 'common'}`)

const resultRarityIcon = computed(() => ({
  common: '✦',
  rare: '💠',
  epic: '🔮',
  legendary: '👑',
}[lastResult.value?.item_rarity ?? 'common'] ?? '✦'))

const resultRevealTitle = computed(() => ({
  common: '本次获得',
  rare: '稀有奖品！',
  epic: '史诗降临！',
  legendary: '传说现世！',
}[lastResult.value?.item_rarity ?? 'common'] ?? '本次获得'))

function spinSuccessMessage(spinData: LuckyWheelSpinResult): string {
  const base = `获得 ${spinData.item_name}！`
  if (spinData.grant_destination === 'warehouse') {
    return `${base}（已存入仓库）`
  }
  return base
}

function desktopAnimationDelay(): Promise<void> {
  const supportsDesktopAnimation = window.matchMedia(
    '(min-width: 900px) and (hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)',
  ).matches
  if (!supportsDesktopAnimation) return Promise.resolve()
  return new Promise<void>((resolve) => window.setTimeout(resolve, 2800))
}

function rotateWheelToRarity(rarity: string): void {
  const segment = wheelSegments.value.find((candidate) => candidate.rarity === rarity)
  const targetAngle = segment ? 360 - segment.midAngle : Math.random() * 360
  const currentTurn = Math.floor(wheelAngle.value / 360)
  wheelAngle.value = (currentTurn + 5) * 360 + targetAngle
}

async function loadPool() {
  loading.value = true
  try {
    const advId = myAdventurer.value?.id ?? pool.value?.adventurer?.id
    const res = await store.fetchLuckyWheelPool(props.building.id, advId)
    if (res.code === 200 && res.data) {
      pool.value = res.data as LuckyWheelPool
    } else {
      message.error(res.message || '加载奖池失败')
    }
  } finally {
    loading.value = false
  }
}

async function doSpin(count: number = 1) {
  if (spinning.value || disposed) return
  if (!canSpin.value || !Number.isSafeInteger(count) || count < 1 || count > (pool.value?.adventurer?.ticket_count ?? 0)) {
    message.error('请确认可用抽奖券和抽取次数')
    return
  }
  const advId = myAdventurer.value?.id ?? pool.value?.adventurer?.id
  if (!advId) {
    message.error('没有可用的冒险者')
    return
  }
  const buildingId = props.building.id
  const contextKey = spinContextKey()
  const stillHere = () => !disposed && contextKey === spinContextKey()
  let needsPoolRefresh = false
  requestedSpins.value = count
  completedSpins.value = 0
  stopRequested.value = false
  spinning.value = true
  try {
    while (completedSpins.value < count) {
      if (stopRequested.value || !stillHere() || !canSpin.value) break
      let spinData: LuckyWheelSpinResult
      let completed = 1
      let stopReason: string | null = null
      if (count > 1) {
        const currentPool = pool.value
        const expectedSpinId = currentPool?.adventurer?.last_spin_id
        const limit = currentPool?.batch_size
        if (!Number.isSafeInteger(expectedSpinId) || !limit || !Number.isSafeInteger(limit)) {
          needsPoolRefresh = true
          message.error('请刷新抽奖面板后重试')
          break
        }
        const chunkSize = Math.min(limit, count - completedSpins.value, currentPool?.adventurer?.ticket_count ?? 0)
        const res = await store.spinLuckyWheelBatch(buildingId, advId, chunkSize, expectedSpinId!)
        if (!stillHere()) break
        if (res.code !== 200 || !res.data) {
          needsPoolRefresh = true
          message.error(res.message || '本批抽奖未完成，已停止继续抽取')
          break
        }
        const batchData = res.data as LuckyWheelBatchResult
        pool.value = batchData.pool
        completed = batchData.completed_count
        stopReason = batchData.stop_reason
        if (completed === 0) {
          if (stopReason) message.warning(stopReason)
          break
        }
        spinData = batchData.results[completed - 1]!
      } else {
        const res = await store.spinLuckyWheel(buildingId, advId)
        if (!stillHere()) break
        if (res.code !== 200 || !res.data) {
          needsPoolRefresh = true
          message.error(res.message || '抽奖未完成')
          break
        }
        spinData = res.data as LuckyWheelSpinResult
        if (pool.value && spinData.adventurer) pool.value.adventurer = spinData.adventurer
        if (spinData.legendary_rotation && pool.value) pool.value.legendary_rotation = spinData.legendary_rotation
        if (spinData.legendary_tier && pool.value) {
          const idx = pool.value.tiers.findIndex((tier) => tier.rarity === 'legendary')
          if (idx >= 0) pool.value.tiers[idx] = spinData.legendary_tier
          else pool.value.tiers.push(spinData.legendary_tier)
        }
      }
      rotateWheelToRarity(spinData.item_rarity)
      if (count === 1) await desktopAnimationDelay()
      if (!stillHere()) break
      lastResult.value = spinData
      resultRevealKey.value += 1
      completedSpins.value += completed
      if (count === 1) message.success(spinSuccessMessage(spinData))
      if (stopReason) {
        message.warning(stopReason)
        break
      }
    }
    if (stillHere() && count > 1 && completedSpins.value > 0) message.success(`已完成 ${completedSpins.value} 次抽取，奖品可在抽取历史中查看`)
  } catch {
    needsPoolRefresh = true
    if (stillHere()) message.error('本次抽奖结果暂未确认，已停止继续抽取，请查看抽取历史')
  } finally {
    spinning.value = false
    if (stillHere()) {
      const refreshes: Promise<unknown>[] = [loadHistory(1, true)]
      if (needsPoolRefresh) refreshes.push(loadPool())
      await Promise.allSettled(refreshes)
    }
  }
}

async function loadHistory(page: number = 1, silent: boolean = false) {
  const advId = myAdventurer.value?.id ?? pool.value?.adventurer?.id
  if (!advId) {
    history.value = null
    return
  }
  if (!silent) historyLoading.value = true
  try {
    const res = await store.fetchLuckyWheelHistory(props.building.id, advId, page, 10)
    if (res.code === 200 && res.data) {
      history.value = res.data as LuckyWheelHistoryPage
    } else {
      message.error(res.message || '加载抽取历史失败')
    }
  } finally {
    if (!silent) historyLoading.value = false
  }
}

onMounted(() => {
  prefetchChallengeToken()
  void loadPool().then(() => loadHistory(1))
})
</script>

<style scoped>
.batch-spin-controls { display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.batch-spin-controls label { display: flex; align-items: center; gap: 8px; }
.batch-spin-controls input {
  width: 76px;
  min-height: 44px;
  box-sizing: border-box;
  padding: 6px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 6px;
  color: var(--color-text, #e6edf3);
  background: var(--color-surface, #161b22);
}
.batch-spin-controls button { min-height: 44px; }
.batch-spin-progress, .batch-spin-hint { font-size: 0.8rem; line-height: 1.6; color: var(--color-text-secondary, #8b949e); text-align: center; }
.lucky-wheel-panel {
  margin-top: 0.5rem;
  min-width: 0;
  max-width: 100%;
  overflow-x: clip;
  color: var(--color-text, #222);
}
.lucky-wheel-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.1rem;
  border: 1px solid color-mix(in srgb, var(--color-border, #ddd) 70%, #f59e0b 30%);
  border-radius: 12px;
  background:
    radial-gradient(circle at 8% 20%, rgb(245 158 11 / 16%), transparent 34%),
    var(--color-surface, #fafafa);
}
.wheel-heading {
  display: flex;
  align-items: center;
  gap: 0.8rem;
}
.wheel-heading-icon {
  display: grid;
  width: 2.8rem;
  height: 2.8rem;
  place-items: center;
  border-radius: 50%;
  font-size: 1.6rem;
  background: rgb(245 158 11 / 14%);
  box-shadow: inset 0 0 0 1px rgb(245 158 11 / 24%);
}
.wheel-heading .section-title {
  margin: 0 0 0.15rem;
  font-size: 1.05rem;
}
.wheel-heading p {
  margin: 0;
  color: var(--color-text-secondary, #666);
  font-size: 0.8rem;
}
.lucky-wheel-wallet {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.45rem;
  font-size: 0.9rem;
}
.wallet-owner {
  margin-right: 0.15rem;
  font-weight: 600;
}
.wallet-chip {
  padding: 0.28rem 0.5rem;
  border: 1px solid var(--color-border, #ddd);
  border-radius: 999px;
  background: var(--color-bg, #fff);
  color: var(--color-text-secondary, #666);
}
.lucky-wheel-hint {
  font-size: 0.85rem;
  color: var(--color-text-secondary, #666);
  margin: 0.7rem 0 1rem;
  padding: 0 0.15rem;
  line-height: 1.5;
}
.lucky-wheel-block-hint {
  margin: -0.5rem 0 1rem;
  padding: 0.55rem 0.75rem;
  border-radius: 8px;
  border: 1px solid rgb(245 158 11 / 35%);
  background: rgb(245 158 11 / 10%);
  color: var(--color-text, #333);
  font-size: 0.85rem;
  line-height: 1.45;
}
.no-adv-hint {
  margin-bottom: 1rem;
}
.wheel-dashboard {
  display: grid;
  grid-template-columns: minmax(12rem, 0.85fr) minmax(0, 1.35fr);
  gap: 0.9rem;
  margin-bottom: 1rem;
}
.spin-station,
.spin-history {
  min-width: 0;
  border: 1px solid var(--color-border, #ddd);
  border-radius: 12px;
  background: var(--color-surface, #fafafa);
}
.spin-station {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 1rem;
  text-align: center;
}
.station-orbit {
  display: grid;
  width: 5rem;
  height: 5rem;
  margin-bottom: 0.65rem;
  place-items: center;
  border: 1px solid rgb(245 158 11 / 28%);
  border-radius: 50%;
  background:
    radial-gradient(circle, rgb(245 158 11 / 18%), transparent 62%),
    var(--color-bg, #fff);
}
.desktop-wheel-stage {
  display: none;
}
.station-icon {
  font-size: 2.75rem;
}
.station-icon.spinning {
  animation: wheel-spin 0.75s linear infinite;
}
.station-copy {
  display: flex;
  flex-direction: column;
  gap: 0.18rem;
}
.station-copy span {
  color: var(--color-text-secondary, #666);
  font-size: 0.78rem;
}
.lucky-wheel-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.75rem;
  width: 100%;
  margin-top: 0.9rem;
}
.spin-button {
  display: inline-flex;
  flex: 1 1 10rem;
  max-width: 14rem;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  min-height: 2.6rem;
  padding: 0.55rem 0.8rem;
  border: 1px solid transparent;
  border-radius: 8px;
  color: #fff;
  font-weight: 600;
  cursor: pointer;
  transition: transform 0.15s ease, filter 0.15s ease;
}
.spin-button.ticket {
  background: linear-gradient(135deg, #2563eb, #7c3aed);
}
.spin-button:not(:disabled):hover {
  filter: brightness(1.08);
  transform: translateY(-1px);
}
.spin-button:disabled {
  cursor: not-allowed;
  filter: grayscale(0.55);
  opacity: 0.48;
}
.lucky-wheel-result {
  width: 100%;
  min-height: 7.25rem;
  padding: 0.75rem 1rem;
  margin-top: 0.9rem;
  border: 1px solid rgb(245 158 11 / 32%);
  border-radius: 8px;
  background: color-mix(in srgb, var(--color-bg, #fff) 88%, #f59e0b 12%);
  text-align: left;
  animation: result-reveal 0.24s ease-out both;
  transition: opacity 0.2s ease, filter 0.2s ease;
}
.result-placeholder {
  color: var(--color-text-secondary, #666);
  border-style: dashed;
  background: color-mix(in srgb, var(--color-bg, #fff) 96%, #f59e0b 4%);
  animation: none;
}
.result-placeholder .result-name {
  color: var(--color-text, #333);
}
.result-title {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-weight: 600;
  margin-bottom: 0.35rem;
}
.result-rarity-icon {
  display: inline-grid;
  width: 1.4rem;
  height: 1.4rem;
  place-items: center;
}
.lucky-wheel-result.rarity-common {
  border-color: rgb(139 148 158 / 48%);
}
.lucky-wheel-result.rarity-rare {
  border-color: rgb(88 166 255 / 65%);
  background: color-mix(in srgb, var(--color-bg, #fff) 92%, #58a6ff 8%);
}
.lucky-wheel-result.rarity-epic {
  border-color: rgb(188 140 255 / 72%);
  background: color-mix(in srgb, var(--color-bg, #fff) 90%, #8b5cf6 10%);
}
.lucky-wheel-result.rarity-legendary {
  border-color: #f59e0b;
  background: color-mix(in srgb, var(--color-bg, #fff) 86%, #f59e0b 14%);
}
.result-item {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.result-name {
  font-weight: 600;
  font-size: 1.05rem;
}
.result-meta {
  padding: 0.08rem 0.4rem;
  border: 1px solid currentColor;
  border-radius: 999px;
  font-size: 0.85rem;
}
.result-desc {
  margin: 0.5rem 0 0;
  font-size: 0.85rem;
  color: var(--color-text-secondary, #666);
  line-height: 1.45;
}
.result-dest-hint {
  margin: 0.45rem 0 0;
  font-size: 0.82rem;
  color: var(--color-text-secondary, #666);
  line-height: 1.4;
}
.spin-history {
  padding: 0.9rem 1rem;
}
.history-head,
.history-item,
.history-pagination {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  min-width: 0;
}
.history-head {
  justify-content: space-between;
  margin-bottom: 0.6rem;
}
.history-head .pool-title {
  margin: 0;
}
.history-total,
.history-payment,
.history-time,
.history-rarity {
  font-size: 0.8rem;
  color: var(--color-text-secondary, #666);
}
.history-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.history-item {
  flex-wrap: wrap;
  padding: 0.5rem 0;
  border-bottom: 1px solid var(--color-border, #eee);
}
.history-item:last-child {
  border-bottom: none;
}
.history-name {
  flex: 1 1 8rem;
  min-width: 0;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.history-time {
  margin-left: auto;
}
.history-pagination {
  justify-content: center;
  margin-top: 0.75rem;
  font-size: 0.82rem;
}
.history-page-btn {
  padding: 0.3rem 0.7rem;
}
.lucky-wheel-pool {
  overflow: hidden;
  border: 1px solid var(--color-border, #ddd);
  border-radius: 12px;
  background: var(--color-surface, #fafafa);
}
.pool-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.8rem 1rem;
  font-size: 0.9rem;
  font-weight: 600;
  cursor: pointer;
  user-select: none;
}
.pool-summary:hover {
  background: color-mix(in srgb, var(--color-surface, #fafafa) 90%, #f59e0b 10%);
}
.pool-summary-hint {
  color: var(--color-text-secondary, #666);
  font-size: 0.78rem;
  font-weight: 400;
}
.pool-content {
  padding: 0 1rem 0.75rem;
  border-top: 1px solid var(--color-border, #eee);
}
.next-legendary-banner {
  margin-bottom: 1rem;
  padding: 0.75rem;
  border-radius: 8px;
  border: 1px dashed var(--color-border, #ccc);
  background: color-mix(in srgb, var(--color-surface, #fafafa) 90%, #7c3aed 10%);
}
.next-legendary-label {
  font-size: 0.85rem;
  font-weight: 600;
  margin-bottom: 0.5rem;
  color: var(--color-text, #333);
}
.next-legendary-card {
  padding: 0.5rem 0.65rem;
  border-radius: 6px;
  background: var(--color-bg, #fff);
  border: 1px solid var(--color-border, #eee);
}
.next-legendary-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
  margin-bottom: 0.35rem;
}
.next-legendary-name {
  font-weight: 600;
  overflow-wrap: anywhere;
}
.next-legendary-skill,
.tier-item-skill {
  margin: 0 0 0.25rem;
  font-size: 0.82rem;
  color: var(--color-text-secondary, #666);
}
.next-legendary-desc {
  margin: 0;
  font-size: 0.8rem;
  color: var(--color-text-secondary, #666);
  line-height: 1.4;
}
.world-unique-badge {
  font-size: 0.72rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  background: #fef3c7;
  color: #92400e;
  border: 1px solid #fcd34d;
}
.fallback-badge {
  font-size: 0.72rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  background: #ecfdf5;
  color: #047857;
  border: 1px solid #6ee7b7;
}
.text-locked-badge {
  font-size: 0.72rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  background: #e0e7ff;
  color: #3730a3;
  border: 1px solid #a5b4fc;
}
.skill-book-badge {
  font-size: 0.72rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  background: #ede9fe;
  color: #5b21b6;
  border: 1px solid #c4b5fd;
}
.tier-item-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  margin-bottom: 0.2rem;
}
.pool-tier {
  margin-bottom: 1rem;
  padding-bottom: 0.75rem;
  border-bottom: 1px solid var(--color-border, #eee);
}
.pool-tier:last-child {
  border-bottom: none;
}
.tier-head {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
}
.tier-rarity {
  font-weight: 600;
}
.tier-prob {
  font-size: 0.85rem;
  color: var(--color-text-secondary, #666);
}
.tier-items {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.tier-item {
  padding: 0.5rem 0.65rem;
  border-radius: 6px;
  background: var(--color-bg, #fff);
  border: 1px solid var(--color-border, #eee);
}
.tier-item-name {
  font-weight: 500;
  overflow-wrap: anywhere;
}
.tier-item-desc {
  font-size: 0.8rem;
  color: var(--color-text-secondary, #666);
  line-height: 1.4;
}
.btn-secondary {
  padding: 0.45rem 1rem;
  border-radius: 6px;
  border: 1px solid var(--color-border, #ccc);
  background: var(--color-bg, #fff);
  color: var(--color-text, #333);
  cursor: pointer;
}
.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
@keyframes wheel-spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes result-reveal {
  from {
    opacity: 0;
    transform: translateY(0.2rem);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (min-width: 900px) and (hover: hover) and (pointer: fine) {
  .spin-station {
    position: relative;
    overflow: hidden;
    padding-top: 1.35rem;
    background:
      radial-gradient(circle at 50% 19%, rgb(139 92 246 / 18%), transparent 36%),
      linear-gradient(155deg, color-mix(in srgb, var(--color-surface, #fafafa) 92%, #312e81 8%), var(--color-surface, #fafafa));
  }
  .spin-station::before {
    position: absolute;
    inset: 0;
    background-image:
      linear-gradient(rgb(255 255 255 / 3%) 1px, transparent 1px),
      linear-gradient(90deg, rgb(255 255 255 / 3%) 1px, transparent 1px);
    background-size: 22px 22px;
    content: '';
    pointer-events: none;
  }
  .station-orbit {
    position: relative;
    width: 13.5rem;
    height: 13.5rem;
    margin: 0 0 0.85rem;
    border: 0;
    background: transparent;
  }
  .station-icon {
    display: none;
  }
  .desktop-wheel-stage {
    position: relative;
    display: grid;
    width: 100%;
    height: 100%;
    place-items: center;
    isolation: isolate;
  }
  .wheel-aura {
    position: absolute;
    width: 82%;
    height: 82%;
    border-radius: 50%;
    background: #7c3aed;
    filter: blur(2.2rem);
    opacity: 0.2;
    transition: opacity 0.3s ease;
  }
  .is-spinning .wheel-aura {
    opacity: 0.52;
    animation: aura-pulse 0.75s ease-in-out infinite alternate;
  }
  .wheel-machine {
    position: relative;
    z-index: 2;
    width: 11.7rem;
    height: 11.7rem;
    padding: 0.72rem;
    border: 0.26rem solid #78350f;
    border-radius: 50%;
    background: linear-gradient(145deg, #fef3c7, #d97706 48%, #78350f);
    box-shadow:
      0 0.8rem 1.5rem rgb(49 46 129 / 28%),
      inset 0 0 0 0.2rem rgb(255 255 255 / 30%);
  }
  .wheel-disc {
    position: relative;
    width: 100%;
    height: 100%;
    overflow: hidden;
    border: 0.22rem solid #fffbeb;
    border-radius: 50%;
    background: var(--wheel-gradient);
    box-shadow: inset 0 0 1.2rem rgb(30 27 75 / 55%);
    transform: rotate(var(--wheel-angle));
    transition: filter 0.25s ease;
  }
  .wheel-disc::after {
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: conic-gradient(from 10deg, transparent, rgb(255 255 255 / 28%), transparent 20%, transparent 75%, rgb(255 255 255 / 16%));
    content: '';
  }
  .is-spinning .wheel-disc {
    transform: rotate(var(--wheel-angle));
    transition: transform 2.8s cubic-bezier(0.08, 0.72, 0.12, 1), filter 0.25s ease;
    filter: saturate(1.22) brightness(1.08);
  }
  .wheel-runes span {
    position: absolute;
    inset: 0;
    padding-top: 0.7rem;
    color: #fff;
    font-size: 1rem;
    text-shadow: 0 1px 4px rgb(30 27 75 / 80%);
  }
  .wheel-runes b {
    display: inline-block;
  }
  .wheel-hub {
    position: absolute;
    z-index: 2;
    top: 50%;
    left: 50%;
    display: grid;
    width: 4.1rem;
    height: 4.1rem;
    place-content: center;
    border: 0.22rem solid #fff7d6;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 28%, #fef3c7, #f59e0b 52%, #92400e);
    box-shadow: 0 0.25rem 0.75rem rgb(30 27 75 / 55%);
    color: #451a03;
    line-height: 1.05;
    transform: translate(-50%, -50%);
  }
  .wheel-hub span {
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  .wheel-hub small {
    margin-top: 0.18rem;
    font-size: 0.58rem;
    font-weight: 700;
  }
  .wheel-pointer {
    position: absolute;
    z-index: 5;
    top: -0.15rem;
    left: 50%;
    width: 2.25rem;
    height: 3.3rem;
    transform: translateX(-50%);
    transform-origin: 50% 0;
  }
  .wheel-pointer span {
    display: block;
    width: 100%;
    height: 100%;
    background: linear-gradient(90deg, #b45309, #fef3c7 45%, #f59e0b 70%, #78350f);
    clip-path: polygon(10% 0, 90% 0, 50% 100%);
    filter: drop-shadow(0 0.2rem 0.15rem rgb(69 26 3 / 55%));
  }
  .is-spinning .wheel-pointer {
    animation: pointer-tick 0.12s ease-in-out infinite alternate;
  }
  .wheel-lights i {
    position: absolute;
    z-index: 4;
    top: 50%;
    left: 50%;
    width: 0.42rem;
    height: 0.42rem;
    margin: -0.21rem;
    border-radius: 50%;
    background: #fff7b2;
    box-shadow: 0 0 0.45rem #fde047;
    transform: rotate(calc(var(--light-index) * 22.5deg)) translateY(-5.48rem);
  }
  .is-spinning .wheel-lights i {
    animation: light-chase 0.8s steps(2, end) infinite;
    animation-delay: calc(var(--light-index) * -0.05s);
  }
  .wheel-sparkles i {
    position: absolute;
    z-index: 1;
    top: 50%;
    left: 50%;
    width: 0.32rem;
    height: 0.32rem;
    border-radius: 50%;
    background: #fde68a;
    opacity: 0;
    transform: rotate(calc(var(--spark-index) * 30deg)) translateY(-5.5rem);
  }
  .is-spinning .wheel-sparkles i {
    animation: sparkle-burst 1.15s ease-out infinite;
    animation-delay: calc(var(--spark-index) * -0.095s);
  }
  .has-result .wheel-machine {
    animation: none;
  }
  .has-result.rarity-rare .wheel-aura {
    background: #2563eb;
    opacity: 0.34;
  }
  .has-result.rarity-epic .wheel-aura {
    background: #7c3aed;
    opacity: 0.32;
  }
  .has-result.rarity-legendary .wheel-aura {
    background: #f59e0b;
    opacity: 0.4;
  }
  .station-copy,
  .lucky-wheel-actions,
  .lucky-wheel-result {
    position: relative;
    z-index: 2;
  }
}

@keyframes pointer-tick {
  to { transform: translateX(-50%) rotate(5deg); }
}
@keyframes aura-pulse {
  to { transform: scale(1.16); }
}
@keyframes light-chase {
  50% { background: #f97316; box-shadow: 0 0 0.65rem #fb923c; }
}
@keyframes sparkle-burst {
  0% { opacity: 0; }
  28% { opacity: 1; }
  100% { opacity: 0; transform: rotate(calc(var(--spark-index) * 30deg)) translateY(-7rem) scale(0); }
}

@media (prefers-reduced-motion: reduce) {
  .station-icon.spinning,
  .desktop-wheel-stage *,
  .lucky-wheel-result {
    animation: none;
  }
}
@media (max-width: 760px) {
  .wheel-dashboard {
    grid-template-columns: 1fr;
  }
  .lucky-wheel-header {
    align-items: flex-start;
  }
  .history-time {
    width: 100%;
    margin-left: 0;
  }
}
</style>
