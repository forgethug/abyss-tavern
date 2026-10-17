<template>
  <section v-if="items.length || hasHistory || errorText" class="team-loot-panel">
    <header class="pool-header">
      <h3>队伍待分配战利品</h3>
      <div class="actions">
        <button type="button" class="btn-secondary btn-sm" :disabled="loading || busy" @click="toggleHistory">
          {{ history ? '查看待分配' : '查看分配记录' }}
        </button>
        <button type="button" class="btn-secondary btn-sm" :disabled="loading || busy" @click="refresh()">刷新</button>
      </div>
    </header>
    <p class="hint">只由该次出发的原队员参与。每份先投票选择竞价或公开掷点；投票平票、无人投票或竞价无人出价时掷点。最高点并列的队员继续重掷。</p>
    <p v-if="errorText" class="pool-error" role="alert">{{ errorText }}</p>
    <p v-if="!items.length && !loading" class="hint">暂无{{ history ? '分配记录' : '待分配战利品' }}。</p>
    <article v-for="lot in items" :key="lot.id" class="loot-card">
      <div class="pool-header">
        <strong :style="{ color: rarityColor(lot.item.rarity) }">{{ lot.name }} ×{{ lot.item.quantity }}</strong>
        <span>{{ statusLabel(lot.status) }}</span>
      </div>
      <p class="hint">{{ lot.party_name || '临时小队' }} · {{ lot.target_dungeon }} · 探险 #{{ lot.expedition_log_id }}</p>
      <ItemDescription :text="lot.item.desc" />
      <p v-if="lot.status === 'voting' || lot.status === 'bidding'" class="hint">{{ lot.status === 'voting' ? '投票' : '竞价' }}截止：{{ formatDateTime(lot.deadline_at!) }}</p>
      <div class="members">
        <span v-for="member in lot.members" :key="member.codename">
          {{ member.name }}<template v-if="member.is_me">（{{ member.codename }} · 我）</template>：{{ member.vote ? modeLabel(member.vote) : '未投票' }}<template v-if="member.has_bid"> · 已出价</template>
        </span>
      </div>
      <label v-if="lot.my_members.length > 1 && (lot.status === 'voting' || lot.status === 'bidding')" class="choose-member">
        本次参与的冒险者
        <select v-model="selectedMembers[lot.id]">
          <option v-for="member in lot.my_members" :key="member.adventurer_id" :value="member.adventurer_id">{{ member.name }}</option>
        </select>
      </label>
      <p v-if="myMember(lot)" class="hint">我的冒险者：{{ myMember(lot)?.name }}（{{ myMember(lot)?.codename }}）</p>
      <p v-if="lot.status === 'voting' || lot.status === 'bidding' || (lot.status === 'claim_pending' && lot.mode === 'auction')" class="hint">竞价成交收取 {{ percent(lot.auction_fee_rate) }} 手续费，其余原队员平分扣费后的金币。只在物品成功交付时收费；撤回出价和掷点分配不收费。</p>
      <div v-if="lot.status === 'voting'" class="actions">
        <button type="button" class="btn-primary btn-sm" :disabled="busy || expired(lot) || !myMember(lot)" @click="vote(lot, 'auction')">{{ myMember(lot)?.vote === 'auction' ? '已选竞价' : '选择竞价' }}</button>
        <button type="button" class="btn-secondary btn-sm" :disabled="busy || expired(lot) || !myMember(lot)" @click="vote(lot, 'roll')">{{ myMember(lot)?.vote === 'roll' ? '已选掷点' : '选择掷点' }}</button>
      </div>
      <form v-if="lot.status === 'bidding'" class="bid-form" @submit.prevent="bid(lot)">
        <p class="hint">金币先托管。截止后最高出价者取得整份物品，其余存活原队员均分扣费后的成交款；最高出价相同则公开掷点决出胜者。未胜出者退回出价。</p>
        <p>我的出价：{{ gold(myMember(lot)?.bid_amount ?? 0) }} 金币 · 托管：{{ gold(myMember(lot)?.held_amount ?? 0) }} 金币</p>
        <p v-if="myMember(lot)?.bid_block_reason" class="pool-error">{{ myMember(lot)?.bid_block_reason }}</p>
        <label>出价（金币）<input v-model="bidAmounts[lot.id]" type="number" min="0" step="0.01" inputmode="decimal" required /></label>
        <div class="actions">
          <button type="submit" class="btn-primary btn-sm" :disabled="busy || expired(lot) || !myMember(lot)?.can_bid">提交出价</button>
          <button type="button" class="btn-secondary btn-sm" :disabled="busy || expired(lot) || !(myMember(lot)?.held_amount)" @click="bid(lot, '0')">撤回出价</button>
        </div>
      </form>
      <details v-if="lot.roll_rounds.length" open class="rolls">
        <summary>公开掷点 · {{ lot.roll_rounds.length }} 轮</summary>
        <div v-for="round in lot.roll_rounds" :key="round.number" class="roll-round">
          <strong>第 {{ round.number }} 轮{{ round.tied ? ' · 最高点并列，继续重掷' : '' }}</strong>
          <span v-for="result in round.results" :key="result.name" :class="{ highest: result.value === round.highest }">{{ result.name }}：{{ result.value }}</span>
        </div>
      </details>
      <p v-if="lot.winner_name" class="winner">获得者：{{ lot.winner_name }}<template v-if="lot.winning_amount"> · 胜出出价 {{ gold(lot.winning_amount) }} 金币</template></p>
      <p v-if="lot.delivery_hint" class="hint">{{ lot.delivery_hint }}</p>
      <button v-if="lot.can_claim" type="button" class="btn-primary btn-sm" :disabled="busy" @click="claim(lot)">领取到仓库</button>
      <div v-if="lot.receipt" class="receipt">
        <p>已送入仓库 · 成交价 {{ gold(lot.receipt.gross_amount ?? lot.receipt.paid_amount) }} 金币 · 手续费 {{ gold(lot.receipt.fee_amount ?? 0) }} 金币 · 实际分款 {{ gold(lot.receipt.paid_amount) }} 金币 · {{ formatDateTime(lot.receipt.claimed_at) }}</p>
        <span v-for="share in lot.receipt.shares" :key="share.name">{{ share.name }} 分得 {{ gold(share.amount) }} 金币</span>
      </div>
    </article>
    <Pagination v-if="pages > 1" :pagination="{ current_page: page, pages, total }" @go="refresh" />
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import Pagination from '@/components/Pagination.vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatDateTime } from '@/utils/time'
import ItemDescription from './ItemDescription.vue'
import { rarityColor } from '../utils'
import { bidTeamLoot, claimTeamLoot, fetchTeamLoot, voteTeamLoot, type TeamLootLot, type TeamLootMode } from '../api/teamLoot'

