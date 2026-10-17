<template>
  <div class="buy-market-overlay s1-mobile-surface s1-mobile-overlay" @click.self="emit('close')">
    <section class="buy-market-card s1-mobile-dialog" role="dialog" aria-modal="true" aria-label="求购市场">
      <header class="panel-head s1-mobile-dialog-header">
        <div>
          <h3>🪙 求购市场</h3>
          <p>求购者按稀有度定价，其他玩家展示物品，由求购者选定后直接成交。</p>
        </div>
        <button type="button" class="close-btn" aria-label="关闭" @click="emit('close')">✕</button>
      </header>

      <div class="panel-body s1-mobile-dialog-body">
        <div class="toolbar">
          <div class="search-wrap">
            <input
              v-model="searchQuery"
              type="search"
              maxlength="50"
              placeholder="搜索求购说明或求购者…"
              @input="debouncedSearch"
            >
            <button v-if="activeSearch" type="button" @click="clearSearch">清除</button>
          </div>
          <div class="rarity-filters">
            <button
              v-for="entry in rarityFilters"
              :key="entry.value"
              type="button"
              :class="{ active: rarityFilter === entry.value }"
              @click="setRarity(entry.value)"
            >{{ entry.label }}</button>
          </div>
          <div class="actions">
            <button type="button" @click="openMyOffers">我的展示</button>
            <button type="button" class="with-badge" @click="openMyOrders">
              我的求购
              <span v-if="store.marketBuyOrderPendingOfferCount" class="badge">
                {{ store.marketBuyOrderPendingOfferCount > 99 ? '99+' : store.marketBuyOrderPendingOfferCount }}
              </span>
            </button>
            <button type="button" class="primary" :disabled="!canTrade" @click="openCreate">
              + 发布求购
            </button>
          </div>
        </div>

        <p class="rule-hint">
          价格为单件价，必须在现有卖单对应稀有度区间内；成交数量按展示数量计算。
          发布时可选择持续求购，让每批成交后订单与未选展示继续开放，直到手动取消。
          展示物品仍留在仓库，可同时展示给多人；成交后，只有数量超过剩余库存的其他展示会自动撤下。
          展示者承担与卖单相同的 {{ Math.round(store.marketSellFeeRate * 100) }}% 手续费。
        </p>

        <div v-if="store.marketBuyOrdersLoading && !store.marketBuyOrders.length" class="empty">加载中…</div>
        <div v-else-if="!store.marketBuyOrders.length" class="empty">
          {{ activeSearch ? `没有与「${activeSearch}」匹配的求购单` : '暂无进行中的求购单' }}
        </div>
        <div v-else class="order-grid">
          <article v-for="order in store.marketBuyOrders" :key="order.id" class="order-card">
            <div class="order-head">
              <strong>{{ order.buyer_name || '未知求购者' }}</strong>
              <div class="order-flags">
                <span v-if="order.keep_open" class="mode-badge">∞ 持续求购</span>
                <span v-if="isMineOrder(order) && order.offers_count" class="offer-alert">
                  🔔 {{ order.offers_count }} 件待选
                </span>
              </div>
            </div>
            <p v-if="order.note" class="order-note">“{{ order.note }}”</p>
            <p v-else class="order-note muted">未填写特定需求，可展示任意符合价格档位的物品。</p>
            <div class="price-list">
              <span
                v-for="(price, rarity) in order.prices"
                :key="rarity"
                :style="{ color: rarityColor(String(rarity)), borderColor: rarityColor(String(rarity)) }"
              >{{ rarityLabel(String(rarity)) }} {{ price }}💰/件</span>
            </div>
            <footer>
              <span>{{ order.created_at ? formatDateTime(order.created_at) : '' }}</span>
              <button
                v-if="isMineOrder(order)"
                type="button"
                class="primary"
                :disabled="busy"
                @click="openOrderOffers(order)"
              >查看展示</button>
              <button
                v-else
                type="button"
                class="primary"
                :disabled="busy || !canTrade"
                @click="openOffer(order)"
              >展示物品</button>
            </footer>
          </article>
        </div>
        <Pagination
          v-if="store.marketBuyOrdersPagination"
          :pagination="store.marketBuyOrdersPagination"
          @go="goPage"
        />
      </div>
    </section>

    <div v-if="showCreate" class="nested-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showCreate = false">
      <section class="dialog-card s1-mobile-dialog">
        <header class="dialog-head s1-mobile-dialog-header">
          <h3>发布求购单</h3>
          <button type="button" @click="showCreate = false">✕</button>
        </header>
        <div class="dialog-body s1-mobile-dialog-body">
          <label>
            求购说明（可选）
            <input v-model="createNote" maxlength="200" placeholder="例如：想看看适合法师的装备">
          </label>
          <p class="muted">至少设置一个稀有度。价格是单件价，空白档位不接受展示。</p>
          <label class="keep-open-option">
            <input v-model="createKeepOpen" type="checkbox">
            <span>
              <strong>持续求购（不限总成交次数）</strong>
              <small>每批成交后保留求购单与未选展示，直到你手动取消；仍按每批实际成交金额扣款。</small>
            </span>
          </label>
          <div class="price-editor">
            <label v-for="rarity in rarities" :key="rarity">
              <span :style="{ color: rarityColor(rarity) }">{{ rarityLabel(rarity) }}</span>
              <input
                v-model.number="createPrices[rarity]"
                type="number"
                step="1"
                :min="priceRange(rarity)?.min"
                :max="priceRange(rarity)?.max"
                :placeholder="rangeText(rarity)"
              >
              <small>{{ rangeText(rarity) }}</small>
            </label>
          </div>
          <div class="dialog-actions">
            <button type="button" class="primary" :disabled="busy || !canTrade" @click="doCreate">
              发布
            </button>
            <button type="button" :disabled="busy" @click="showCreate = false">取消</button>
          </div>
        </div>
      </section>
    </div>

    <div v-if="offerTarget" class="nested-overlay s1-mobile-surface s1-mobile-overlay" @click.self="offerTarget = null">
      <section class="dialog-card wide s1-mobile-dialog">
        <header class="dialog-head s1-mobile-dialog-header">
          <h3>向 {{ offerTarget.buyer_name || '求购者' }} 展示物品</h3>
          <button type="button" @click="offerTarget = null">✕</button>
        </header>
        <div class="dialog-body s1-mobile-dialog-body">
          <p v-if="offerTarget.note" class="order-note">需求：“{{ offerTarget.note }}”</p>
          <p class="muted">
            物品暂不离开仓库。若其他求购先成交，仅在剩余库存不足本条展示数量时自动撤下。
          </p>
          <div v-if="warehouseLoading" class="empty">加载收藏室…</div>
          <div v-else-if="!eligibleItems.length" class="empty">收藏室没有该求购单接受的物品</div>
          <template v-else>
            <InventoryListToolbar
              v-model:rarity="inventoryRarity"
              v-model:sort="inventorySort"
              :items="eligibleItems"
              aria-label="按稀有度筛选可展示物品"
            />
            <div v-if="!visibleEligibleItems.length" class="empty">当前筛选下没有可展示物品</div>
            <div class="item-list">
              <div v-for="item in visibleEligibleItems" :key="item.id" class="item-row">
                <div>
                  <strong :style="{ color: rarityColor(item.rarity) }">{{ itemDisplayName(item) }}</strong>
                  <span>×{{ item.quantity ?? 1 }}</span>
                  <p v-if="item.desc">{{ item.desc }}</p>
                  <small>
                    单价 {{ priceFor(offerTarget, item.rarity) }}💰 ·
                    展示者实得 {{ sellerIncome(offerTarget, item.rarity, offerQty(item)) }}💰
                  </small>
                </div>
                <div class="item-actions">
                  <label v-if="(item.quantity ?? 1) > 1">
                    数量
                    <input
                      type="number"
                      :min="1"
                      :max="item.quantity ?? 1"
                      :value="offerQty(item)"
                      @focus="selectOfferQtyInput"
                      @input="setOfferQty(item, $event)"
                      @blur="normalizeOfferQtyInput(item, $event)"
                    >
                  </label>
                  <button type="button" class="primary" :disabled="busy" @click="doSubmitOffer(item)">
                    展示
                  </button>
                </div>
              </div>
            </div>
          </template>
        </div>
      </section>
    </div>

    <div v-if="showOrderOffers" class="nested-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeOrderOffers">
      <section class="dialog-card wide s1-mobile-dialog">
        <header class="dialog-head s1-mobile-dialog-header">
          <h3>待选物品展示</h3>
          <button type="button" @click="closeOrderOffers">✕</button>
        </header>
        <div class="dialog-body s1-mobile-dialog-body">
          <div v-if="!store.marketBuyOrderOffers.length" class="empty">还没有玩家展示物品</div>
          <template v-else>
            <div
              v-if="pendingBuyOffers.length && store.marketBuyOrderOffersTarget?.status === 'active'"
              class="offer-selection-toolbar"
            >
              <label class="select-all-offers">
                <input
                  type="checkbox"
                  :checked="allPendingOffersSelected"
                  :disabled="busy || !canTrade"
                  @change="toggleAllPendingOffers"
                >
                全选待处理展示
              </label>
              <span class="offer-selection-summary">
                已选 {{ selectedBuyOffers.length }} 项 / {{ selectedOfferQuantityTotal }} 件，
                共 {{ selectedOffersTotal }}💰
              </span>
              <button
                type="button"
                class="primary"
                :disabled="busy || !canTrade || !selectedBuyOffers.length"
                @click="doAcceptOffers"
              >成交所选</button>
            </div>
            <div class="item-list">
              <div
                v-for="offer in store.marketBuyOrderOffers"
                :key="offer.id"
                class="item-row"
                :class="{ selected: isBuyOfferSelected(offer) }"
              >
                <div>
                  <strong :style="{ color: rarityColor(offer.rarity) }">{{ buyOfferDisplayName(offer) }}</strong>
                  <span>×{{ offer.quantity }}</span>
                  <p v-if="offer.desc">{{ offer.desc }}</p>
                  <small>
                    {{ offer.seller_name || '?' }} ·
                    总价 {{ selectedOfferTotal(offer) }}💰 · {{ offerStatus(offer.status) }}
                  </small>
                </div>
                <label
                  v-if="offer.status === 'pending' && store.marketBuyOrderOffersTarget?.status === 'active'"
                  class="buy-offer-select"
                >
                  <input
                    type="checkbox"
                    :checked="isBuyOfferSelected(offer)"
                    :disabled="busy || !canTrade"
                    :aria-label="`选择${buyOfferDisplayName(offer)}`"
                    @change="toggleBuyOfferSelection(offer)"
                  >
                  {{ isBuyOfferSelected(offer) ? '已选择' : '选择' }}
                </label>
                <button
                  v-if="offer.status === 'pending' && store.marketBuyOrderOffersTarget?.status === 'active'"
                  type="button" :disabled="busy" @click="doRejectOffer(offer)"
                >不要这个</button>
              </div>
            </div>
          </template>
        </div>
      </section>
    </div>

    <div v-if="showMyOrders" class="nested-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showMyOrders = false">
      <section class="dialog-card wide s1-mobile-dialog">
        <header class="dialog-head s1-mobile-dialog-header">
          <h3>我的求购</h3>
          <button type="button" @click="showMyOrders = false">✕</button>
        </header>
        <div class="dialog-body s1-mobile-dialog-body">
          <h4>进行中（{{ store.myMarketBuyOrdersActive.length }}）</h4>
          <div v-if="!store.myMarketBuyOrdersActive.length" class="empty compact">没有进行中的求购</div>
          <div v-for="order in store.myMarketBuyOrdersActive" :key="order.id" class="history-row">
            <div class="history-order-copy">
              <span>{{ order.note || '不限具体物品' }}</span>
              <small v-if="order.keep_open">
                持续求购 · {{ order.price_paid ? `已累计成交 ${order.price_paid}💰` : '成交后继续开放' }}
              </small>
            </div>
            <div>
              <button type="button" class="primary" @click="openOrderOffers(order)">查看展示</button>
              <button type="button" :disabled="busy || !canTrade" @click="doCancelOrder(order)">取消</button>
            </div>
          </div>
          <h4>最近记录</h4>
          <div v-if="!store.myMarketBuyOrdersHistory.length" class="empty compact">暂无记录</div>
          <div v-for="order in store.myMarketBuyOrdersHistory" :key="order.id" class="history-row">
            <div>
              <template v-if="typeof order.price_paid === 'number' && order.price_paid > 0">
                <div class="history-purchase">
                  <strong>
                    {{ order.status === 'completed'
                      ? `已购入 ${acceptedOffersFor(order).length} 项`
                      : '持续求购已结束' }}
                    · {{ order.price_paid }}💰
                  </strong>
                  <small v-for="offer in acceptedOffersFor(order)" :key="offer.id">
                    「{{ buyOfferDisplayName(offer) }}」×{{ offer.quantity }}
                  </small>
                  <small v-if="order.keep_open && !acceptedOffersFor(order).length">已停止继续收购</small>
                </div>
              </template>
              <template v-else>已取消：{{ order.note || '不限具体物品' }}</template>
            </div>
          </div>
        </div>
      </section>
    </div>

    <div v-if="showMyOffers" class="nested-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showMyOffers = false">
      <section class="dialog-card wide s1-mobile-dialog">
        <header class="dialog-head s1-mobile-dialog-header">
          <h3>我的物品展示</h3>
          <button type="button" @click="showMyOffers = false">✕</button>
        </header>
        <div class="dialog-body s1-mobile-dialog-body">
          <div v-if="!store.myMarketBuyOffers.length" class="empty">暂无展示记录</div>
          <div v-for="offer in store.myMarketBuyOffers" :key="offer.id" class="history-row">
            <span>
              <strong :style="{ color: rarityColor(offer.rarity) }">{{ buyOfferDisplayName(offer) }}</strong>
              ×{{ offer.quantity }} → {{ offer.buy_order?.buyer_name || '?' }}
              · {{ offerStatus(offer.status) }}
            </span>
            <button
              v-if="offer.status === 'pending'"
              type="button"
              :disabled="busy || !canTrade"
              @click="doCancelOffer(offer)"
            >撤下</button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Pagination from '@/components/Pagination.vue'
