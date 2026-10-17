<template>
  <Teleport to="body">
    <div class="tribute-modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="$emit('close')">
      <div class="tribute-modal s1-mobile-dialog">
      <!-- 头部 -->
      <div class="tribute-modal-header s1-mobile-dialog-header">
        <div class="tribute-modal-title">
          <span class="tribute-modal-icon">🪦</span>
          <span>{{ tombstone.name }}</span>
          <span class="tribute-modal-sub">
            Lv.{{ tombstone.level }}
            <template v-if="tombstone.character_class"> · {{ tombstone.character_class }}</template>
          </span>
        </div>
        <button type="button" class="tribute-modal-close" @click="$emit('close')">×</button>
      </div>

        <div class="tribute-modal-body s1-mobile-dialog-body">
        <!-- 墓志铭 -->
        <div class="tribute-epitaph-section">
          <p class="tribute-epitaph">{{ tombstone.epitaph }}</p>
          <div v-if="tombstone.died_at" class="tribute-died-at">
            阵亡于 {{ formatDate(tombstone.died_at) }}
          </div>
        </div>

        <!-- 献花汇总 -->
        <div v-if="flowerSummary.length" class="tribute-summary-section">
          <div
            v-for="f in flowerSummary"
            :key="f.code"
            class="tribute-summary-badge"
          >
            {{ f.emoji }} {{ f.name }} ×{{ f.total_quantity }}
          </div>
        </div>

        <!-- 献花记录 -->
        <div class="tribute-section">
          <div class="tribute-section-title">
            🌸 献花记录
            <span v-if="tributeTotal > 0" class="tribute-count">({{ tributeTotal }})</span>
          </div>
          <div v-if="tributeLoading && !tributes.length" class="tribute-empty">加载中…</div>
          <div v-else-if="!tributes.length" class="tribute-empty">暂无献花</div>
          <ul v-else class="tribute-list">
            <li v-for="t in tributes" :key="t.id" class="tribute-item">
              <div class="tribute-item-head">
                <span class="tribute-item-user">{{ t.adventurer_name || '已离开的冒险者' }}</span>
                <span class="tribute-item-flower">{{ t.flower_emoji }} {{ t.flower_name }} ×{{ t.quantity }}</span>
                <span v-if="t.created_at" class="tribute-item-time">{{ formatDate(t.created_at) }}</span>
              </div>
              <p v-if="t.message" class="tribute-item-message">{{ t.message }}</p>
            </li>
          </ul>
          <button
            v-if="tributeHasMore"
            type="button"
            class="btn-link load-more-btn"
            :disabled="tributeLoading"
            @click="loadMoreTributes"
          >
            {{ tributeLoading ? '加载中…' : '加载更多' }}
          </button>

          <!-- 献花操作 -->
          <div class="tribute-compose">
            <div class="tribute-flower-note">
              鲜花仅作墓碑纪念展示，每束会在 {{ flowerLifetimeHours }} 小时后枯萎，不提供任何属性或玩法效果。
            </div>
            <div v-if="!shopBuilt" class="tribute-empty">
              {{ shopMessage || '鲜花店尚未建成' }}
            </div>
            <div v-else-if="!adventurers.length" class="tribute-empty">
              你没有可支付鲜花费用的冒险者
            </div>
            <template v-else>
              <div class="tribute-compose-row">
                <select v-model="tributeAdvId" class="tribute-select">
                  <option v-for="adv in adventurers" :key="adv.id" :value="adv.id">
                    {{ adv.name }}（{{ adv.gold.toLocaleString() }} 金币）
                  </option>
                </select>
                <select v-model="tributeFlowerCode" class="tribute-select">
                  <option value="">选择鲜花价位</option>
                  <option
                    v-for="f in flowerCatalog"
                    :key="f.code"
                    :value="f.code"
                  >
                    {{ f.emoji }} {{ f.name }} · {{ f.price.toLocaleString() }} 金币
                  </option>
                </select>
              </div>
              <div v-if="selectedFlower" class="tribute-flower-description">
                {{ selectedFlower.description }}
              </div>
              <textarea
                v-model="tributeMessage"
                class="tribute-message-input"
                rows="2"
                maxlength="100"
                placeholder="写下寄语（可选）…"
              />
              <button
                type="button"
                class="btn-primary btn-sm tribute-submit-btn"
                :disabled="tributePosting || !canSubmitTribute"
                @click="submitTribute"
              >
                {{ tributePosting ? '送花中…' : tributeButtonLabel }}
              </button>
            </template>
          </div>
        </div>

        <!-- 留言区 -->
        <div class="tribute-section">
          <div class="tribute-section-title">
            💬 留言
            <span v-if="messageTotal > 0" class="tribute-count">({{ messageTotal }})</span>
          </div>
          <div v-if="messageLoading && !messages.length" class="tribute-empty">加载中…</div>
          <div v-else-if="!messages.length" class="tribute-empty">暂无留言</div>
          <ul v-else class="tribute-list">
            <li v-for="msg in messages" :key="msg.id" class="tribute-item">
              <div class="tribute-item-head">
                <span class="tribute-item-user">{{ msg.nickname }}</span>
                <span v-if="msg.created_at" class="tribute-item-time">{{ formatDate(msg.created_at) }}</span>
              </div>
              <p class="tribute-item-message">{{ msg.content }}</p>
            </li>
          </ul>
          <button
            v-if="messageHasMore"
            type="button"
            class="btn-link load-more-btn"
            :disabled="messageLoading"
            @click="loadMoreMessages"
          >
            {{ messageLoading ? '加载中…' : '加载更多' }}
          </button>

          <div class="tribute-compose">
            <textarea
              v-model="messageDraft"
              class="tribute-message-input"
              rows="2"
              maxlength="200"
              placeholder="写下你的追思或告别…"
            />
            <button
              type="button"
              class="btn-primary btn-sm tribute-submit-btn"
              :disabled="messagePosting || !messageDraft.trim()"
              @click="submitMessage"
            >
              {{ messagePosting ? '发送中…' : '留言' }}
            </button>
          </div>
        </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  useAdventurerS1Store,
  type TombstoneItem,
  type TombstoneMessage,
  type TombstoneTribute,
  type TributeFlowerSummary,
  type FlowerCatalogItem,
  type FlowerShopAdventurer,
} from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'

