<template>
  <div class="shop-panel">
    <div class="shop-header">
      <h5 class="section-title">🛒 商品列表</h5>
      <div v-if="catalog?.adventurer" class="shop-wallet">
        {{ catalog.adventurer.name }} · 💰 {{ catalog.adventurer.gold }} 金币
      </div>
    </div>
    <p class="shop-hint">无需派遣冒险者占位；购买后物品进入冒险者随身，随身满时非草药店类商品自动入仓库。</p>
    <p v-if="catalog?.sale_ends_at" class="shop-hint">
      {{ saleStartLabel }} 至 {{ saleEndLabel }}（香港时间）。愿月圆人安，旅途有甜。
    </p>
    <button v-if="catalog?.sale_ends_at" type="button" :disabled="loading || buyingId != null" @click="loadCatalog">刷新商品</button>
    <p v-if="saleClosedReason" class="shop-reject-reason">{{ saleClosedReason }}</p>

    <div v-if="loading" class="empty-helpers">加载中…</div>
    <div v-else-if="!catalog?.adventurer" class="empty-helpers">你还没有冒险者，请先创建</div>
    <div v-else-if="items.length === 0" class="empty-helpers">暂无在售商品</div>
    <div v-else class="shop-list">
      <div
        v-for="item in items"
        :key="item.id"
        class="shop-card"
        :class="{ disabled: !item.can_buy }"
      >
        <div class="shop-card-head">
          <span class="shop-item-name" :style="{ color: rarityColor(item.rarity) }">
            {{ item.name }}<span v-if="item.quantity && item.quantity > 1" class="shop-item-qty">×{{ item.quantity }}</span>
          </span>
          <span class="shop-item-meta">{{ rarityLabel(item.rarity) }}<template v-if="item.immutable"> · 不可变</template> · 库存 {{ item.stock }}</span>
        </div>
        <ItemDescription class="shop-item-desc" :text="item.description" />
        <div class="shop-card-foot">
          <span class="shop-price">💰 {{ item.price }}</span>
          <button
            type="button"
            class="btn-primary btn-sm shop-buy-btn"
            :disabled="!item.can_buy || buyingId != null || !!saleClosedReason"
            @click="buyItem(item)"
          >
            {{ buyingId === item.id ? '购买中…' : '购买' }}
          </button>
        </div>
        <div v-if="!item.can_buy && item.reject_reason" class="shop-reject-reason">
          {{ item.reject_reason }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import ItemDescription from './ItemDescription.vue'
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  useAdventurerS1Store,
  type BuildingInstance,
  type ShopCatalog,
  type ShopCatalogItem,
} from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { rarityColor, rarityLabel } from '../utils'

const props = defineProps<{
  building: BuildingInstance
}>()

const emit = defineEmits<{
  refreshed: []
}>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const loading = ref(false)
const buyingId = ref<number | null>(null)
const catalog = ref<ShopCatalog | null>(null)

const items = computed(() => catalog.value?.items ?? [])
const now = ref(Date.now())
let serverClockOffset = 0
let clockTimer: ReturnType<typeof setInterval> | undefined
const saleStartLabel = computed(() => catalog.value?.sale_starts_at
  ? new Date(catalog.value.sale_starts_at).toLocaleString('zh-CN', { timeZone: 'Asia/Hong_Kong', hourCycle: 'h23' })
  : '')
const saleEndLabel = computed(() => catalog.value?.sale_ends_at
  ? new Date(catalog.value.sale_ends_at).toLocaleString('zh-CN', { timeZone: 'Asia/Hong_Kong', hourCycle: 'h23' })
  : '')
const saleClosedReason = computed(() => {
  if (catalog.value?.sale_closed_reason) return catalog.value.sale_closed_reason
  if (catalog.value?.sale_ends_at && now.value >= Date.parse(catalog.value.sale_ends_at)) {
    return '本次限时售卖已结束，愿与你下次月圆再会'
  }
  return ''
})

async function loadCatalog() {
  loading.value = true
  try {
    const res = await store.fetchShopItems(props.building.id)
    if (res.code === 200 && res.data) {
      catalog.value = res.data as ShopCatalog
      if (catalog.value.server_now) {
        const serverNow = Date.parse(catalog.value.server_now)
        if (Number.isFinite(serverNow)) serverClockOffset = serverNow - Date.now()
        now.value = Date.now() + serverClockOffset
      }
    } else {
      message.error(res.message || '加载商品失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载商品失败')
  } finally {
    loading.value = false
  }
}

async function buyItem(item: ShopCatalogItem) {
  const adv = catalog.value?.adventurer
  if (!adv || buyingId.value != null || saleClosedReason.value) return
  buyingId.value = item.id
  try {
    const res = await store.buyFromShop(props.building.id, item.id, adv.id)
    if (res.code === 200) {
      const purchased = res.data?.purchased?.name ?? item.name
      const qty = res.data?.purchased?.quantity ?? item.quantity
      const qtyNote = qty && qty > 1 ? `×${qty}` : ''
      const dest = res.data?.purchased?.destination
      const destNote = dest === 'warehouse' ? '（已入仓库）' : ''
      message.success(`已购买「${purchased}」${qtyNote}${destNote}`)
      await Promise.all([
        loadCatalog(),
        store.fetchAdventurers(),
      ])
      emit('refreshed')
    } else {
      message.error(res.message || '购买失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '购买失败')
  } finally {
    buyingId.value = null
  }
}

onMounted(() => {
  void loadCatalog()
  clockTimer = setInterval(() => {
    const previous = now.value
    now.value = Date.now() + serverClockOffset
    const start = Date.parse(catalog.value?.sale_starts_at || '')
    if (previous < start && now.value >= start && !loading.value) void loadCatalog()
  }, 1000)
})
onUnmounted(() => { if (clockTimer) clearInterval(clockTimer) })
</script>

<style scoped>
.shop-panel {
  margin-bottom: 16px;
}
.shop-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.shop-wallet {
  font-size: 0.9em;
  color: var(--color-text-secondary);
}
.shop-hint {
  font-size: 0.85em;
  color: var(--color-text-secondary);
  margin: 0 0 12px;
}
.shop-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 260px), 1fr));
  gap: 10px;
}
.shop-card {
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 12px;
}
.shop-card.disabled {
  opacity: 0.75;
}
.shop-card-head {
  display: flex;
  flex-wrap: wrap;
  min-width: 0;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.shop-item-name {
  min-width: 0;
  overflow-wrap: anywhere;
  font-weight: 600;
}
.shop-item-qty {
  margin-left: 4px;
  font-weight: 500;
  color: var(--color-text-secondary);
}
.shop-item-meta {
  font-size: 0.8em;
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.shop-item-desc {
  font-size: 0.85em;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin-bottom: 10px;
}
.shop-card-foot {
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--color-border);
  flex-wrap: wrap;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.shop-price {
  font-weight: 600;
  color: var(--color-warning);
}
.shop-buy-btn {
  min-width: 72px;
}
.shop-reject-reason {
  margin-top: 8px;
  font-size: 0.8em;
  color: var(--color-danger);
}
.empty-helpers {
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 0.85em;
  padding: 12px;
}
.section-title {
  margin: 0;
  font-size: 1em;
}
@media (max-width: 640px) {
  .shop-card-head, .shop-card-foot { flex-wrap: wrap; gap: 8px; }
  .shop-item-meta { white-space: normal; }
  .shop-card-foot .shop-buy-btn { margin-left: auto; }
  .shop-item-desc { font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
}
</style>
