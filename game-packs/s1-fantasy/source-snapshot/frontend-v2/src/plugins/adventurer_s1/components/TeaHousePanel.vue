<template>
  <div class="tea-house-panel">
    <div class="tea-header">
      <div class="tea-header-copy">
        <span class="tea-eyebrow">ABYSS OUTPOST · 茶歇处</span>
        <h5 class="tea-title"><span class="tea-title-mark">茶</span> 深渊驻地茶馆</h5>
        <span class="tea-hint">卸下行囊，围桌歇脚。这里永远为归来的冒险者留一盏热茶。</span>
      </div>
      <div class="tea-rules" aria-label="茶舍规则">
        <span><b>{{ teaHouseEntryFee }}</b> 金币入座</span>
        <span><b>{{ store.teaHouseLowConsumptionGold.toFixed(2) }}</b> 低消费临时金</span>
        <span><b>+2</b> 精力 / 分钟</span>
        <span><b>6h</b> 最长茶歇</span>
      </div>
    </div>

    <div v-if="loading && seats.length === 0" class="state-msg">加载中…</div>
    <div v-else-if="seats.length === 0" class="state-msg">暂无座位</div>

    <template v-else>
      <div v-for="t in tables" :key="t.key" class="tea-table-card">
        <div class="tea-table-name">
          <span class="tea-table-ornament">◆</span>
          <span>{{ t.label }}</span>
          <span v-if="t.locked" class="tea-table-lock">🔒 需 Lv2</span>
          <span v-else class="tea-table-count">{{ t.seats.filter(seat => !!seat.adventurer).length }}/{{ t.seats.length }} 人</span>
        </div>
        <div class="tea-seats-grid">
          <div
            v-for="seat in t.seats"
            :key="seat.id"
            class="tea-seat"
            :class="{ 'tea-seat-occupied': !!seat.adventurer, 'tea-seat-mine': seat.adventurer?.is_mine, 'tea-seat-clickable': !seat.locked && (!seat.adventurer || seat.adventurer?.is_mine), 'tea-seat-locked': seat.locked }"
            @click="onSeatClick(seat)"
          >
            <template v-if="seat.locked">
              <div class="tea-seat-lock-label">🔒</div>
              <div class="tea-seat-empty">需 Lv2</div>
            </template>
            <template v-else-if="seat.adventurer">
              <div class="tea-seat-avatar">{{ seat.adventurer.name.slice(0, 1) }}</div>
              <div class="tea-seat-name">{{ seat.adventurer.name }}</div>
              <div v-if="seat.tea" class="tea-seat-drink">{{ seat.tea.icon }} {{ seat.tea.name }}</div>
              <div v-else class="tea-seat-drink tea-seat-drink-none">还没点茶</div>
              <p
                v-if="teaSeatSummary(seat)"
                class="tea-seat-summary"
                :title="teaSeatSummary(seat)"
              >{{ teaSeatSummary(seat) }}</p>
            </template>
            <template v-else>
              <div class="tea-seat-empty-icon">＋</div>
              <div class="tea-seat-empty">坐下歇脚</div>
            </template>
          </div>
        </div>
      </div>
      <div v-if="mySeatId != null" class="tea-footer">
        <span class="tea-footer-note">座位已经暖好，给今天的冒险选一盏收尾茶吧。</span>
        <button type="button" class="tea-order-btn" :disabled="acting" @click="openTeaMenu(mySeat)">🍵 查看茶单</button>
        <button type="button" class="tea-leave-btn" :disabled="acting" @click="doLeave(mySeatId)">🚪 离座</button>
      </div>
    </template>

    <Teleport to="body">
      <div v-if="teaTargetSeat" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="teaTargetSeat = null">
        <div class="tea-modal s1-mobile-dialog">
          <div class="tea-modal-header s1-mobile-dialog-header">
            <h4>给 {{ teaTargetSeat.adventurer?.name }} 点一杯茶</h4>
            <button type="button" class="modal-close" @click="teaTargetSeat = null">×</button>
          </div>
          <p class="tea-modal-hint">茶品免费，入场费已经付过；点茶只更换桌上的茶和一句小动作文案。</p>
          <div class="tea-pick-list">
            <button
              v-for="tea in store.teaHouseMenu"
              :key="tea.key"
              type="button"
              class="tea-pick-item"
              :disabled="acting"
              @click="doOrderTea(tea)"
            >
              <span class="tea-pick-main">{{ tea.icon }} {{ tea.name }} <strong>免费</strong></span>
              <small>{{ tea.desc }}</small>
            </button>
          </div>
          <div class="tea-modal-actions s1-mobile-dialog-footer">
            <button type="button" class="tea-leave-btn" :disabled="acting" @click="doLeave(teaTargetSeat.id)">🚪 离座</button>
            <button type="button" class="tea-order-btn" @click="teaTargetSeat = null">取消</button>
          </div>
        </div>
      </div>
    </Teleport>

    <S1ConfirmDialog
      v-if="pendingSeat && pendingAdventurer"
      title="确认入座深渊茶舍"
      :message="`让「${pendingAdventurer.name}」坐到这个座位？\n入场费 ${teaHouseEntryFee} 金币，先扣非负金币、再用低消费临时金补足；入座后点茶免费。${pendingGoldText}`"
      :confirm-label="`确认入座并支付 ${teaHouseEntryFee}`"
      :busy="acting"
      @confirm="confirmSit"
      @cancel="cancelPendingSit"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useAdventurerS1Store, type TeaHouseSeat, type TeaHouseTeaMenuItem } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import S1ConfirmDialog from './S1ConfirmDialog.vue'

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const loading = ref(false)
const acting = ref(false)
const teaTargetSeat = ref<TeaHouseSeat | null>(null)
const pendingSeat = ref<TeaHouseSeat | null>(null)
const pendingAdventurer = ref<{
  id: number
  name: string
  gold?: number
  lowConsumptionGold?: number
} | null>(null)
const teaHouseEntryFee = computed(() => store.teaHouseEntryFee)
const pendingGoldText = computed(() => typeof pendingAdventurer.value?.gold === 'number'
  ? `\n当前金币：${pendingAdventurer.value.gold.toFixed(2)}；低消费临时金：${(pendingAdventurer.value.lowConsumptionGold ?? 0).toFixed(2)}`
  : '')