const props = defineProps<{
  buildingId: number
  tombstone: TombstoneItem
}>()

const emit = defineEmits<{
  close: []
  'tribute-posted': [{ stoneId: number; flowerSummary: TributeFlowerSummary[] }]
  'message-posted': [{ stoneId: number }]
}>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()

// ── 留言 ──
const messages = ref<TombstoneMessage[]>([])
const messageLoading = ref(false)
const messagePage = ref(1)
const messageTotal = ref(0)
const messageHasMore = ref(false)
const messageDraft = ref('')
const messagePosting = ref(false)

// ── 献花记录 ──
const tributes = ref<TombstoneTribute[]>([])
const tributeLoading = ref(false)
const tributePage = ref(1)
const tributeTotal = ref(0)
const tributeHasMore = ref(false)
const flowerSummary = ref<TributeFlowerSummary[]>([])

// ── 献花表单 ──
const adventurers = ref<FlowerShopAdventurer[]>([])
const flowerCatalog = ref<FlowerCatalogItem[]>([])
const tributeAdvId = ref<number | null>(null)
const tributeFlowerCode = ref('')
const tributeMessage = ref('')
const tributePosting = ref(false)
const shopBuilt = ref(false)
const shopMessage = ref('鲜花店尚未建成')
const flowerLifetimeHours = ref(24)

const selectedAdventurer = computed(() => (
  adventurers.value.find(adv => adv.id === tributeAdvId.value) ?? null
))
const selectedFlower = computed(() => (
  flowerCatalog.value.find(flower => flower.code === tributeFlowerCode.value) ?? null
))
const canSubmitTribute = computed(() => (
  shopBuilt.value
  && !!selectedAdventurer.value
  && !!selectedFlower.value
  && selectedAdventurer.value.gold >= selectedFlower.value.price
))
const tributeButtonLabel = computed(() => {
  if (!selectedFlower.value) return '选择鲜花后送出'
  if (selectedAdventurer.value && selectedAdventurer.value.gold < selectedFlower.value.price) {
    return '金币不足'
  }
  return `送花（${selectedFlower.value.price.toLocaleString()} 金币）`
})

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// ── 数据加载 ──