import api from '@/composables/useApi'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useSocket } from '@/composables/useSocket'
import { formatDateTime } from '@/utils/time'
import { useInventoryListControls } from '../composables/useInventoryListControls'
import {
  isPlayableAdventurer,
  type MarketBuyOffer,
  type MarketBuyOrder,
  useAdventurerS1Store,
} from '../stores/adventurerS1'
import { hiddenBossClueItemDisplayName, isExploringStatus, rarityColor, rarityLabel } from '../utils'
import InventoryListToolbar from './InventoryListToolbar.vue'

interface WarehouseEntry {
  id: number
  name: string
  clue_label?: string | null
  type: string
  rarity: string
  desc?: string
  quantity?: number | null
  uses_left?: number | null
  evolution_stage?: number
  created_at?: string | null
  is_player_locked?: boolean
}

const emit = defineEmits<{ close: [] }>()
const store = useAdventurerS1Store()
const message = useGlobalMessage()
const socket = useSocket()
const rarities = ['common', 'rare', 'epic', 'legendary']
const rarityFilters = [
  { value: '', label: '全部' },
  ...rarities.map(value => ({ value, label: rarityLabel(value) })),
]

const busy = ref(false)
const rarityFilter = ref('')
const searchQuery = ref('')
const activeSearch = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null
const showCreate = ref(false)
const createNote = ref('')
const createKeepOpen = ref(false)
const createPrices = ref<Record<string, number | string | null>>({
  common: null,
  rare: null,
  epic: null,
  legendary: null,
})
const offerTarget = ref<MarketBuyOrder | null>(null)
const showOrderOffers = ref(false)
const showMyOrders = ref(false)
const showMyOffers = ref(false)
const viewingBuyOrderId = ref<number | null>(null)
const warehouseLoading = ref(false)
const warehouse = ref<WarehouseEntry[]>([])
const offerQuantities = ref<Record<number, number>>({})
const selectedBuyOfferIds = ref<Set<number>>(new Set())

