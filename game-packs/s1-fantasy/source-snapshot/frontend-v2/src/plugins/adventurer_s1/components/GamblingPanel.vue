<template>
  <div class="gambling-panel">
    <div class="hall-top">
      <div class="hall-title-row">
        <h5 class="hall-title">深渊赌坊</h5>
        <span v-if="status?.adventurer" class="wallet-chip">
          {{ status.adventurer.name }} · {{ status.adventurer.gold }} 金
        </span>
      </div>

      <div v-if="loading && !status" class="state-msg">加载中…</div>
      <div v-else-if="!status" class="state-msg">加载失败</div>

      <template v-else>
        <div class="hall-strip">
          <span class="status-pill" :class="status.is_open ? 'open' : 'closed'">
            {{ status.is_open ? '开市' : '收注' }}
          </span>
          <span v-if="status.is_open" class="strip-text">
            {{ status.active_dealers }} 名荷官在班
          </span>
          <span v-else class="strip-text muted">
            需「{{ status.linked_work_name }}」有人打工
          </span>
          <span v-if="status.current_round?.status === 'betting'" class="countdown-chip">
            <span class="countdown-bar" :style="{ width: `${countdownPercent}%` }" />
            <span class="countdown-label">{{ displaySecondsRemaining }}s 开骰</span>
          </span>
        </div>

        <p class="hall-hint">{{ status.rules }}</p>
        <div class="meta-row">
          <span class="meta-chip">注额 {{ status.min_bet }}–{{ status.max_bet }}</span>
          <span class="meta-chip">{{ status.round_seconds }}s / 轮</span>
        </div>
      </template>
    </div>

    <template v-if="status">
      <section class="hall-card table-zone">
        <div class="card-head">
          <h6 class="card-title">赌桌</h6>
          <span class="card-badge">{{ status.seated_count }}/{{ status.seat_count }}</span>
        </div>
        <div class="seats-grid">
          <div
            v-for="seat in status.seats"
            :key="seat.index"
            class="seat-tile"
            :class="{ occupied: !!seat.adventurer, mine: isMySeat(seat), empty: !seat.adventurer }"
          >
            <span class="seat-num">{{ seat.index + 1 }}</span>
            <template v-if="seat.adventurer">
              <span class="seat-player">{{ seat.adventurer.name }}</span>
              <button
                v-if="isMySeat(seat)"
                type="button"
                class="btn-leave"
                :disabled="acting"
                @mousedown.stop
                @click.stop="leaveSeat"
              >
                离座
              </button>
            </template>
            <template v-else-if="status.adventurer && !status.adventurer.is_seated && status.adventurer.can_sit">
              <button
                type="button"
                class="btn-seat"
                :disabled="acting"
                @mousedown.stop
                @click.stop="sitDown(seat.index)"
              >
                入座
              </button>
            </template>
            <span v-else class="seat-vacant">空</span>
          </div>
        </div>
        <p v-if="status.adventurer?.reject_reason && !status.adventurer.is_seated" class="field-hint">
          {{ status.adventurer.reject_reason }}
        </p>
      </section>

      <section v-if="status.is_open && status.dealers.length" class="hall-card dealer-zone">
        <div class="card-head">
          <h6 class="card-title">荷官</h6>
          <span class="card-badge">{{ status.dealers.length }} 在班</span>
        </div>

        <div v-if="canTip" class="tip-tiers" @mousedown.stop>
          <span class="tip-tiers-label">打赏档位</span>
          <button
            v-for="tier in status.tip_tiers"
            :key="tier"
            type="button"
            class="tip-chip"
            :class="{ active: tipAmount === tier }"
            :disabled="tipping || tier > (status.adventurer?.gold ?? 0)"
            @mousedown.stop
            @click.stop="tipAmount = tier"
          >
            {{ tier }}
          </button>
        </div>

        <ul class="dealer-list">
          <li v-for="d in status.dealers" :key="d.id" class="dealer-row">
            <span class="dealer-name">{{ d.name }}</span>
            <span v-if="d.tips_received > 0" class="dealer-tips">本班已收 {{ d.tips_received }}</span>
            <button
              v-if="canTip"
              type="button"
              class="btn-tip"
              :disabled="tipping || d.id === status.adventurer?.id || tipAmount > (status.adventurer?.gold ?? 0)"
              @mousedown.stop
              @click.stop="tipDealer(d.id)"
            >
              打赏 {{ tipAmount }}
            </button>
          </li>
        </ul>
        <p class="field-hint dealer-hint">
          <template v-if="canTip">荷官实收打赏的一半，余下作为赌场抽成。</template>
          <template v-else>入座赌桌后即可打赏在班荷官。</template>
        </p>
      </section>

      <section v-if="status.current_round" class="hall-card">
        <div class="card-head">
          <h6 class="card-title">本轮 · #{{ status.current_round.round_epoch }}</h6>
          <span v-if="status.current_round.status === 'betting'" class="live-tag">收注中</span>
          <span class="card-badge">{{ status.current_round.bet_count }} 注</span>
        </div>
        <div v-if="status.current_round.bets.length === 0" class="state-msg compact">暂无人下注</div>
        <ul v-else class="bet-table">
          <li v-for="(bet, i) in status.current_round.bets" :key="i" class="bet-line">
            <span class="bet-player">{{ bet.adventurer_name }}</span>
            <span class="choice-tag" :class="bet.choice">{{ bet.choice_label }}</span>
            <span class="bet-gold">{{ bet.bet_amount }}</span>
          </li>
        </ul>
      </section>

      <section
        v-if="status.last_round && finishedRoundStatuses.has(status.last_round.status)"
        class="hall-card hall-card-muted"
      >
        <div class="card-head">
          <h6 class="card-title">上轮 · #{{ status.last_round.round_epoch }}</h6>
        </div>

        <div v-if="status.last_round.status === 'cancelled'" class="result-banner cancelled">
          {{ status.last_round.outcome_label }}
        </div>
        <div v-else-if="status.last_round.dice" class="dice-result">
          <div class="dice-group">
            <span v-for="(d, di) in status.last_round.dice" :key="di" class="die">{{ d }}</span>
          </div>
          <div class="dice-meta">
            <span class="dice-sum">= {{ status.last_round.dice_sum }}</span>
            <span class="outcome-tag" :class="status.last_round.outcome || ''">
              {{ status.last_round.outcome_label }}
            </span>
          </div>
        </div>

        <ul v-if="status.last_round.bets.length" class="bet-table compact">
          <li v-for="(bet, i) in status.last_round.bets" :key="i" class="bet-line">
            <span class="bet-player">{{ bet.adventurer_name }}</span>
            <span class="choice-tag" :class="bet.choice">{{ bet.choice_label }}</span>
            <span class="bet-gold muted">{{ bet.bet_amount }}</span>
            <span class="bet-outcome" :class="betResultClass(bet)">
              <template v-if="bet.result_label">{{ bet.result_label }}</template>
              <template v-else-if="bet.refunded">退还</template>
              <template v-else-if="bet.won === true">+{{ bet.payout - bet.bet_amount }}</template>
              <template v-else-if="bet.won === false">-{{ bet.bet_amount }}</template>
            </span>
          </li>
        </ul>
      </section>

      <section
        v-if="status.adventurer?.is_seated && status.is_open && status.current_round?.status === 'betting' && !status.current_round.my_bet"
        class="hall-card bet-slip"
        @mousedown.stop
      >
        <div class="card-head">
          <h6 class="card-title">下注</h6>
          <span class="card-badge">已选 {{ betAmount }} 金</span>
        </div>
        <div class="amount-chips">
          <button
            v-for="opt in betAmountOptions"
            :key="opt"
            type="button"
            class="amount-chip"
            :class="{ active: betAmount === opt }"
            :disabled="acting || opt > status.adventurer!.gold"
            @mousedown.stop
            @click.stop="betAmount = opt"
          >
            {{ opt }}
          </button>
        </div>
        <div class="bet-actions">
          <button
            type="button"
            class="bet-action low"
            :disabled="acting || !canBet"
            @mousedown.stop
            @click.stop="placeBet('low')"
          >
            <span class="action-label">猜小</span>
            <span class="action-range">2 – 6</span>
          </button>
          <button
            type="button"
            class="bet-action high"
            :disabled="acting || !canBet"
            @mousedown.stop
            @click.stop="placeBet('high')"
          >
            <span class="action-label">猜大</span>
            <span class="action-range">8 – 12</span>
          </button>
        </div>
      </section>

      <div v-else-if="status.current_round?.my_bet" class="pending-bet">
        已下注 · {{ status.current_round.my_bet.choice_label }}
        {{ status.current_round.my_bet.bet_amount }} 金，等待开骰
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  useAdventurerS1Store,
  type BuildingInstance,
  type GamblingStatus,
  type GamblingSeat,
  type GamblingBetPublic,
} from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useSocket } from '@/composables/useSocket'
import { useLiveTick } from '../composables/useLiveTick'

