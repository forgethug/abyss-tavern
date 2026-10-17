<template>
  <div class="buyback-panel">
    <div class="buyback-header">
      <h5 class="section-title">💵 物品收购</h5>
      <div v-if="catalog?.adventurer" class="buyback-wallet">
        {{ catalog.adventurer.name }} · 💰 {{ catalog.adventurer.gold }} 金币
      </div>
    </div>
    <p class="buyback-hint">
      回收价 = 类型与稀有度底价的一半；若在草药店/军需店有售，则不超过商店价的一半。无需派遣占位。
    </p>

    <div v-if="loading" class="empty-helpers">加载中…</div>
    <div v-else-if="!catalog?.adventurer" class="empty-helpers">你还没有冒险者，请先创建</div>
    <template v-else>
      <div class="buyback-tabs">
        <button
          type="button"
          :class="['tab-btn', { active: tab === 'equipment' }]"
          @click="tab = 'equipment'"
        >
          随身背包 ({{ carryItems.length }})
        </button>
        <button
          type="button"
          :class="['tab-btn', { active: tab === 'warehouse' }]"
          @click="tab = 'warehouse'"
        >
          仓库 ({{ warehouseItems.length }})
        </button>
      </div>

      <div v-if="activeItems.length === 0" class="empty-helpers">暂无可出售物品</div>
      <InventoryListToolbar
        v-else
        v-model:rarity="rarityFilter"
        v-model:sort="inventorySort"
        :items="activeItems"
        aria-label="按稀有度筛选可出售物品"
      />
      <div v-if="activeItems.length > 0 && visibleItems.length === 0" class="empty-helpers">当前稀有度下没有可出售物品</div>
      <div v-if="visibleItems.length > 0" class="buyback-batch-toolbar">
        <label class="buyback-select-visible">
          <input
            type="checkbox"
            :checked="allVisibleSellableSelected"
            :disabled="visibleSellableItems.length === 0 || isSelling"
            @change="toggleVisibleSelection"
          />
          全选当前列表
        </label>
        <div class="buyback-batch-summary">
          已选 {{ selectedItems.length }} 项 / {{ selectedQuantityTotal }} 件，
          预计 💰 {{ selectedGoldTotal.toLocaleString() }}
        </div>
        <button
          type="button"
          class="btn-primary btn-sm buyback-batch-sell-btn"
          :disabled="selectedItems.length === 0 || isSelling"
          @click="sellSelectedItems"
        >
          {{ batchSelling ? '批量出售中…' : '一起卖出' }}
        </button>
      </div>
      <div v-if="visibleItems.length > 0" class="buyback-list">
        <div
          v-for="item in visibleItems"
          :key="`${item.source}-${item.id}`"
          class="buyback-card"
          :class="{ disabled: !item.can_sell, selected: isSelected(item) }"
        >
          <div class="buyback-card-head">
            <label class="buyback-item-select">
              <input
                type="checkbox"
                :checked="isSelected(item)"
                :disabled="!item.can_sell || isSelling"
                :aria-label="`选择出售${item.name}`"
                @change="toggleItemSelection(item)"
              />
              <span class="buyback-item-name" :style="{ color: rarityColor(item.rarity) }">{{ item.name }}</span>
            </label>
            <span class="buyback-item-meta">
              {{ rarityLabel(item.rarity) }}
              <template v-if="stackQuantity(item) > 1"> · ×{{ stackQuantity(item) }}</template>
              <template v-if="item.uses_left != null"> · 剩余 {{ item.uses_left }} 次</template>
            </span>
          </div>
          <ItemDescription class="buyback-item-desc" :text="item.description" />
          <div class="buyback-card-foot">
            <span class="buyback-price">
              回收
              <template v-if="stackQuantity(item) > 1">
                💰 {{ item.recycle_price }} × {{ sellQtyFor(item) }} = {{ sellGoldTotal(item) }}
              </template>
              <template v-else>💰 {{ item.recycle_price }}</template>
            </span>
            <div class="buyback-sell-actions">
              <label
                v-if="stackQuantity(item) > 1 && item.can_sell"
                class="buyback-qty-label"
              >
                数量
                <input
                  type="number"
                  class="buyback-qty-input"
                  :min="1"
                  :max="stackQuantity(item)"
                  :value="sellQtyFor(item)"
                  @input="onQtyInput(item, $event)"
                />
              </label>
              <button
                type="button"
                class="btn-primary btn-sm buyback-sell-btn"
                :disabled="!item.can_sell || isSelling"
                @click="sellItem(item)"
              >
                {{ sellingKey === itemKey(item) ? '出售中…' : '出售' }}
              </button>
            </div>
          </div>
          <div v-if="!item.can_sell && item.reject_reason" class="buyback-reject-reason">
            {{ item.reject_reason }}
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import ItemDescription from './ItemDescription.vue'
import { ref, computed, onMounted } from 'vue'
import {
  useAdventurerS1Store,
  type BuildingInstance,
  type BuybackCatalog,
  type BuybackSellableItem,
} from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { rarityColor, rarityLabel } from '../utils'
import InventoryListToolbar from './InventoryListToolbar.vue'
import { useInventoryListControls } from '../composables/useInventoryListControls'