async function fetchMessages(reset = false) {
  if (messageLoading.value) return
  messageLoading.value = true
  try {
    const targetPage = reset ? 1 : messagePage.value
    const res = await store.fetchTombstoneMessages(props.buildingId, props.tombstone.id, {
      page: targetPage,
      pageSize: 20,
    })
    if (res.code !== 200 || !res.data) {
      message.error(res.message || '加载留言失败')
      return
    }
    const batch = (res.data.items ?? []) as TombstoneMessage[]
    messages.value = reset ? batch : [...messages.value, ...batch]
    messageTotal.value = res.data.total ?? 0
    messageHasMore.value = !!res.data.has_more
    messagePage.value = targetPage + 1
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载留言失败')
  } finally {
    messageLoading.value = false
  }
}

async function fetchTributes(reset = false) {
  if (tributeLoading.value) return
  tributeLoading.value = true
  try {
    const targetPage = reset ? 1 : tributePage.value
    const res = await store.fetchTombstoneTributes(props.buildingId, props.tombstone.id, {
      page: targetPage,
      pageSize: 20,
    })
    if (res.code !== 200 || !res.data) {
      message.error(res.message || '加载献花记录失败')
      return
    }
    const batch = (res.data.items ?? []) as TombstoneTribute[]
    tributes.value = reset ? batch : [...tributes.value, ...batch]
    tributeTotal.value = res.data.total ?? 0
    tributeHasMore.value = !!res.data.has_more
    flowerSummary.value = (res.data.flower_summary ?? []) as TributeFlowerSummary[]
    tributePage.value = targetPage + 1
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载献花记录失败')
  } finally {
    tributeLoading.value = false
  }
}

async function loadMoreMessages() {
  fetchMessages(false)
}

async function loadMoreTributes() {
  fetchTributes(false)
}

// ── 提交 ──

async function submitMessage() {
  const content = messageDraft.value.trim()
  if (!content || messagePosting.value) return
  messagePosting.value = true
  try {
    const res = await store.postTombstoneMessage(props.buildingId, props.tombstone.id, content)
    if (res.code !== 200 || !res.data) {
      message.error(res.message || '留言失败')
      return
    }
    const posted = res.data as TombstoneMessage
    messages.value = [...messages.value, posted]
    messageTotal.value += 1
    messageDraft.value = ''
    emit('message-posted', { stoneId: props.tombstone.id })
    message.success('留言已发布')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '留言失败')
  } finally {
    messagePosting.value = false
  }
}

async function submitTribute() {
  if (!tributeAdvId.value || !selectedFlower.value || !canSubmitTribute.value || tributePosting.value) return
  tributePosting.value = true
  try {
    const res = await store.postTombstoneTribute(props.buildingId, props.tombstone.id, {
      adventurer_id: tributeAdvId.value,
      flower_code: selectedFlower.value.code,
      quantity: 1,
      message: tributeMessage.value.trim() || undefined,
    })
    if (res.code !== 200 || !res.data) {
      message.error(res.message || '献花失败')
      return
    }
    message.success(`鲜花已送到墓碑前，将在 ${flowerLifetimeHours.value} 小时后枯萎`)
    tributeMessage.value = ''
    tributeFlowerCode.value = ''
    const payer = adventurers.value.find(adv => adv.id === tributeAdvId.value)
    if (payer && typeof res.data.remaining_gold === 'number') {
      payer.gold = res.data.remaining_gold
    }
    await fetchTributes(true)
    emit('tribute-posted', {
      stoneId: props.tombstone.id,
      flowerSummary: flowerSummary.value,
    })
  } catch (e: any) {
    message.error(e?.response?.data?.message || '献花失败')
  } finally {
    tributePosting.value = false
  }
}

onMounted(async () => {
  const [, , catalogRes] = await Promise.all([
    fetchMessages(true),
    fetchTributes(true),
    store.fetchFlowerCatalog(),
  ])
  if (catalogRes?.code === 200 && catalogRes.data) {
    const payload = catalogRes.data as {
      catalog: FlowerCatalogItem[]
      adventurers: FlowerShopAdventurer[]
      shop_built?: boolean
      shop_message?: string | null
      flower_lifetime_hours?: number
    }
    flowerCatalog.value = payload.catalog ?? []
    adventurers.value = payload.adventurers ?? []
    shopBuilt.value = payload.shop_built === true
    shopMessage.value = payload.shop_message || '鲜花店尚未建成'
    flowerLifetimeHours.value = payload.flower_lifetime_hours ?? 24
  }
  if (adventurers.value.length && tributeAdvId.value === null) {
    tributeAdvId.value = adventurers.value[0].id
  }
})
</script>