const props = defineProps<{ building: BuildingInstance }>()
const emit = defineEmits<{ refreshed: [] }>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const socket = useSocket()
const { now } = useLiveTick()

const loading = ref(true)
const acting = ref(false)
const status = ref<GamblingStatus | null>(null)
const betAmount = ref(10)
const tipping = ref(false)
const tipAmount = ref(2)
let roundEndTimer: ReturnType<typeof setTimeout> | null = null
let roundSnapshotAt = 0
let roundSecondsAtLoad = 0

const GAMBLING_REFRESH_ACTIONS = new Set([
  'gambling_round_settled',
  'gambling_round_cancelled',
  'gambling_bet_placed',
  'gambling_seat_changed',
  'gambling_tip',
])

const finishedRoundStatuses = new Set(['settled', 'cancelled'])

const displaySecondsRemaining = computed(() => {
  const cr = status.value?.current_round
  if (!cr || cr.status !== 'betting') return cr?.seconds_remaining ?? 0
  if (!roundSnapshotAt) return cr.seconds_remaining
  const elapsed = Math.floor((now.value - roundSnapshotAt) / 1000)
  return Math.max(0, roundSecondsAtLoad - elapsed)
})

const countdownPercent = computed(() => {
  const total = status.value?.round_seconds ?? 60
  if (total <= 0) return 0
  return Math.min(100, Math.max(0, (displaySecondsRemaining.value / total) * 100))
})