const seats = computed(() => store.teaHouseSeats)
const mySeat = computed(() => seats.value.find(s => s.adventurer?.is_mine) ?? null)
const mySeatId = computed(() => mySeat.value?.id ?? null)

function teaSeatSummary(seat: TeaHouseSeat) {
  return [seat.tea?.text, seat.chatter].filter(Boolean).join(' · ')
}

const tables = computed(() => {
  const map = new Map<string, { key: string; label: string; locked: boolean; seats: TeaHouseSeat[] }>()
  for (const s of seats.value) {
    let g = map.get(s.table_key)
    if (!g) {
      g = { key: s.table_key, label: s.table_label || '茶桌', locked: !!s.locked, seats: [] }
      map.set(s.table_key, g)
    }
    g.seats.push(s)
  }
  return [...map.values()]
})

async function loadSeats() {
  loading.value = true
  try {
    const res = await store.fetchTeaHouseSeats(false)
    console.log('[TeaHouse] fetchSeats response:', res)
    if (res?.code !== 200) {
      message.error(res?.message || '加载茶舍座位失败')
    }
  } catch (e: any) {
    console.error('[TeaHouse] fetchSeats error:', e)
    message.error(e?.response?.data?.message || '加载茶舍座位失败')
  } finally {
    loading.value = false
  }
}