const props = defineProps<{
  building: BuildingInstance
}>()

const emit = defineEmits<{
  refreshed: []
}>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const loading = ref(false)
const sellingKey = ref<string | null>(null)
const batchSelling = ref(false)
const catalog = ref<BuybackCatalog | null>(null)
const tab = ref<'equipment' | 'warehouse'>('equipment')
const sellQuantities = ref<Record<string, number>>({})
const selectedKeys = ref<Set<string>>(new Set())

const carryItems = computed(() => catalog.value?.carry_items ?? [])
const warehouseItems = computed(() => catalog.value?.warehouse_items ?? [])
const allItems = computed(() => [...carryItems.value, ...warehouseItems.value])
const activeItems = computed(() =>
  tab.value === 'equipment' ? carryItems.value : warehouseItems.value,
)
const {
  rarityFilter,
  sort: inventorySort,
  visibleItems,
} = useInventoryListControls(activeItems)
const isSelling = computed(() => sellingKey.value != null || batchSelling.value)
const visibleSellableItems = computed(() => visibleItems.value.filter(item => item.can_sell))
const selectedItems = computed(() =>
  allItems.value.filter(item => item.can_sell && selectedKeys.value.has(itemKey(item))),
)
const allVisibleSellableSelected = computed(() =>
  visibleSellableItems.value.length > 0
  && visibleSellableItems.value.every(item => selectedKeys.value.has(itemKey(item))),
)
const selectedQuantityTotal = computed(() =>
  selectedItems.value.reduce((total, item) => total + sellQtyFor(item), 0),
)
const selectedGoldTotal = computed(() =>
  selectedItems.value.reduce((total, item) => total + sellGoldTotal(item), 0),
)

function itemKey(item: BuybackSellableItem) {
  return `${item.source}-${item.id}`
}

function stackQuantity(item: BuybackSellableItem) {
  return item.quantity ?? 1
}

function sellQtyFor(item: BuybackSellableItem) {
  const key = itemKey(item)
  const max = stackQuantity(item)
  const raw = sellQuantities.value[key]
  if (raw == null) return max
  return Math.min(max, Math.max(1, Math.floor(raw)))
}

function sellGoldTotal(item: BuybackSellableItem) {
  return item.recycle_price * sellQtyFor(item)
}

function isSelected(item: BuybackSellableItem) {
  return selectedKeys.value.has(itemKey(item))
}

function toggleItemSelection(item: BuybackSellableItem) {
  if (!item.can_sell || isSelling.value) return
  const next = new Set(selectedKeys.value)
  const key = itemKey(item)
  if (next.has(key)) {
    next.delete(key)
  } else {
    next.add(key)
  }
  selectedKeys.value = next
}