const myAdv = computed(() => store.adventurers.find(isPlayableAdventurer) ?? null)
const canTrade = computed(() => !!myAdv.value && !isExploringStatus(myAdv.value?.status))
const eligibleItems = computed(() => {
  if (!offerTarget.value) return []
  return warehouse.value.filter(
    item => !item.is_player_locked && priceFor(offerTarget.value!, item.rarity) != null,
  )
})
const pendingBuyOffers = computed(() =>
  store.marketBuyOrderOffers.filter(offer => offer.status === 'pending'),
)
const selectedBuyOffers = computed(() =>
  pendingBuyOffers.value.filter(offer => selectedBuyOfferIds.value.has(offer.id)),
)
const allPendingOffersSelected = computed(() =>
  pendingBuyOffers.value.length > 0
  && pendingBuyOffers.value.every(offer => selectedBuyOfferIds.value.has(offer.id)),
)
const selectedOfferQuantityTotal = computed(() =>
  selectedBuyOffers.value.reduce((total, offer) => total + offer.quantity, 0),
)
const selectedOffersTotal = computed(() => roundMoney(
  selectedBuyOffers.value.reduce((total, offer) => total + selectedOfferTotal(offer), 0),
))
const {
  rarityFilter: inventoryRarity,
  sort: inventorySort,
  visibleItems: visibleEligibleItems,
} = useInventoryListControls(eligibleItems, itemDisplayName)