const message = useGlobalMessage()
const items = ref<TeamLootLot[]>([])
const page = ref(1), pages = ref(0), total = ref(0)
const history = ref(false), hasHistory = ref(false), loading = ref(false), busy = ref(false)
const errorText = ref('')
const bidAmounts = ref<Record<number, string>>({})
const selectedMembers = ref<Record<number, number>>({})
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
let disposed = false
let queuedPage: number | undefined

const statusLabel = (status: TeamLootLot['status']) => ({ voting: '投票中', bidding: '竞价中', rolling: '正在掷点', claim_pending: '待领取', settled: '已分配', unclaimed: '原队伍无人可领取', cancelled: '已撤销登记' }[status])
const modeLabel = (mode: TeamLootMode) => mode === 'auction' ? '竞价' : '掷点'
const gold = (value: number) => value.toFixed(2)
const percent = (rate: number) => `${Number((rate * 100).toFixed(2))}%`
const expired = (lot: TeamLootLot) => !lot.deadline_at || Date.parse(lot.deadline_at) <= now.value
const myMember = (lot: TeamLootLot) => lot.my_members.find(member => member.adventurer_id === selectedMembers.value[lot.id]) ?? lot.my_members[0]

async function refresh(targetPage = page.value) {
  if (disposed) return
  if (loading.value) { queuedPage = targetPage; return }
  loading.value = true
  try {
    const result = await fetchTeamLoot(targetPage, history.value)
    if (disposed) return
    items.value = result.items
    page.value = result.page; pages.value = result.pages; total.value = result.total
    hasHistory.value = result.has_history
    errorText.value = ''
    for (const lot of result.items) {
      selectedMembers.value[lot.id] ??= lot.my_members[0]?.adventurer_id ?? 0
      bidAmounts.value[lot.id] ??= String(myMember(lot)?.bid_amount || '')
    }
  } catch (error) {
    if (!disposed) errorText.value = error instanceof Error ? error.message : '战利品暂时无法读取，请刷新重试'
  } finally {
    loading.value = false
    if (!disposed && queuedPage !== undefined) {
      const nextPage = queuedPage
      queuedPage = undefined
      void refresh(nextPage)
    }
  }
}
defineExpose({ refresh })