function toggleVisibleSelection() {
  if (isSelling.value) return
  const next = new Set(selectedKeys.value)
  if (allVisibleSellableSelected.value) {
    for (const item of visibleSellableItems.value) next.delete(itemKey(item))
  } else {
    for (const item of visibleSellableItems.value) next.add(itemKey(item))
  }
  selectedKeys.value = next
}

function syncSellQuantities(items: BuybackSellableItem[]) {
  const next = { ...sellQuantities.value }
  for (const item of items) {
    const key = itemKey(item)
    const max = stackQuantity(item)
    const prev = next[key]
    if (prev == null || prev > max) {
      next[key] = max
    } else if (prev < 1) {
      next[key] = 1
    }
  }
  sellQuantities.value = next
}

function onQtyInput(item: BuybackSellableItem, event: Event) {
  const input = event.target as HTMLInputElement
  const max = stackQuantity(item)
  let value = parseInt(input.value, 10)
  if (Number.isNaN(value)) value = 1
  value = Math.min(max, Math.max(1, value))
  sellQuantities.value = { ...sellQuantities.value, [itemKey(item)]: value }
  if (String(value) !== input.value) {
    input.value = String(value)
  }
}

async function loadCatalog() {
  loading.value = true
  try {
    const res = await store.fetchBuybackItems(props.building.id)
    if (res.code === 200 && res.data) {
      catalog.value = res.data as BuybackCatalog
      syncSellQuantities([
        ...catalog.value.carry_items,
        ...catalog.value.warehouse_items,
      ])
      const validKeys = new Set(
        allItems.value.filter(item => item.can_sell).map(item => itemKey(item)),
      )
      selectedKeys.value = new Set(
        [...selectedKeys.value].filter(key => validKeys.has(key)),
      )
    } else {
      message.error(res.message || '加载收购列表失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载收购列表失败')
  } finally {
    loading.value = false
  }
}

async function sellItem(item: BuybackSellableItem) {
  const adv = catalog.value?.adventurer
  if (!adv || isSelling.value) return

  const qty = sellQtyFor(item)
  const total = sellGoldTotal(item)
  const qtyText = qty > 1 ? `${item.recycle_price} × ${qty} = ${total}` : String(total)
  if (!confirm(`确定以 ${qtyText} 金币出售「${item.name}」吗？`)) return

  const key = itemKey(item)
  sellingKey.value = key
  try {
    const res = await store.sellToBuyback(
      props.building.id,
      adv.id,
      item.source,
      item.id,
      qty > 1 || stackQuantity(item) > 1 ? qty : undefined,
    )
    if (res.code === 200) {
      const gold = res.data?.sold?.gold_total ?? total
      message.success(`已出售「${item.name}」${qty > 1 ? `×${qty}` : ''}，获得 ${gold} 金币`)
      await Promise.all([
        loadCatalog(),
        store.fetchAdventurers(),
      ])
      emit('refreshed')
    } else {
      message.error(res.message || '出售失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '出售失败')
  } finally {
    sellingKey.value = null
  }
}

async function sellSelectedItems() {
  const adv = catalog.value?.adventurer
  const items = selectedItems.value
  if (!adv || items.length === 0 || isSelling.value) return

  const quantityTotal = selectedQuantityTotal.value
  const goldTotal = selectedGoldTotal.value
  if (!confirm(
    `确定一起卖出已选的 ${items.length} 项、共 ${quantityTotal} 件物品，获得 ${goldTotal} 金币吗？`,
  )) return

  batchSelling.value = true
  try {
    const res = await store.sellBatchToBuyback(
      props.building.id,
      adv.id,
      items.map(item => ({
        source: item.source,
        item_id: item.id,
        quantity: sellQtyFor(item),
      })),
    )
    if (res.code === 200) {
      const receivedGold = res.data?.gold_total ?? goldTotal
      message.success(`已一起卖出 ${items.length} 项、共 ${quantityTotal} 件物品，获得 ${receivedGold} 金币`)
      selectedKeys.value = new Set()
      await Promise.all([
        loadCatalog(),
        store.fetchAdventurers(),
      ])
      emit('refreshed')
    } else {
      message.error(res.message || '批量出售失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '批量出售失败')
  } finally {
    batchSelling.value = false
  }
}

onMounted(loadCatalog)
</script>

<style scoped>
.buyback-panel {
  margin-bottom: 16px;
}
.buyback-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.buyback-wallet {
  font-size: 0.9rem;
  color: var(--color-text-secondary, #8b949e);
}
.buyback-hint {
  font-size: 0.85rem;
  color: var(--color-text-secondary, #8b949e);
  margin: 8px 0 12px;
}
.buyback-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.tab-btn {
  padding: 6px 12px;
  border: 1px solid var(--color-border, #30363d);
  background: var(--color-bg, #0d1117);
  color: var(--color-text, #e6edf3);
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.85rem;
}
.tab-btn.active {
  background: var(--color-surface, #161b22);
  border-color: var(--color-primary, #58a6ff);
}
.buyback-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr));
  gap: 10px;
}
.buyback-batch-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 10px 0;
  padding: 10px 12px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 8px;
  background: var(--color-surface, #161b22);
}
.buyback-select-visible,
.buyback-item-select {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.buyback-select-visible {
  font-size: 0.85rem;
}
.buyback-batch-summary {
  flex: 1;
  min-width: 180px;
  color: var(--color-text-secondary, #8b949e);
  font-size: 0.85rem;
}
.buyback-batch-sell-btn {
  flex-shrink: 0;
}
.buyback-card {
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 12px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 8px;
  background: var(--color-surface, #161b22);
}
.buyback-card.disabled {
  opacity: 0.65;
}
.buyback-card.selected {
  border-color: var(--color-primary, #58a6ff);
  box-shadow: 0 0 0 1px var(--color-primary, #58a6ff);
}
.buyback-card-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.buyback-item-name {
  overflow-wrap: anywhere;
  font-weight: 600;
}
.buyback-item-meta {
  font-size: 0.8rem;
  color: var(--color-text-secondary, #8b949e);
}
.buyback-item-desc {
  font-size: 0.85rem;
  color: var(--color-text-secondary, #8b949e);
  margin: 6px 0;
  line-height: 1.4;
}
.buyback-card-foot {
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--color-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.buyback-price {
  font-weight: 600;
  color: #f0883e;
}
.buyback-sell-actions {
  min-width: 0;
  flex-wrap: wrap;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.buyback-qty-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.8rem;
  color: var(--color-text-secondary, #8b949e);
}
.buyback-qty-input {
  width: 56px;
  padding: 4px 6px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 4px;
  background: var(--color-bg, #0d1117);
  color: var(--color-text, #e6edf3);
  font-size: 0.85rem;
}
.buyback-sell-btn {
  flex-shrink: 0;
}
.buyback-reject-reason {
  font-size: 0.8rem;
  color: #f85149;
  margin-top: 6px;
}
@media (max-width: 640px) {
  .buyback-tabs { flex-wrap: wrap; }
  .buyback-tabs button { flex: 1; white-space: normal; }
  .buyback-batch-toolbar { align-items: stretch; gap: 8px; }
  .buyback-batch-summary { min-width: 0; flex-basis: 100%; }
  .buyback-batch-sell-btn { width: 100%; }
  .buyback-select-visible, .buyback-item-select { min-height: 32px; }
  .buyback-sell-actions { width: 100%; min-width: 0; justify-content: space-between; flex-wrap: wrap; }
  .buyback-qty-input { width: 80px; }
  .buyback-item-desc { font-size: 13px; line-height: 1.6; }
}
</style>