function itemDisplayName(item: WarehouseEntry): string {
  const name = hiddenBossClueItemDisplayName(item)
  if (item.type !== 'companion') return name
  return typeof item.evolution_stage === 'number'
    ? `${name}（嬗变${Math.max(0, item.evolution_stage)}阶）`
    : `${name}（阶级未记录）`
}

function buyOfferDisplayName(offer: MarketBuyOffer): string {
  const name = hiddenBossClueItemDisplayName({
    name: offer.item_name,
    clue_label: offer.clue_label,
  })
  if (offer.item_type !== 'companion') return name
  return typeof offer.evolution_stage === 'number'
    ? `${name}（嬗变${Math.max(0, offer.evolution_stage)}阶）`
    : `${name}（历史阶级未记录）`
}

function priceRange(rarity: string) {
  return store.marketSellPriceRange[rarity] ?? null
}

function rangeText(rarity: string): string {
  const range = priceRange(rarity)
  return range ? `${range.min} ~ ${range.max} 金币/件` : '加载价格范围中'
}

function priceFor(order: MarketBuyOrder, rarity: string): number | null {
  const value = order.prices?.[rarity]
  return typeof value === 'number' ? value : null
}

function sellerIncome(order: MarketBuyOrder, rarity: string, quantity: number): number {
  const total = (priceFor(order, rarity) ?? 0) * quantity
  return Math.round(total * (1 - store.marketSellFeeRate) * 100) / 100
}

function roundMoney(value: number): number {
  return Math.round(value * 100) / 100
}

function selectedOfferTotal(offer: MarketBuyOffer): number {
  const target = store.marketBuyOrderOffersTarget
  return target ? roundMoney((priceFor(target, offer.rarity) ?? 0) * offer.quantity) : 0
}