const canBet = computed(() => {
  if (!status.value?.adventurer) return false
  const bet = betAmount.value
  const s = status.value
  return bet >= s.min_bet && bet <= s.max_bet && bet <= s.adventurer!.gold
})

const canTip = computed(() => {
  const s = status.value
  return !!s && s.is_open && !!s.adventurer?.is_seated && s.dealers.length > 0
})

const betAmountOptions = computed(() => {
  const s = status.value
  if (!s?.adventurer) return []
  const cap = Math.min(s.max_bet, s.adventurer.gold)
  if (cap < s.min_bet) return []

  const raw = [
    s.min_bet,
    10, 20, 50, 100,
    Math.floor(cap / 2),
    cap,
  ]
    .map(v => Math.min(Math.max(Math.floor(v), s.min_bet), cap))
    .filter(v => v >= s.min_bet && v <= cap)

  return [...new Set(raw)].sort((a, b) => a - b)
})

function isMySeat(seat: GamblingSeat): boolean {
  const adv = status.value?.adventurer
  return !!adv && !!seat.adventurer && seat.adventurer.id === adv.id
}

function betResultClass(bet: GamblingBetPublic) {
  if (bet.refunded || bet.result_label === '已退还') return 'refunded'
  if (bet.result_label === '通吃' || bet.won === false) return 'lost'
  if (bet.won === true) return 'won'
  return ''
}

function syncRoundCountdown() {
  if (roundEndTimer) {
    clearTimeout(roundEndTimer)
    roundEndTimer = null
  }
  const cr = status.value?.current_round
  if (cr?.status === 'betting') {
    roundSnapshotAt = Date.now()
    roundSecondsAtLoad = cr.seconds_remaining
    const delayMs = (cr.seconds_remaining + 1.5) * 1000
    roundEndTimer = setTimeout(async () => {
      await loadStatus(true)
    }, delayMs)
  } else {
    roundSnapshotAt = 0
    roundSecondsAtLoad = 0
  }
}

function syncBetAmountSelection() {
  const s = status.value
  if (!s?.adventurer) return
  const opts = betAmountOptions.value
  if (opts.length === 0) return
  if (!opts.includes(betAmount.value)) {
    betAmount.value = opts[0]
  }
}

