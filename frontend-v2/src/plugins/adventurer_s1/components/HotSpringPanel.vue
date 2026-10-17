<template>
  <div class="hs-panel">
    <div class="hs-header">
      <div class="hs-steam" aria-hidden="true"><span></span><span></span><span></span></div>
      <h5 class="hs-title">深渊温泉 ♨️</h5>
      <span class="hs-hint">入座扣 200 金币 · 生命 1点/2分钟 · 精力 1点/1分钟 · 泡满或最多 6 小时自动起身</span>
    </div>

    <div v-if="loading && seats.length === 0" class="state-msg">加载中…</div>
    <div v-else-if="seats.length === 0" class="state-msg">暂无座位</div>

    <template v-else>
      <div class="hs-tables">
        <div
          v-for="t in orderedTables"
          :key="t.key"
          class="hs-table-card"
          :class="{ 'hs-table-card-big': t.seats.length > 8 }"
        >
          <div class="hs-table-name">{{ t.label }}</div>
          <div class="hs-seats-grid" :class="{ 'hs-seats-grid-big': t.seats.length > 8 }">
            <div
              v-for="seat in t.seats"
              :key="seat.id"
              class="hs-seat"
              :class="{ 'hs-seat-occupied': !!seat.adventurer, 'hs-seat-mine': seat.adventurer?.is_mine, 'hs-seat-clickable': !seat.adventurer || seat.adventurer?.is_mine }"
              @click="onSeatClick(seat)"
            >
              <template v-if="seat.adventurer">
                <div class="hs-seat-name">{{ seat.adventurer.name }}</div>
                <div v-if="seat.drink" class="hs-seat-drink">{{ seat.drink.icon }} {{ seat.drink.name }}</div>
                <div v-else class="hs-seat-drink hs-seat-drink-none">还没点饮品</div>
                <p
                  v-if="hotSpringSeatSummary(seat)"
                  class="hs-seat-summary"
                  :title="hotSpringSeatSummary(seat)"
                >{{ hotSpringSeatSummary(seat) }}</p>
              </template>
              <template v-else>
                <div class="hs-seat-empty">♨️ 空位</div>
              </template>
            </div>
          </div>
        </div>
      </div>
      <div v-if="mySeatId != null" class="hs-footer">
        <button type="button" class="hs-order-btn" :disabled="acting" @click="openDrinkMenu(mySeat)">🍶 点饮品</button>
        <button type="button" class="hs-leave-btn" :disabled="acting" @click="doLeave(mySeatId)">🚪 起身</button>
      </div>
    </template>

    <Teleport to="body">
      <div v-if="drinkTargetSeat" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="drinkTargetSeat = null">
        <div class="hs-modal s1-mobile-dialog">
          <div class="hs-modal-header s1-mobile-dialog-header">
            <h4>给 {{ drinkTargetSeat.adventurer?.name }} 点一份饮品</h4>
            <button type="button" class="modal-close" @click="drinkTargetSeat = null">×</button>
          </div>
          <p class="hs-modal-hint">饮品免费，入场费已经付过；点饮品只更换池边的饮品和一句小动作文案。</p>
          <div class="hs-pick-list">
            <button
              v-for="drink in store.hotSpringMenu"
              :key="drink.key"
              type="button"
              class="hs-pick-item"
              :disabled="acting"
              @click="doOrderDrink(drink)"
            >
              <span class="hs-pick-main">{{ drink.icon }} {{ drink.name }} <strong>免费</strong></span>
              <small>{{ drink.desc }}</small>
            </button>
          </div>
          <div class="hs-modal-actions s1-mobile-dialog-footer">
            <button type="button" class="hs-leave-btn" :disabled="acting" @click="doLeave(drinkTargetSeat.id)">🚪 起身</button>
            <button type="button" class="hs-order-btn" @click="drinkTargetSeat = null">取消</button>
          </div>
        </div>
      </div>
    </Teleport>

    <S1ConfirmDialog
      v-if="pendingSeat && pendingAdventurer"
      title="确认进入深渊温泉"
      :message="`让「${pendingAdventurer.name}」坐到这个座位？\n将扣除 200 金币入场费，入座后点饮品免费。${pendingGoldText}`"
      confirm-label="确认入座并扣除 200 金币"
      :busy="acting"
      @confirm="confirmSit"
      @cancel="cancelPendingSit"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useAdventurerS1Store, type HotSpringSeat, type HotSpringDrinkMenuItem } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import S1ConfirmDialog from './S1ConfirmDialog.vue'

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const loading = ref(false)
const acting = ref(false)
const drinkTargetSeat = ref<HotSpringSeat | null>(null)
const pendingSeat = ref<HotSpringSeat | null>(null)
const pendingAdventurer = ref<{ id: number; name: string; gold?: number } | null>(null)
const pendingGoldText = computed(() => typeof pendingAdventurer.value?.gold === 'number'
  ? `\n当前金币：${pendingAdventurer.value.gold.toFixed(2)}`
  : '')