function acceptedOffersFor(order: MarketBuyOrder): MarketBuyOffer[] {
  if (order.accepted_offers?.length) return order.accepted_offers
  return order.accepted_offer ? [order.accepted_offer] : []
}

function isBuyOfferSelected(offer: MarketBuyOffer): boolean {
  return selectedBuyOfferIds.value.has(offer.id)
}

function toggleBuyOfferSelection(offer: MarketBuyOffer) {
  if (offer.status !== 'pending' || busy.value || !canTrade.value) return
  const next = new Set(selectedBuyOfferIds.value)
  if (next.has(offer.id)) next.delete(offer.id)
  else next.add(offer.id)
  selectedBuyOfferIds.value = next
}

function toggleAllPendingOffers() {
  if (busy.value || !canTrade.value) return
  if (allPendingOffersSelected.value) {
    selectedBuyOfferIds.value = new Set()
  } else {
    selectedBuyOfferIds.value = new Set(pendingBuyOffers.value.map(offer => offer.id))
  }
}

function syncSelectedBuyOffers() {
  const validIds = new Set(pendingBuyOffers.value.map(offer => offer.id))
  selectedBuyOfferIds.value = new Set(
    [...selectedBuyOfferIds.value].filter(offerId => validIds.has(offerId)),
  )
}

function isMineOrder(order: MarketBuyOrder): boolean {
  return store.adventurers.some(adv => adv.id === order.buyer_adventurer_id)
}

function offerQty(item: WarehouseEntry): number {
  return Math.min(item.quantity ?? 1, Math.max(1, offerQuantities.value[item.id] ?? 1))
}

function setOfferQty(item: WarehouseEntry, event: Event) {
  const input = event.target as HTMLInputElement
  const rawValue = input.value.trim()
  if (!/^\d+$/.test(rawValue)) return
  let value = Number.parseInt(rawValue, 10)
  value = Math.min(item.quantity ?? 1, Math.max(1, value))
  offerQuantities.value = { ...offerQuantities.value, [item.id]: value }
  input.value = String(value)
}

function selectOfferQtyInput(event: FocusEvent) {
  const input = event.target as HTMLInputElement
  input.select()
}

function normalizeOfferQtyInput(item: WarehouseEntry, event: FocusEvent) {
  const input = event.target as HTMLInputElement
  input.value = String(offerQty(item))
}

function offerStatus(status: string): string {
  return ({
    pending: '待选择',
    accepted: '已成交',
    rejected: '未选中',
    cancelled: '已撤下',
  } as Record<string, string>)[status] ?? status
}

async function refresh(page?: number) {
  await store.fetchMarketBuyOrders(
    page ?? store.marketBuyOrdersPagination?.current_page ?? 1,
    rarityFilter.value || undefined,
    activeSearch.value || undefined,
  )
}

function goPage(page: number) {
  refresh(page)
}

function setRarity(value: string) {
  rarityFilter.value = value
  refresh(1)
}

function debouncedSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    activeSearch.value = searchQuery.value.trim()
    refresh(1)
  }, 300)
}

function clearSearch() {
  searchQuery.value = ''
  activeSearch.value = ''
  if (searchTimer) clearTimeout(searchTimer)
  refresh(1)
}

function openCreate() {
  if (!canTrade.value) return
  createNote.value = ''
  createKeepOpen.value = false
  createPrices.value = { common: null, rare: null, epic: null, legendary: null }
  showCreate.value = true
}

async function doCreate() {
  if (!myAdv.value || busy.value || !canTrade.value) return
  const prices: Record<string, number> = {}
  for (const rarity of rarities) {
    const raw = createPrices.value[rarity]
    if (raw == null || raw === '') continue
    const value = Number(raw)
    const range = priceRange(rarity)
    if (!Number.isFinite(value) || value <= 0) {
      message.error(`${rarityLabel(rarity)}价格格式不正确`)
      return
    }
    if (range && (value < range.min || value > range.max)) {
      message.error(`${rarityLabel(rarity)}价格须在 ${range.min}~${range.max} 金币之间`)
      return
    }
    prices[rarity] = value
  }
  if (!Object.keys(prices).length) {
    message.error('至少设置一个稀有度的求购价格')
    return
  }
  busy.value = true
  try {
    const res = await store.createMarketBuyOrder(
      myAdv.value.id,
      prices,
      createNote.value.trim(),
      createKeepOpen.value,
    )
    if (res.code === 201 || res.code === 200) {
      message.success('求购单已发布')
      showCreate.value = false
      await refresh(1)
    } else {
      message.error(res.message || '发布失败')
    }
  } catch (error: any) {
    message.error(error?.response?.data?.message || '发布失败')
  } finally {
    busy.value = false
  }
}

async function loadWarehouse() {
  if (!myAdv.value) return
  warehouseLoading.value = true
  try {
    const { data } = await api.get(`/adventurer-s1/${myAdv.value.id}`)
    if (data.code === 200) warehouse.value = data.data?.warehouse ?? []
    else message.error(data.message || '加载收藏室失败')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '加载收藏室失败')
  } finally {
    warehouseLoading.value = false
  }
}