async function loadStatus(silent = false) {
  if (!silent) loading.value = true
  try {
    const res = await store.fetchGamblingStatus(props.building.id)
    if (res.code === 200 && res.data) {
      status.value = res.data
      syncRoundCountdown()
      syncBetAmountSelection()
      syncTipAmount()
    } else if (!silent) {
      message.error(res.message || '加载赌坊状态失败')
    }
  } finally {
    if (!silent) loading.value = false
  }
}

function onAdventurerChanged(payload: {
  action?: string
  building_instance_id?: number
}) {
  if (payload?.building_instance_id !== props.building.id) return
  if (!payload?.action || !GAMBLING_REFRESH_ACTIONS.has(payload.action)) return
  loadStatus(true)
}

async function sitDown(seatIndex: number) {
  if (!status.value?.adventurer || acting.value) return
  acting.value = true
  try {
    const res = await store.startBuildingUsage(
      props.building.id,
      status.value.adventurer.id,
      { seat_index: seatIndex },
    )
    if (res.code === 200) {
      message.success('已入座')
      emit('refreshed')
      await loadStatus(true)
    } else {
      message.error(res.message || '入座失败')
    }
  } finally {
    acting.value = false
  }
}

async function leaveSeat() {
  if (!status.value?.adventurer || acting.value) return
  acting.value = true
  try {
    const res = await store.completeBuildingUsage(props.building.id, status.value.adventurer.id)
    if (res.code === 200) {
      message.success('已离座')
      emit('refreshed')
      await loadStatus(true)
    } else {
      message.error(res.message || '离座失败')
    }
  } finally {
    acting.value = false
  }
}

async function placeBet(choice: 'high' | 'low') {
  if (!status.value?.adventurer || acting.value) return
  acting.value = true
  try {
    const res = await store.playGambling(
      props.building.id,
      status.value.adventurer.id,
      betAmount.value,
      choice,
    )
    if (res.code === 200 && res.data?.status) {
      status.value = res.data.status
      message.success('下注成功')
      emit('refreshed')
    } else {
      message.error(res.message || '下注失败')
      await loadStatus(true)
    }
  } finally {
    acting.value = false
  }
}

function syncTipAmount() {
  const tiers = status.value?.tip_tiers
  if (!tiers || tiers.length === 0) return
  if (!tiers.includes(tipAmount.value)) {
    tipAmount.value = tiers[0]
  }
}

async function tipDealer(dealerId: number) {
  const adv = status.value?.adventurer
  if (!adv || tipping.value) return
  if (tipAmount.value > adv.gold) {
    message.error('金币不足')
    return
  }
  tipping.value = true
  try {
    const res = await store.tipGambling(props.building.id, adv.id, dealerId, tipAmount.value)
    if (res.code === 200 && res.data?.status) {
      status.value = res.data.status
      syncTipAmount()
      message.success(`打赏成功，荷官实收 ${res.data.dealer_gain} 金`)
      emit('refreshed')
    } else {
      message.error(res.message || '打赏失败')
      await loadStatus(true)
    }
  } finally {
    tipping.value = false
  }
}

onMounted(() => {
  loadStatus()
  socket.on('adventurer_changed', onAdventurerChanged)
})

onUnmounted(() => {
  socket.off('adventurer_changed', onAdventurerChanged)
  if (roundEndTimer) clearTimeout(roundEndTimer)
})
</script>

<style scoped lang="scss">
.gambling-panel {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.hall-top {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.hall-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.hall-title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--color-text);
  letter-spacing: 0.02em;
}

.wallet-chip {
  font-size: 12px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 999px;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-primary) 10%, var(--color-surface));
  border: 1px solid color-mix(in srgb, var(--color-primary) 28%, var(--color-border));
  white-space: nowrap;
}

.hall-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.status-pill {
  font-size: 11px;
  font-weight: 700;
  padding: 3px 10px;
  border-radius: 999px;
  letter-spacing: 0.04em;

  &.open {
    color: var(--color-success, #2ecc71);
    background: color-mix(in srgb, var(--color-success, #2ecc71) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--color-success, #2ecc71) 40%, var(--color-border));
  }

  &.closed {
    color: var(--color-text-secondary);
    background: var(--color-surface);
    border: 1px solid var(--color-border);
  }
}

.strip-text {
  font-size: 12px;
  color: var(--color-text);

  &.muted {
    color: var(--color-text-secondary);
  }
}

.countdown-chip {
  position: relative;
  margin-left: auto;
  overflow: hidden;
  min-width: 88px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
}

.countdown-bar {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  background: color-mix(in srgb, var(--color-primary) 18%, transparent);
  transition: width 1s linear;
  pointer-events: none;
}

.countdown-label {
  position: relative;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text);
}

.hall-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-text-secondary);
}

.meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.meta-chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 6px;
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.hall-card {
  padding: 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;

  &.hall-card-muted {
    opacity: 0.92;
    background: color-mix(in srgb, var(--color-bg) 40%, var(--color-surface));
    background-color: #161b22;
    background-color: var(--color-surface, #161b22);
  }
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.card-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.card-badge {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.live-tag {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-primary) 35%, var(--color-border));
}

.table-zone {
  background: color-mix(in srgb, var(--color-primary) 4%, var(--color-surface));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
}

.dealer-zone {
  background: color-mix(in srgb, var(--color-warning, #e6a817) 4%, var(--color-surface));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
}

.tip-tiers {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.tip-tiers-label {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.tip-chip {
  min-width: 40px;
  padding: 6px 10px;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  cursor: pointer;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  transition: border-color 0.15s, background 0.15s;

  &:hover:not(:disabled) {
    border-color: color-mix(in srgb, var(--color-primary) 50%, var(--color-border));
  }

  &.active {
    background: color-mix(in srgb, var(--color-primary) 14%, var(--color-bg));
    border-color: var(--color-primary);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-primary) 20%, transparent);
  }

  &:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
}

.dealer-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.dealer-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.dealer-name {
  flex: 1;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dealer-tips {
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;

  &::before {
    content: '💰 ';
    font-size: 10px;
  }
}

.btn-tip {
  padding: 5px 12px;
  font-size: 12px;
  font-weight: 600;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-warning, #e6a817) 12%, var(--color-surface));
  border: 1px solid color-mix(in srgb, var(--color-warning, #e6a817) 40%, var(--color-border));

  &:hover:not(:disabled) {
    background: color-mix(in srgb, var(--color-warning, #e6a817) 20%, var(--color-surface));
  }

  &:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
}

.dealer-hint {
  margin-top: 8px;
}

.seats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}

@media (max-width: 480px) {
  .seats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

.seat-tile {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-height: 68px;
  padding: 8px 6px;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: var(--color-bg);
  transition: border-color 0.15s, box-shadow 0.15s;

  &.occupied {
    border-color: color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
  }

  &.mine {
    border-color: var(--color-primary);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-primary) 25%, transparent);
  }

  &.empty {
    border-style: dashed;
    opacity: 0.85;
  }
}

.seat-num {
  font-size: 10px;
  font-weight: 700;
  color: var(--color-text-secondary);
  letter-spacing: 0.06em;
}

.seat-player {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
  text-align: center;
  word-break: break-all;
}

.seat-vacant {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.btn-seat {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  border-radius: 6px;
  cursor: pointer;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-primary) 14%, var(--color-surface));
  border: 1px solid color-mix(in srgb, var(--color-primary) 35%, var(--color-border));

  &:hover:not(:disabled) {
    background: color-mix(in srgb, var(--color-primary) 22%, var(--color-surface));
  }

  &:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
}

.btn-leave {
  margin-top: 2px;
  padding: 3px 10px;
  font-size: 11px;
  font-weight: 600;
  border-radius: 6px;
  cursor: pointer;
  color: var(--color-danger, #dc3545);
  background: color-mix(in srgb, var(--color-danger, #dc3545) 10%, var(--color-bg));
  border: 1px solid color-mix(in srgb, var(--color-danger, #dc3545) 45%, var(--color-border));

  &:hover:not(:disabled) {
    background: color-mix(in srgb, var(--color-danger, #dc3545) 18%, var(--color-bg));
  }

  &:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
}

.field-hint,
.state-msg {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--color-text-secondary);

  &.compact {
    margin: 0;
    padding: 4px 0;
  }
}

.bet-table {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;

  &.compact .bet-line {
    font-size: 12px;
  }
}

.bet-line {
  display: grid;
  grid-template-columns: 1fr auto auto auto;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  font-size: 13px;
}

.bet-player {
  font-weight: 500;
  color: var(--color-text);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.choice-tag {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;

  &.low {
    color: color-mix(in srgb, #5a9e9e 80%, var(--color-text));
    background: color-mix(in srgb, #5a9e9e 14%, transparent);
    border: 1px solid color-mix(in srgb, #5a9e9e 35%, var(--color-border));
  }

  &.high {
    color: color-mix(in srgb, var(--color-primary) 85%, var(--color-text));
    background: color-mix(in srgb, var(--color-primary) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--color-primary) 35%, var(--color-border));
  }
}

.bet-gold {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);

  &.muted {
    color: var(--color-text-secondary);
    font-weight: 500;
  }

  &::before {
    content: '💰 ';
    font-size: 10px;
  }
}

.bet-outcome {
  font-size: 12px;
  font-weight: 600;
  text-align: right;
  min-width: 36px;

  &.won {
    color: var(--color-success, #2ecc71);
  }

  &.lost {
    color: var(--color-text-secondary);
  }

  &.refunded {
    color: var(--color-warning, #e6a817);
  }
}

.dice-result {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 10px 0 12px;
}

.dice-group {
  display: flex;
  gap: 10px;
}

.die {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  font-size: 18px;
  font-weight: 700;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  box-shadow: 0 2px 0 color-mix(in srgb, var(--color-border) 80%, transparent);
}

.dice-meta {
  display: flex;
  align-items: center;
  gap: 10px;
}

.dice-sum {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text);
}

.outcome-tag {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);

  &.low,
  &.high {
    color: var(--color-text);
    background: var(--color-bg);
  }

  &.house {
    color: var(--color-warning, #e6a817);
    border-color: color-mix(in srgb, var(--color-warning, #e6a817) 45%, var(--color-border));
    background: color-mix(in srgb, var(--color-warning, #e6a817) 10%, transparent);
  }
}

.result-banner {
  text-align: center;
  padding: 10px;
  font-size: 13px;
  border-radius: 8px;

  &.cancelled {
    color: var(--color-warning, #e6a817);
    background: color-mix(in srgb, var(--color-warning, #e6a817) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--color-warning, #e6a817) 30%, var(--color-border));
  }
}

.bet-slip {
  border-color: color-mix(in srgb, var(--color-primary) 30%, var(--color-border));
}

.amount-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.amount-chip {
  min-width: 52px;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  cursor: pointer;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  transition: border-color 0.15s, background 0.15s;

  &:hover:not(:disabled) {
    border-color: color-mix(in srgb, var(--color-primary) 50%, var(--color-border));
  }

  &.active {
    color: var(--color-text);
    background: color-mix(in srgb, var(--color-primary) 14%, var(--color-bg));
    border-color: var(--color-primary);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-primary) 20%, transparent);
  }

  &:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
}

.bet-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.bet-action {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 12px 8px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;

  &:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  &.low {
    color: var(--color-text);
    background: color-mix(in srgb, #5a9e9e 8%, var(--color-bg));
    border: 1px solid color-mix(in srgb, #5a9e9e 35%, var(--color-border));

    &:hover:not(:disabled) {
      background: color-mix(in srgb, #5a9e9e 14%, var(--color-bg));
    }
  }

  &.high {
    color: var(--color-text);
    background: color-mix(in srgb, var(--color-primary) 8%, var(--color-bg));
    border: 1px solid color-mix(in srgb, var(--color-primary) 35%, var(--color-border));

    &:hover:not(:disabled) {
      background: color-mix(in srgb, var(--color-primary) 14%, var(--color-bg));
    }
  }
}

.action-label {
  font-size: 14px;
  font-weight: 700;
}

.action-range {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.pending-bet {
  padding: 12px;
  text-align: center;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  border-radius: 10px;
  background: color-mix(in srgb, var(--color-primary) 10%, var(--color-surface));
  border: 1px solid color-mix(in srgb, var(--color-primary) 28%, var(--color-border));
}
@media (max-width: 640px) {
  .seats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .amount-chips { gap: 6px; }
  .amount-chip { flex: 1 1 60px; }
  .bet-line { grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
  .bet-actions { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .bet-action { min-width: 0; }
  .result-banner { font-size: 14px; line-height: 1.7; }
}
</style>