const seats = computed(() => store.hotSpringSeats)
const mySeat = computed(() => seats.value.find(s => s.adventurer?.is_mine) ?? null)
const mySeatId = computed(() => mySeat.value?.id ?? null)

function hotSpringSeatSummary(seat: HotSpringSeat) {
  return [seat.drink?.text, seat.chatter].filter(Boolean).join(' · ')
}

const tables = computed(() => {
  const map = new Map<string, { key: string; label: string; seats: HotSpringSeat[] }>()
  for (const s of seats.value) {
    let g = map.get(s.table_key)
    if (!g) {
      g = { key: s.table_key, label: s.table_label || '汤池', seats: [] }
      map.set(s.table_key, g)
    }
    g.seats.push(s)
  }
  return [...map.values()]
})

const orderedTables = computed(() => [...tables.value].sort((a, b) => {
  const aBig = a.seats.length > 8 ? 1 : 0
  const bBig = b.seats.length > 8 ? 1 : 0
  return aBig - bBig || a.seats.length - b.seats.length
}))

async function loadSeats() {
  loading.value = true
  try {
    const res = await store.fetchHotSpringSeats(false)
    if (res?.code !== 200) {
      message.error(res?.message || '加载温泉座位失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载温泉座位失败')
  } finally {
    loading.value = false
  }
}

async function doLeave(seatId: number) {
  if (acting.value) return
  acting.value = true
  try {
    const res = await store.leaveHotSpring(seatId)
    if (res.code === 200) {
      message.success('已起身')
      drinkTargetSeat.value = null
    } else {
      message.error(res.message || '起身失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '起身失败')
  } finally {
    acting.value = false
  }
}

async function openDrinkMenu(seat: HotSpringSeat | null) {
  if (!seat?.adventurer?.is_mine || acting.value) return
  try {
    await store.fetchHotSpringMenu()
    drinkTargetSeat.value = seat
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载饮品单失败')
  }
}

async function doOrderDrink(drink: HotSpringDrinkMenuItem) {
  if (acting.value || !drinkTargetSeat.value) return
  acting.value = true
  try {
    const res = await store.orderHotSpring(drinkTargetSeat.value.id, drink.key)
    if (res.code === 200) {
      message.success(`${drink.icon} ${drink.name} 已上桌`)
      drinkTargetSeat.value = null
    } else {
      message.error(res.message || '点饮品失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '点饮品失败')
  } finally {
    acting.value = false
  }
}

async function onSeatClick(seat: HotSpringSeat) {
  if (acting.value) return
  if (seat.adventurer) {
    if (seat.adventurer.is_mine) await openDrinkMenu(seat)
    return
  }

  // 空位：入座（确保冒险者列表已加载）
  acting.value = true
  try {
    if (store.adventurers.length === 0) {
      await store.fetchAdventurers()
    }
    const myAdv = store.adventurers.find(a => a.status !== 'dead')
    if (!myAdv) {
      message.error('你还没有存活的冒险者')
      return
    }
    pendingSeat.value = seat
    pendingAdventurer.value = { id: myAdv.id, name: myAdv.name, gold: myAdv.gold }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载入座信息失败')
  } finally {
    acting.value = false
  }
}

function cancelPendingSit() {
  if (acting.value) return
  pendingSeat.value = null
  pendingAdventurer.value = null
}

async function confirmSit() {
  if (acting.value || !pendingSeat.value || !pendingAdventurer.value) return
  const seatId = pendingSeat.value.id
  const adventurer = pendingAdventurer.value
  acting.value = true
  try {
    const res = await store.sitHotSpring(seatId, adventurer.id)
    if (res.code === 200) {
      message.success(`${adventurer.name} 已入座（-200 金币）`)
      pendingSeat.value = null
      pendingAdventurer.value = null
    } else {
      message.error(res.message || '入座失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '入座失败')
  } finally {
    acting.value = false
  }
}

onMounted(() => {
  // 每次打开都强制拉取最新座位数据
  store.hotSpringSeats = []
  loadSeats()
  store.fetchHotSpringMenu()
})
</script>

<style scoped>
.hs-panel {
  --spring-water: color-mix(in srgb, #55aeb0 72%, var(--color-primary));
  --spring-warm: color-mix(in srgb, #d08a58 62%, var(--color-warning));
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}

.hs-header {
  position: relative;
  overflow: hidden;
  min-height: 104px;
  padding: 22px 18px 16px;
  border: 1px solid color-mix(in srgb, var(--spring-water) 28%, var(--color-border));
  border-radius: 15px;
  background:
    radial-gradient(ellipse at 50% 115%, color-mix(in srgb, var(--spring-water) 22%, transparent), transparent 54%),
    linear-gradient(145deg, color-mix(in srgb, var(--spring-water) 7%, var(--color-surface)), var(--color-surface));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  text-align: center;
}

.hs-header::after {
  content: '';
  position: absolute;
  right: -8%;
  bottom: -34px;
  left: -8%;
  height: 58px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--spring-water) 12%, transparent);
  box-shadow: inset 0 8px 16px color-mix(in srgb, var(--spring-water) 8%, transparent);
}

.hs-steam {
  position: absolute;
  inset: 0;
  pointer-events: none;
}
.hs-steam span {
  position: absolute;
  bottom: 8px;
  width: 22px;
  height: 52px;
  border-radius: 50%;
  border-left: 2px solid color-mix(in srgb, var(--color-text) 18%, transparent);
  opacity: 0;
  filter: blur(0.4px);
  animation: spring-steam 3.6s ease-in-out infinite;
}
.hs-steam span:nth-child(1) { left: 24%; animation-delay: -0.8s; }
.hs-steam span:nth-child(2) { left: 50%; height: 64px; animation-delay: -2.2s; }
.hs-steam span:nth-child(3) { left: 74%; animation-delay: -1.5s; }
.hs-title,
.hs-hint { position: relative; z-index: 1; }

.hs-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.hs-hint {
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}

.state-msg {
  text-align: center;
  padding: 20px;
  color: var(--color-text-secondary, #888);
}

.hs-table-card {
  background: linear-gradient(155deg, color-mix(in srgb, var(--spring-water) 6%, var(--color-surface)), var(--color-surface));
  border: 1px solid color-mix(in srgb, var(--spring-water) 22%, var(--color-border));
  border-radius: 14px;
  padding: 14px;
}

.hs-table-name {
  font-weight: 600;
  font-size: 14px;
  color: var(--color-text);
  text-align: center;
  margin-bottom: 10px;
}

.hs-seats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  max-width: 400px;
  margin: 0 auto;
}

.hs-seats-grid-big {
  grid-template-columns: repeat(6, 1fr);
  max-width: 640px;
}

.hs-seat {
  background: linear-gradient(160deg, color-mix(in srgb, var(--spring-water) 7%, var(--color-bg)), var(--color-bg));
  background-color: #0d1117;
  background-color: var(--color-bg, #0d1117);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 10px 6px;
  text-align: center;
  min-height: 60px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  transition: border-color 0.18s, transform 0.18s, box-shadow 0.18s;
}

.hs-seat-clickable {
  cursor: pointer;
}

.hs-seat-clickable:hover {
  border-color: var(--spring-water);
  transform: translateY(-2px);
  box-shadow: 0 7px 18px color-mix(in srgb, var(--spring-water) 15%, transparent);
}

.hs-seat-occupied {
  background: var(--color-surface);
}

.hs-seat-mine {
  border-color: var(--spring-water);
  background: color-mix(in srgb, var(--spring-water) 12%, var(--color-surface));
}

.hs-seat-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  line-height: 1.2;
}

.hs-seat-drink {
  margin-top: 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--spring-water);
  line-height: 1.25;
}

.hs-seat-drink-none {
  color: var(--color-text-secondary, #888);
  font-weight: 400;
}

.hs-seat-summary {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--color-text);
  line-height: 1.3;
}

.hs-seat-empty {
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}

.hs-order-btn,
.hs-leave-btn {
  margin-top: 6px;
  padding: 2px 10px;
  font-size: 11px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg, #f5f5f5);
  color: var(--color-text);
  cursor: pointer;
}

.hs-order-btn:hover {
  border-color: var(--color-primary, #4a9);
  background: var(--color-surface);
}

.hs-leave-btn:hover {
  background: var(--color-danger, #e55);
  color: #fff;
  border-color: var(--color-danger, #e55);
}

.hs-order-btn:disabled,
.hs-leave-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.hs-footer {
  display: flex;
  justify-content: flex-start;
  gap: 8px;
  padding-top: 4px;
}

.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(0, 0, 0, 0.45);
}

.hs-modal {
  width: min(560px, 100%);
  max-height: min(720px, calc(100vh - 32px));
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-text);
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.24);
}

.hs-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.hs-modal-header h4 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}

.modal-close {
  width: 28px;
  height: 28px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
  cursor: pointer;
}

.hs-modal-hint {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}

.hs-pick-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  padding-right: 2px;
}

.hs-pick-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
  text-align: left;
  cursor: pointer;
}

.hs-pick-item:hover:not(:disabled) {
  border-color: var(--color-primary, #4a9);
}

.hs-pick-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.hs-pick-main {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  font-weight: 600;
}

.hs-pick-main strong {
  color: var(--color-primary, #4a9);
  font-size: 12px;
}

.hs-pick-item small {
  color: var(--color-text-secondary, #888);
  line-height: 1.35;
}

.hs-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

@keyframes spring-steam {
  0% { opacity: 0; transform: translate(0, 12px) scale(0.78); }
  30% { opacity: 0.42; }
  70% { opacity: 0.18; }
  100% { opacity: 0; transform: translate(10px, -42px) scale(1.18); }
}

/* 桌面端并排放置汤池，并压缩非交互留白，使主要内容保持在一屏内。 */
@media (min-width: 900px) {
  .hs-panel {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .hs-header,
  .state-msg,
  .hs-tables,
  .hs-footer {
    grid-column: 1 / -1;
  }

  .hs-tables {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .hs-header {
    min-height: 78px;
    padding: 15px 18px 12px;
  }
  .hs-table-card {
    width: 100%;
    margin: 0;
    padding: 11px;
    box-sizing: border-box;
  }
  .hs-table-card-big {
    grid-column: 1 / -1;
  }
  .hs-table-name { margin-bottom: 8px; }
  .hs-seats-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
    max-width: none;
  }
  .hs-seats-grid-big {
    grid-template-columns: repeat(6, minmax(0, 1fr));
  }
  .hs-seat {
    height: 92px;
    min-height: 92px;
    padding: 7px 5px;
    box-sizing: border-box;
  }
  .hs-seat-summary {
    min-height: 2.6em;
    display: -webkit-box;
    overflow: hidden;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
  }
}

@media (prefers-reduced-motion: reduce) {
  .hs-steam span { animation: none; display: none; }
  .hs-seat { transition: none; }
}

@media (max-width: 640px) {
  .hs-tables {
    grid-template-columns: 1fr;
  }

  .hs-table-card-big { grid-column: auto; }

  .hs-seats-grid,
  .hs-seats-grid-big {
    grid-template-columns: repeat(2, 1fr);
  }

  .hs-modal {
    max-height: calc(100vh - 24px);
  }
}
</style>