async function openOffer(order: MarketBuyOrder) {
  if (!canTrade.value) return
  offerTarget.value = order
  offerQuantities.value = {}
  await loadWarehouse()
}

async function doSubmitOffer(item: WarehouseEntry) {
  if (!myAdv.value || !offerTarget.value || busy.value || !canTrade.value) return
  const quantity = offerQty(item)
  busy.value = true
  try {
    const res = await store.submitMarketBuyOffer(
      offerTarget.value.id,
      myAdv.value.id,
      item.id,
      quantity,
    )
    if (res.code === 201 || res.code === 200) {
      message.success(`已向求购者展示「${itemDisplayName(item)}」×${quantity}`)
      offerTarget.value = null
    } else {
      message.error(res.message || '展示失败')
    }
  } catch (error: any) {
    message.error(error?.response?.data?.message || '展示失败')
  } finally {
    busy.value = false
  }
}

async function openOrderOffers(order: MarketBuyOrder) {
  selectedBuyOfferIds.value = new Set()
  viewingBuyOrderId.value = order.id
  showOrderOffers.value = true
  showMyOrders.value = false
  await store.fetchMarketBuyOrderOffers(order.id)
  if (viewingBuyOrderId.value === order.id) syncSelectedBuyOffers()
}

function closeOrderOffers() {
  showOrderOffers.value = false
  viewingBuyOrderId.value = null
  selectedBuyOfferIds.value = new Set()
}

async function doAcceptOffers() {
  const order = store.marketBuyOrderOffersTarget
  const orderId = viewingBuyOrderId.value
  const offers = selectedBuyOffers.value
  if (
    !orderId
    || !order
    || order.id !== orderId
    || !offers.length
    || busy.value
    || !canTrade.value
  ) return
  const total = selectedOffersTotal.value
  const quantityTotal = selectedOfferQuantityTotal.value
  const settlementHint = order.keep_open
    ? '所选展示将全部成交；任一项失败均不会扣款，未选展示继续等待后续选择。'
    : '所选展示将全部成交；任一项失败均不会扣款，未选展示会自动撤下。'
  if (!window.confirm(
    `确认花费 ${total} 金币，一次购入 ${offers.length} 项、共 ${quantityTotal} 件物品？\n`
    + settlementHint,
  )) return
  busy.value = true
  try {
    const res = await store.acceptMarketBuyOffers(orderId, offers.map(offer => offer.id))
    if (res.code === 200) {
      const orderStillOpen = res.data?.buy_order_status === 'active'
        || (res.data?.buy_order_status == null && order.keep_open)
      message.success(
        `成交成功，获得 ${offers.length} 项、共 ${quantityTotal} 件物品`
        + (orderStillOpen ? '；求购单继续开放' : ''),
      )
      const keepCurrentDialog = orderStillOpen && viewingBuyOrderId.value === orderId
      if (viewingBuyOrderId.value === orderId) {
        selectedBuyOfferIds.value = new Set()
        if (!keepCurrentDialog) closeOrderOffers()
      }
      const refreshRequests: Promise<unknown>[] = [
        refresh(),
        store.fetchMyMarketBuyOrders(),
        store.fetchAdventurers(),
      ]
      if (keepCurrentDialog) {
        refreshRequests.push(store.fetchMarketBuyOrderOffers(orderId))
      }
      await Promise.all(refreshRequests)
      if (keepCurrentDialog && viewingBuyOrderId.value === orderId) {
        syncSelectedBuyOffers()
      }
    } else {
      message.error(res.message || '成交失败')
      if (showOrderOffers.value && viewingBuyOrderId.value === orderId) {
        await store.fetchMarketBuyOrderOffers(orderId)
        if (viewingBuyOrderId.value === orderId) syncSelectedBuyOffers()
      }
    }
  } catch (error: any) {
    message.error(error?.response?.data?.message || '成交失败')
    if (showOrderOffers.value && viewingBuyOrderId.value === orderId) {
      await store.fetchMarketBuyOrderOffers(orderId).catch(() => undefined)
      if (viewingBuyOrderId.value === orderId) syncSelectedBuyOffers()
    }
  } finally {
    busy.value = false
  }
}

async function doRejectOffer(offer: MarketBuyOffer) {
  const orderId = viewingBuyOrderId.value
  if (!orderId || busy.value) return
  busy.value = true
  try {
    const res = await store.rejectMarketBuyOffer(orderId, offer.id)
    if (res.code !== 200) {
      message.error(res.message || '拒绝展示失败')
      return
    }
    message.success('已拒绝，对方可以重新展示')
    await Promise.all([store.fetchMarketBuyOrderOffers(orderId), store.fetchMyMarketBuyOrders()])
    if (viewingBuyOrderId.value === orderId) syncSelectedBuyOffers()
  } catch (e: any) {
    message.error(e?.response?.data?.message || '处理失败，请刷新展示列表确认状态')
  } finally {
    busy.value = false
  }
}