async function doLeave(seatId: number) {
  if (acting.value) return
  acting.value = true
  try {
    const res = await store.leaveTeaHouse(seatId)
    if (res.code === 200) {
      message.success('已离座')
      teaTargetSeat.value = null
    } else {
      message.error(res.message || '离座失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '离座失败')
  } finally {
    acting.value = false
  }
}

async function openTeaMenu(seat: TeaHouseSeat | null) {
  if (!seat?.adventurer?.is_mine || acting.value) return
  try {
    await store.fetchTeaHouseMenu()
    teaTargetSeat.value = seat
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载茶单失败')
  }
}

async function doOrderTea(tea: TeaHouseTeaMenuItem) {
  if (acting.value || !teaTargetSeat.value) return
  acting.value = true
  try {
    const res = await store.orderTeaHouse(teaTargetSeat.value.id, tea.key)
    if (res.code === 200) {
      message.success(`${tea.icon} ${tea.name} 已上桌`)
      teaTargetSeat.value = null
    } else {
      message.error(res.message || '点茶失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '点茶失败')
  } finally {
    acting.value = false
  }
}

async function onSeatClick(seat: TeaHouseSeat) {
  if (acting.value) return
  if (seat.locked) {
    message.info('这桌需茶舍升至 Lv2 后开放')
    return
  }
  if (seat.adventurer) {
    if (seat.adventurer.is_mine) await openTeaMenu(seat)
    return
  }

  // 空位：入座（确保冒险者列表已加载）
  acting.value = true
  try {
    if (store.adventurers.length === 0) {
      await store.fetchAdventurers()
    }
    const myAdv = store.adventurers.find(a => a.status !== 'dead' && a.is_player_created)
    if (!myAdv) {
      message.error('你还没有存活的冒险者')
      return
    }
    pendingSeat.value = seat
    pendingAdventurer.value = {
      id: myAdv.id,
      name: myAdv.name,
      gold: myAdv.gold,
      lowConsumptionGold: store.teaHouseLowConsumptionGold,
    }
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
    const res = await store.sitTeaHouse(seatId, adventurer.id)
    if (res.code === 200) {
      const temporarySpent = Number(res.data?.low_consumption_spent ?? 0)
      const goldSpent = Number(res.data?.gold_spent ?? 0)
      const paidText = temporarySpent > 0
        ? `金币 ${goldSpent.toFixed(2)} + 临时金 ${temporarySpent.toFixed(2)}`
        : `金币 ${goldSpent.toFixed(2)}`
      message.success(`${adventurer.name} 已入座（${paidText}）`)
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
  store.teaHouseSeats = []
  loadSeats()
  store.fetchTeaHouseMenu()
})
</script>

<style scoped>
.tea-house-panel {
  --tea-accent: #a97838;
  --tea-accent-soft: color-mix(in srgb, var(--tea-accent) 12%, var(--color-surface));
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.tea-header {
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  padding: 22px 24px;
  border: 1px solid color-mix(in srgb, var(--tea-accent) 35%, var(--color-border));
  border-radius: 16px;
  background:
    radial-gradient(circle at 90% 10%, color-mix(in srgb, var(--tea-accent) 18%, transparent), transparent 42%),
    linear-gradient(135deg, var(--tea-accent-soft), var(--color-surface) 62%);
}

.tea-header::after {
  content: '茶';
  position: absolute;
  right: 18px;
  top: -34px;
  font-family: serif;
  font-size: 118px;
  line-height: 1;
  color: color-mix(in srgb, var(--tea-accent) 7%, transparent);
  pointer-events: none;
}

.tea-header-copy {
  position: relative;
  z-index: 1;
  min-width: 0;
}

.tea-eyebrow {
  display: block;
  margin-bottom: 7px;
  color: var(--tea-accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.18em;
}

.tea-title {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 0 0 6px;
  font-family: ui-serif, Georgia, serif;
  font-size: 23px;
  font-weight: 700;
  color: var(--color-text);
}

.tea-title-mark {
  display: inline-grid;
  width: 31px;
  height: 31px;
  place-items: center;
  border-radius: 9px;
  background: var(--tea-accent);
  color: #fffaf0;
  font-size: 17px;
}

.tea-hint {
  display: block;
  font-size: 13px;
  color: var(--color-text-secondary, #888);
}

.tea-rules {
  position: relative;
  z-index: 1;
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 7px;
}

.tea-rules span {
  padding: 7px 10px;
  border: 1px solid color-mix(in srgb, var(--tea-accent) 25%, var(--color-border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-surface) 82%, transparent);
  color: var(--color-text-secondary, #888);
  font-size: 11px;
  white-space: nowrap;
}

.tea-rules b { color: var(--tea-accent); }

.state-msg {
  text-align: center;
  padding: 20px;
  color: var(--color-text-secondary, #888);
}

.tea-table-card {
  position: relative;
  background: linear-gradient(180deg, var(--color-surface), color-mix(in srgb, var(--tea-accent) 4%, var(--color-surface)));
  border: 1px solid color-mix(in srgb, var(--tea-accent) 20%, var(--color-border));
  border-radius: 16px;
  padding: 16px;
  box-shadow: 0 10px 28px color-mix(in srgb, var(--tea-accent) 7%, transparent);
}

.tea-table-name {
  font-weight: 600;
  font-size: 14px;
  color: var(--color-text);
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
}

.tea-table-ornament { color: var(--tea-accent); font-size: 9px; }

.tea-table-count {
  margin-left: auto;
  color: var(--color-text-secondary, #888);
  font-size: 11px;
  font-weight: 500;
}

.tea-seats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  max-width: 560px;
  margin: 0 auto;
}

.tea-seat {
  position: relative;
  overflow: hidden;
  background: color-mix(in srgb, var(--color-bg, #f5f5f5) 88%, var(--tea-accent));
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 13px 8px;
  text-align: center;
  min-height: 112px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}

.tea-seat-clickable {
  cursor: pointer;
}

.tea-seat-clickable:hover {
  transform: translateY(-2px);
  border-color: var(--tea-accent);
  box-shadow: 0 8px 20px color-mix(in srgb, var(--tea-accent) 13%, transparent);
}

.tea-seat-occupied {
  background: var(--color-surface);
}

.tea-seat-mine {
  border-color: var(--tea-accent);
  background: var(--tea-accent-soft);
}

.tea-seat-mine::after {
  content: '我的座位';
  position: absolute;
  top: 0;
  right: 0;
  padding: 3px 6px;
  border-radius: 0 7px 0 7px;
  background: var(--tea-accent);
  color: #fff;
  font-size: 9px;
}

.tea-seat-avatar {
  display: grid;
  width: 29px;
  height: 29px;
  margin-bottom: 6px;
  place-items: center;
  border: 1px solid color-mix(in srgb, var(--tea-accent) 35%, var(--color-border));
  border-radius: 50%;
  background: var(--tea-accent-soft);
  color: var(--tea-accent);
  font-family: ui-serif, Georgia, serif;
  font-weight: 700;
}

.tea-seat-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  line-height: 1.2;
}

.tea-seat-drink {
  margin-top: 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary, #4a9);
  line-height: 1.25;
}

.tea-seat-drink-none {
  color: var(--color-text-secondary, #888);
  font-weight: 400;
}

.tea-seat-summary {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--color-text);
  line-height: 1.3;
}

.tea-seat-empty {
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}

.tea-seat-empty-icon {
  display: grid;
  width: 28px;
  height: 28px;
  margin-bottom: 6px;
  place-items: center;
  border: 1px dashed color-mix(in srgb, var(--tea-accent) 55%, var(--color-border));
  border-radius: 50%;
  color: var(--tea-accent);
  font-size: 17px;
}

.tea-seat-locked {
  border-style: dashed;
  opacity: 0.6;
  cursor: not-allowed;
}

.tea-seat-lock-label {
  font-size: 16px;
  line-height: 1;
}

.tea-table-lock {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 500;
  color: var(--color-text-secondary, #888);
}

.tea-order-btn,
.tea-leave-btn {
  padding: 8px 13px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 9px;
  background: var(--color-bg, #f5f5f5);
  color: var(--color-text);
  cursor: pointer;
}

.tea-order-btn:hover {
  border-color: var(--tea-accent);
  background: var(--color-surface);
}

.tea-leave-btn:hover {
  background: var(--color-danger, #e55);
  color: #fff;
  border-color: var(--color-danger, #e55);
}

.tea-order-btn:disabled,
.tea-leave-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.tea-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--tea-accent) 20%, var(--color-border));
  border-radius: 12px;
  background: var(--tea-accent-soft);
}

.tea-footer-note {
  margin-right: auto;
  color: var(--color-text-secondary, #888);
  font-size: 12px;
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

.tea-modal {
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

.tea-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.tea-modal-header h4 {
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

.tea-modal-hint {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}

.tea-pick-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  padding-right: 2px;
}

.tea-pick-item {
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

.tea-pick-item:hover:not(:disabled) {
  border-color: var(--tea-accent);
  transform: translateX(3px);
}

.tea-pick-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.tea-pick-main {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  font-weight: 600;
}

.tea-pick-main strong {
  color: var(--color-primary, #4a9);
  font-size: 12px;
}

.tea-pick-item small {
  color: var(--color-text-secondary, #888);
  line-height: 1.35;
}

.tea-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

/* 桌面端将茶桌并排展示，避免两张桌子纵向堆叠超出一个视口。 */
@media (min-width: 900px) {
  .tea-house-panel {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .tea-header,
  .state-msg,
  .tea-footer {
    grid-column: 1 / -1;
  }

  .tea-header { padding: 16px 20px; }
  .tea-table-card { padding: 12px; }
  .tea-table-name { margin-bottom: 10px; }
  .tea-seats-grid { gap: 6px; }
  .tea-seat {
    height: 112px;
    min-height: 112px;
    padding: 9px 6px;
    box-sizing: border-box;
  }
  .tea-seat-avatar {
    width: 25px;
    height: 25px;
    margin-bottom: 4px;
  }
  .tea-seat-summary {
    min-height: 2.6em;
    display: -webkit-box;
    overflow: hidden;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
  }
}

/* 驻地茶馆：颜色全部由全局主题驱动，白天与黑夜保持同一套温暖木铜质感。 */
.tea-house-panel {
  --tea-accent: color-mix(in srgb, #5d87b3 76%, var(--color-primary));
  --tea-accent-hot: color-mix(in srgb, #82add3 82%, var(--color-text));
  --tea-text: var(--color-text);
  --tea-muted: var(--color-text-secondary, #888);
  --tea-accent-soft: color-mix(in srgb, var(--tea-accent) 10%, var(--color-surface));
  border-color: color-mix(in srgb, var(--tea-accent) 25%, var(--color-border));
  background:
    radial-gradient(circle at 8% 0%, color-mix(in srgb, var(--tea-accent) 10%, transparent), transparent 33%),
    linear-gradient(145deg, var(--color-surface), color-mix(in srgb, var(--color-bg) 94%, var(--tea-accent)));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 5%, transparent), 0 14px 36px rgba(0, 0, 0, 0.1);
}

.tea-house-panel::before {
  opacity: 0.28;
  background-image:
    linear-gradient(color-mix(in srgb, var(--tea-accent) 5%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, var(--tea-accent) 5%, transparent) 1px, transparent 1px);
  background-size: 36px 36px;
}

.tea-header {
  border-color: color-mix(in srgb, var(--tea-accent) 32%, var(--color-border));
  background:
    radial-gradient(circle at 88% 16%, color-mix(in srgb, var(--tea-accent) 15%, transparent), transparent 35%),
    linear-gradient(135deg, color-mix(in srgb, var(--tea-accent) 10%, var(--color-surface)), var(--color-surface) 68%);
  box-shadow: inset 0 0 28px color-mix(in srgb, var(--tea-accent) 5%, transparent), 0 10px 24px rgba(0, 0, 0, 0.08);
}

.tea-header::after {
  content: '茶';
  color: color-mix(in srgb, var(--tea-accent) 7%, transparent);
  text-shadow: none;
}

.tea-title {
  color: var(--color-text);
  text-shadow: none;
}

.tea-hint,
.tea-table-count,
.tea-seat-drink-none,
.tea-seat-summary,
.tea-seat-empty,
.tea-table-lock,
.tea-footer-note,
.state-msg { color: var(--color-text-secondary, #888); }

.tea-title-mark {
  border-color: color-mix(in srgb, var(--tea-accent) 55%, var(--color-border));
  border-radius: 8px;
  background: linear-gradient(145deg, #7ca8cf, color-mix(in srgb, var(--tea-accent) 70%, #29445f));
  color: #fffaf0;
  box-shadow: 0 5px 14px color-mix(in srgb, var(--tea-accent) 22%, transparent);
}

.tea-rules span {
  border-color: color-mix(in srgb, var(--tea-accent) 23%, var(--color-border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-surface) 88%, transparent);
  color: var(--color-text-secondary, #888);
}

.tea-rules b { color: var(--tea-accent); text-shadow: none; }

.tea-table-card {
  background:
    linear-gradient(100deg, color-mix(in srgb, var(--tea-accent) 5%, transparent), transparent 42%),
    var(--color-surface);
  border-color: color-mix(in srgb, var(--tea-accent) 22%, var(--color-border));
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent), 0 10px 24px rgba(0, 0, 0, 0.08);
}

.tea-table-card::before {
  border-color: color-mix(in srgb, var(--tea-accent) 42%, transparent);
}

.tea-table-card::after {
  border-color: color-mix(in srgb, var(--tea-accent) 22%, transparent);
}

.tea-table-name,
.tea-seat-name { color: var(--color-text); }
.tea-table-ornament { color: var(--tea-accent); font-size: 9px; text-shadow: none; }

.tea-seat,
.tea-seat-occupied {
  background: linear-gradient(155deg, color-mix(in srgb, var(--tea-accent) 5%, var(--color-bg)), var(--color-bg));
  border-color: var(--color-border);
}

.tea-seat-clickable:hover {
  border-color: var(--tea-accent);
  box-shadow: 0 8px 20px color-mix(in srgb, var(--tea-accent) 14%, transparent);
}

.tea-seat-mine {
  background: linear-gradient(155deg, color-mix(in srgb, var(--tea-accent) 14%, var(--color-surface)), var(--color-surface));
  box-shadow: inset 0 0 20px color-mix(in srgb, var(--tea-accent) 6%, transparent);
}

.tea-seat-mine::after {
  background: var(--tea-accent);
  color: #fff;
}

.tea-seat-avatar {
  border-color: color-mix(in srgb, var(--tea-accent) 42%, var(--color-border));
  background: var(--tea-accent-soft);
  color: var(--tea-accent);
  box-shadow: none;
}

.tea-seat-drink { color: var(--tea-accent); }
.tea-seat-summary { color: var(--color-text); }
.tea-seat-empty-icon {
  border-color: color-mix(in srgb, var(--tea-accent) 50%, var(--color-border));
  color: var(--tea-accent);
  text-shadow: none;
}

.tea-seat-locked { filter: none; }
.tea-order-btn,
.tea-leave-btn {
  border-color: var(--color-border);
  background: var(--color-bg);
  color: var(--color-text);
}

.tea-order-btn:hover {
  border-color: var(--tea-accent);
  background: var(--tea-accent-soft);
  color: var(--tea-accent);
}

.tea-leave-btn:hover {
  background: var(--color-danger, #e55);
  border-color: var(--color-danger, #e55);
}

.tea-footer {
  border-color: color-mix(in srgb, var(--tea-accent) 22%, var(--color-border));
  background: linear-gradient(90deg, var(--tea-accent-soft), color-mix(in srgb, var(--color-surface) 92%, transparent));
}

.tea-modal {
  border-color: color-mix(in srgb, #5d87b3 30%, var(--color-border));
  background:
    radial-gradient(circle at 100% 0%, color-mix(in srgb, #5d87b3 9%, transparent), transparent 35%),
    var(--color-surface);
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  color: var(--color-text);
  box-shadow: 0 20px 56px rgba(0, 0, 0, 0.25);
}

.tea-modal-hint,
.tea-pick-item small { color: var(--color-text-secondary, #888); }
.tea-pick-item {
  border-color: var(--color-border);
  background: var(--color-bg);
  color: var(--color-text);
}
.tea-pick-item:hover:not(:disabled) {
  border-color: color-mix(in srgb, #5d87b3 76%, var(--color-primary));
  box-shadow: 0 6px 18px color-mix(in srgb, #5d87b3 10%, transparent);
}
.tea-pick-main strong { color: color-mix(in srgb, #5d87b3 76%, var(--color-primary)); }
.modal-close { border-color: var(--color-border); background: var(--color-bg); color: var(--color-text); }

@media (max-width: 640px) {
  .tea-header { align-items: flex-start; flex-direction: column; padding: 18px; }
  .tea-rules { justify-content: flex-start; }
  .tea-seats-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .tea-footer { align-items: stretch; flex-wrap: wrap; }
  .tea-footer-note { width: 100%; margin-bottom: 2px; }

  .tea-modal {
    max-height: calc(100vh - 24px);
  }
}
</style>