async function run(action: () => Promise<TeamLootLot>, success: string) {
  if (busy.value) return
  busy.value = true
  try { await action(); message.success(success); await refresh() }
  catch (error) { message.error(error instanceof Error ? error.message : '操作暂时未完成，请重试') }
  finally { busy.value = false }
}
function vote(lot: TeamLootLot, mode: TeamLootMode) {
  const member = myMember(lot)
  if (member) void run(() => voteTeamLoot(lot.id, mode, member.adventurer_id), '投票已记录')
}
function bid(lot: TeamLootLot, amount = bidAmounts.value[lot.id] || '') {
  const member = myMember(lot)
  if (member) void run(() => bidTeamLoot(lot.id, amount, member.adventurer_id), amount === '0' ? '出价已撤回' : '出价已托管')
}
function claim(lot: TeamLootLot) { void run(() => claimTeamLoot(lot.id), '物品已送入仓库') }
function toggleHistory() { history.value = !history.value; void refresh(1) }
function onVisible() { if (document.visibilityState === 'visible') void refresh() }
onMounted(() => {
  void refresh()
  document.addEventListener('visibilitychange', onVisible)
  timer = setInterval(() => {
    now.value = Date.now()
    if (document.visibilityState === 'visible' && !busy.value && items.value.some(lot => ['voting', 'bidding', 'rolling', 'claim_pending'].includes(lot.status))) void refresh()
  }, 30_000)
})
onUnmounted(() => { disposed = true; if (timer) clearInterval(timer); document.removeEventListener('visibilitychange', onVisible) })
</script>

<style scoped>
.team-loot-panel { margin-bottom: 24px; padding: 16px; border: 1px solid var(--border-color, #555); border-radius: 10px; }
.pool-header, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; justify-content: space-between; }
.pool-header h3 { margin: 0; }
.hint { opacity: .75; font-size: 13px; line-height: 1.7; }
.loot-card { margin-top: 16px; padding: 14px; border: 1px solid var(--border-color, #555); border-radius: 8px; overflow-wrap: anywhere; }
.members, .roll-round, .receipt { display: flex; flex-wrap: wrap; gap: 8px 14px; margin: 10px 0; }
.members span, .roll-round span, .receipt span { padding: 3px 6px; background: rgba(128, 128, 128, .12); border-radius: 4px; }
.roll-round strong { flex-basis: 100%; }
.highest, .winner { color: var(--primary-color, #b69856); font-weight: 600; }
.choose-member, .bid-form label { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 12px 0; }
.bid-form input, .choose-member select { max-width: 100%; padding: 8px; border-radius: 4px; color: inherit; background: var(--card-color, #27272a); border: 1px solid var(--border-color, #555); }
.bid-form .actions { justify-content: flex-start; }
.pool-error { color: #e37b7b; }
button { min-height: 36px; }
</style>