async function openMyOrders() {
  showMyOrders.value = true
  await store.fetchMyMarketBuyOrders()
}

async function doCancelOrder(order: MarketBuyOrder) {
  if (busy.value || !canTrade.value) return
  busy.value = true
  try {
    const res = await store.cancelMarketBuyOrder(order.id)
    if (res.code === 200) {
      message.success('求购单已取消')
      await Promise.all([store.fetchMyMarketBuyOrders(), refresh()])
    } else message.error(res.message || '取消失败')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '取消失败')
  } finally {
    busy.value = false
  }
}

async function openMyOffers() {
  showMyOffers.value = true
  await store.fetchMyMarketBuyOffers()
}

async function doCancelOffer(offer: MarketBuyOffer) {
  if (busy.value || !canTrade.value) return
  busy.value = true
  try {
    const res = await store.cancelMarketBuyOffer(offer.id)
    if (res.code === 200) {
      message.success('展示已撤下')
      await store.fetchMyMarketBuyOffers()
    } else message.error(res.message || '撤下失败')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '撤下失败')
  } finally {
    busy.value = false
  }
}

async function onMarketChanged() {
  const orderId = viewingBuyOrderId.value
  const requests: Promise<unknown>[] = [refresh()]
  if (showOrderOffers.value && orderId) {
    requests.push(store.fetchMarketBuyOrderOffers(orderId))
  }
  await Promise.all(requests)
  syncSelectedBuyOffers()
}

onMounted(async () => {
  if (!store.adventurers.length) await store.fetchAdventurers()
  await store.fetchMarketBuyOrders(1)
  socket.on('adventurer_s1_market_changed', onMarketChanged)
})

onUnmounted(() => {
  if (searchTimer) clearTimeout(searchTimer)
  socket.off('adventurer_s1_market_changed', onMarketChanged)
})
</script>

<style scoped>
.buy-market-overlay,
.nested-overlay {
  position: fixed;
  inset: 0;
  z-index: 1320;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(7, 10, 18, 0.78);
}

.nested-overlay {
  z-index: 1330;
  background: rgba(7, 10, 18, 0.84);
}

.buy-market-card,
.dialog-card {
  width: min(1120px, 96vw);
  max-height: 92vh;
  overflow: hidden;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 14px;
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.45);
}

.dialog-card {
  width: min(620px, 94vw);
}

.dialog-card.wide {
  width: min(920px, 94vw);
}

.panel-head,
.dialog-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
  border-bottom: 1px solid var(--color-border);
}

.panel-head h3,
.dialog-head h3 {
  margin: 0;
}

.panel-head p {
  margin: 5px 0 0;
  color: var(--color-text-secondary);
  font-size: 13px;
}

button,
input {
  font: inherit;
}

input[type='checkbox'] {
  width: 16px;
  height: 16px;
  min-width: 16px;
  padding: 0;
  accent-color: #c77a29;
}

button {
  padding: 7px 12px;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  cursor: pointer;
}

button:hover:not(:disabled) {
  border-color: var(--color-primary);
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

button.primary {
  color: #fff;
  background: #9a5a17;
  border-color: #c77a29;
}

.close-btn,
.dialog-head button {
  padding: 4px 8px;
  background: transparent;
  border-color: transparent;
}

.panel-body,
.dialog-body {
  max-height: calc(92vh - 78px);
  overflow: auto;
  padding: 18px 20px 24px;
}

.toolbar {
  display: grid;
  grid-template-columns: minmax(210px, 1fr) auto auto;
  gap: 12px;
  align-items: center;
}

.search-wrap {
  display: flex;
  gap: 7px;
}

input {
  min-width: 0;
  padding: 8px 10px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.search-wrap input {
  width: 100%;
}

.rarity-filters,
.actions,
.price-list,
.dialog-actions,
.item-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.rarity-filters button.active {
  background: color-mix(in srgb, #c77a29 16%, var(--color-surface));
  border-color: #c58a3b;
}

.with-badge {
  position: relative;
}

.badge {
  display: inline-flex;
  min-width: 18px;
  height: 18px;
  align-items: center;
  justify-content: center;
  margin-left: 5px;
  padding: 0 5px;
  color: #fff;
  background: #c43d45;
  border-radius: 999px;
  font-size: 11px;
}

.rule-hint {
  margin: 14px 0;
  padding: 10px 12px;
  color: var(--color-text-secondary);
  background: rgba(202, 133, 45, 0.1);
  border: 1px solid rgba(202, 133, 45, 0.35);
  border-radius: 9px;
  font-size: 13px;
  line-height: 1.6;
}

.order-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 290px), 1fr));
  gap: 12px;
}

.order-card { display: flex; min-width: 0; flex-direction: column; gap: 8px; }

.order-card,
.item-row,
.history-row {
  padding: 14px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 10px;
}

.order-head { flex-wrap: wrap; }
.order-head > strong { min-width: 0; overflow-wrap: anywhere; }