<style scoped>
.tribute-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.tribute-modal {
  width: 92%;
  max-width: 520px;
  max-height: 80vh;
  background: var(--color-bg, #fff);
  border-radius: 12px;
  border: 1px solid var(--color-border);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.tribute-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--color-border);
}

.tribute-modal-title {
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  font-weight: 600;
  font-size: 1.05rem;
}

.tribute-modal-icon {
  font-size: 1.2rem;
}

.tribute-modal-sub {
  font-weight: 400;
  font-size: 0.82rem;
  color: var(--color-text-muted, #888);
}

.tribute-modal-close {
  border: none;
  background: none;
  font-size: 1.4rem;
  line-height: 1;
  cursor: pointer;
  color: var(--color-text-muted, #888);
}

.tribute-modal-body {
  padding: 1rem;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.tribute-epitaph-section {
  padding: 0.65rem 0.75rem;
  border-radius: 8px;
  background: var(--color-surface, rgba(127, 127, 127, 0.08));
}

.tribute-epitaph {
  margin: 0;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.92rem;
}

.tribute-died-at {
  margin-top: 0.4rem;
  font-size: 0.78rem;
  color: var(--color-text-muted, #888);
}

.tribute-summary-section {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.tribute-summary-badge {
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  background: var(--color-surface, rgba(127, 127, 127, 0.08));
  font-size: 0.82rem;
}

.tribute-section {
  border-top: 1px dashed var(--color-border);
  padding-top: 0.75rem;
}

.tribute-section-title {
  font-size: 0.92rem;
  font-weight: 600;
  margin-bottom: 0.5rem;
}

.tribute-count {
  font-weight: 400;
  color: var(--color-text-muted, #888);
}

.tribute-empty {
  font-size: 0.82rem;
  color: var(--color-text-muted, #888);
  margin-bottom: 0.5rem;
}

.tribute-list {
  list-style: none;
  margin: 0 0 0.5rem;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.tribute-item {
  padding: 0.45rem 0.55rem;
  border-radius: 8px;
  background: var(--color-surface, rgba(127, 127, 127, 0.08));
}

.tribute-item-head {
  display: flex;
  align-items: baseline;
  gap: 0.3rem;
  flex-wrap: wrap;
  margin-bottom: 0.2rem;
}

.tribute-item-user {
  font-size: 0.82rem;
  font-weight: 600;
}

.tribute-item-adv {
  font-size: 0.78rem;
  color: var(--color-text-muted, #888);
}

.tribute-item-flower {
  font-size: 0.82rem;
  margin-left: auto;
}

.tribute-item-time {
  font-size: 0.72rem;
  color: var(--color-text-muted, #888);
}

.tribute-item-message {
  margin: 0.15rem 0 0;
  font-size: 0.85rem;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.load-more-btn {
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text);
  font-size: 0.82rem;
  cursor: pointer;
  text-decoration: underline;
  margin-bottom: 0.5rem;
}

.tribute-compose {
  margin-top: 0.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.tribute-flower-note,
.tribute-flower-description {
  padding: 0.5rem 0.6rem;
  border-radius: 8px;
  background: var(--color-surface, rgba(127, 127, 127, 0.08));
  color: var(--color-text-muted, #777);
  font-size: 0.82rem;
  line-height: 1.5;
}

.tribute-flower-description {
  padding: 0.35rem 0.5rem;
}

.tribute-compose-row {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}

.tribute-select {
  flex: 1;
  min-width: 0;
  box-sizing: border-box;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 0.4rem 0.5rem;
  background: var(--color-bg);
  color: var(--color-text);
  font: inherit;
  font-size: 0.85rem;
}

.tribute-qty-input {
  width: 3.5rem;
  text-align: center;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 0.35rem 0.4rem;
  background: var(--color-bg);
  color: var(--color-text);
  font: inherit;
  font-size: 0.85rem;
}

.tribute-message-input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 0.45rem 0.55rem;
  background: var(--color-bg);
  color: var(--color-text);
  font: inherit;
  font-size: 0.85rem;
  resize: vertical;
}

.tribute-submit-btn {
  align-self: flex-end;
}
</style>