.order-head,
.order-card footer,
.item-row,
.history-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.offer-alert {
  color: var(--color-warning);
  font-size: 12px;
}

.order-flags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 7px;
}

.mode-badge {
  padding: 3px 7px;
  color: var(--color-success);
  background: rgba(48, 143, 105, 0.14);
  border: 1px solid rgba(88, 194, 151, 0.45);
  border-radius: 999px;
  font-size: 12px;
}

.order-note {
  overflow-wrap: anywhere;
  min-height: 0;
  margin: 10px 0;
  line-height: 1.5;
}

.muted {
  color: var(--color-text-secondary);
}

.price-list span {
  padding: 4px 7px;
  border: 1px solid;
  border-radius: 999px;
  font-size: 12px;
}

.order-card footer {
  margin-top: auto;
  padding-top: 10px;
  border-top: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  font-size: 12px;
}

.price-editor {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 14px 0;
}

.dialog-body > label,
.price-editor label {
  display: grid;
  gap: 6px;
}

.price-editor small,
.item-row small {
  color: var(--color-text-secondary);
}

.dialog-body > .keep-open-option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin: 12px 0 4px;
  padding: 11px 12px;
  background: rgba(48, 143, 105, 0.1);
  border: 1px solid rgba(88, 194, 151, 0.32);
  border-radius: 9px;
  cursor: pointer;
}

.keep-open-option input {
  margin-top: 2px;
}

.keep-open-option span,
.history-order-copy {
  display: grid;
  gap: 4px;
}

.keep-open-option small,
.history-order-copy small {
  color: var(--color-text-secondary);
  line-height: 1.45;
}

.item-list {
  display: grid;
  gap: 10px;
  margin-top: 12px;
}

.item-row > div:first-child {
  min-width: 0;
}

.item-row p {
  margin: 5px 0;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.item-row strong {
  margin-right: 8px;
}

.item-row.selected {
  background: rgba(154, 90, 23, 0.18);
  border-color: #c77a29;
}

.offer-selection-toolbar {
  position: sticky;
  top: -18px;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
}

.select-all-offers,
.buy-offer-select {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  cursor: pointer;
}

.offer-selection-summary {
  flex: 1;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.buy-offer-select {
  flex: 0 0 auto;
  color: var(--color-primary);
}

.history-purchase {
  display: grid;
  gap: 4px;
}

.history-purchase small {
  color: var(--color-text-secondary);
}

.item-actions {
  flex: 0 0 auto;
}

.item-actions label {
  display: flex;
  gap: 6px;
  align-items: center;
}

.item-actions input {
  width: 70px;
}

.history-row {
  margin-bottom: 8px;
}

.dialog-body h4 {
  margin: 18px 0 8px;
}

.dialog-body h4:first-child {
  margin-top: 0;
}

.empty {
  padding: 36px 12px;
  color: var(--color-text-secondary);
  text-align: center;
}

.empty.compact {
  padding: 15px 10px;
}

@media (max-width: 820px) {
  .buy-market-overlay,
  .nested-overlay {
    align-items: flex-end;
    padding: 0;
  }

  .buy-market-card,
  .dialog-card,
  .dialog-card.wide {
    width: 100%;
    max-height: 94vh;
    border-radius: 14px 14px 0 0;
  }

  .toolbar {
    grid-template-columns: 1fr;
  }

  .actions {
    justify-content: stretch;
  }

  .actions button {
    flex: 1;
  }

  .price-editor {
    grid-template-columns: 1fr;
  }

  .item-row,
  .history-row {
    align-items: flex-start;
    flex-direction: column;
  }

  .offer-selection-toolbar {
    position: static;
    align-items: stretch;
    flex-direction: column;
  }

  .offer-selection-toolbar button {
    width: 100%;
  }
}
@media (max-width: 640px) {
  .panel-head, .dialog-head { padding: 8px 12px; gap: 8px; }
  .panel-body, .dialog-body { min-width: 0; max-height: none; padding: 14px; }
  .buy-market-card, .dialog-card { display: flex; flex-direction: column; }
  .order-grid { grid-template-columns: minmax(0, 1fr); }
  .order-card, .item-row, .history-row { min-width: 0; padding: 12px; }
  .order-head, .order-card footer { flex-wrap: wrap; align-items: flex-start; gap: 8px; }
  .order-flags { justify-content: flex-start; }
  .order-note { min-height: 0; font-size: 13px; line-height: 1.6; }
  .rarity-filters { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 5px; }
  .rarity-filters button { min-width: 0; min-height: 32px; padding: 5px 3px; font-size: 12px; }
  .actions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px; }
  .actions button { min-width: 0; min-height: 32px; padding: 5px 3px; font-size: 12px; white-space: normal; }
  .dialog-actions, .item-actions { width: 100%; }
  .dialog-actions button, .item-actions button { flex: 1 1 100px; white-space: normal; }
}
</style>
