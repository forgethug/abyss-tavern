<template>
  <div class="tombstone-panel">
    <div class="tombstone-header">
      <h5 class="section-title">🪦 英灵碑林</h5>
      <div class="tombstone-filters">
        <button
          type="button"
          class="filter-btn"
          :class="{ active: !mineOnly }"
          :disabled="loading"
          @click="setMineOnly(false)"
        >
          全部
        </button>
        <button
          type="button"
          class="filter-btn"
          :class="{ active: mineOnly }"
          :disabled="loading"
          @click="setMineOnly(true)"
        >
          我的
        </button>
      </div>
    </div>
    <p class="tombstone-hint">
      此处存放已在探险中阵亡的冒险者。点击墓碑可查看详情、留言与献花。
    </p>

    <form class="tombstone-search" role="search" @submit.prevent="submitSearch">
      <input
        v-model="searchDraft"
        type="search"
        maxlength="100"
        placeholder="搜索阵亡冒险者姓名"
        aria-label="搜索阵亡冒险者姓名"
      />
      <button type="submit" class="btn-secondary btn-sm" :disabled="loading">搜索</button>
      <button
        v-if="searchDraft || activeSearch"
        type="button"
        class="search-clear-btn"
        :disabled="loading"
        @click="clearSearch"
      >
        清除
      </button>
    </form>

    <div v-if="loading && items.length === 0" class="empty-helpers">加载中…</div>
    <div v-else-if="items.length === 0" class="empty-helpers">
      {{ emptyText }}
    </div>
    <div v-else class="tombstone-grid">
      <article
        v-for="stone in items"
        :key="stone.id"
        class="tombstone-grid-card"
        @click="openDetail(stone)"
      >
        <div class="tombstone-grid-icon">🪦</div>
        <button
          type="button"
          class="tombstone-grid-name"
          :aria-label="`查看${stone.name}的冒险者名片`"
          @click.stop="openAdventurerCard(stone)"
        >
          <span>{{ stone.name }}</span>
          <span class="tombstone-grid-name-mark" aria-hidden="true">↗</span>
        </button>
        <div class="tombstone-grid-sub">
          Lv.{{ stone.level }}
          <template v-if="stone.character_class"> · {{ stone.character_class }}</template>
        </div>
        <div class="tombstone-grid-badges">
          <span v-if="(stone.message_count ?? 0) > 0" class="badge">💬 {{ stone.message_count }}</span>
          <span v-if="(stone.tribute_count ?? 0) > 0" class="badge">🌸 {{ stone.tribute_count }}</span>
        </div>
      </article>
    </div>

    <div v-if="hasMore" class="tombstone-more">
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="loadMore">
        {{ loading ? '加载中…' : '加载更多墓碑' }}
      </button>
    </div>

    <TombstoneDetailModal
      v-if="detailStone"
      :building-id="buildingId"
      :tombstone="detailStone"
      @close="closeDetail"
      @tribute-posted="onTributePosted"
      @message-posted="onMessagePosted"
    />
    <AdventurerDetailModal
      v-if="detailAdventurerId != null"
      :adv-id="detailAdventurerId"
      :current-user-id="currentUserId"
      elevated
      @close="detailAdventurerId = null"
      @view-logs="openAdventurerLogs"
    />
    <AdventurerLogsModal
      v-if="logsAdventurer"
      :adv-id="logsAdventurer.id"
      :adv-name="logsAdventurer.name"
      @close="logsAdventurer = null"
      @open-log="openExpeditionLog"
    />
    <ExpeditionLogModal
      v-if="expeditionLogId != null"
      :key="expeditionLogId"
      :log-id="expeditionLogId"
      @close="expeditionLogId = null"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue'
import { useAdventurerS1Store, type TombstoneItem } from '../stores/adventurerS1'
import { useUserStore } from '@/stores/user'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import AdventurerDetailModal from './AdventurerDetailModal.vue'
import AdventurerLogsModal from './AdventurerLogsModal.vue'
import ExpeditionLogModal from './ExpeditionLogModal.vue'
import TombstoneDetailModal from './TombstoneDetailModal.vue'

const props = defineProps<{
  buildingId: number
}>()

const store = useAdventurerS1Store()
const userStore = useUserStore()
const message = useGlobalMessage()

const items = ref<TombstoneItem[]>([])
const loading = ref(false)
const page = ref(1)
const hasMore = ref(false)
const mineOnly = ref(false)
const searchDraft = ref('')
const activeSearch = ref('')
const detailStone = ref<TombstoneItem | null>(null)
const detailAdventurerId = ref<number | null>(null)
const logsAdventurer = ref<{ id: number; name: string } | null>(null)
const expeditionLogId = ref<number | null>(null)
const currentUserId = computed(() => userStore.user?.id ?? null)
const emptyText = computed(() => {
  if (activeSearch.value) return `未找到姓名与「${activeSearch.value}」匹配的阵亡冒险者`
  return mineOnly.value ? '你名下尚无阵亡冒险者' : '碑林尚空，愿无人再入此列'
})

async function fetchPage(reset = false) {
  if (loading.value) return
  if (reset) {
    items.value = []
    hasMore.value = false
  }
  loading.value = true
  try {
    const targetPage = reset ? 1 : page.value
    const res = await store.fetchTombstones(props.buildingId, {
      page: targetPage,
      mineOnly: mineOnly.value,
      search: activeSearch.value || undefined,
    })
    if (res.code !== 200 || !res.data) {
      message.error(res.message || '加载墓碑失败')
      return
    }
    const batch = (res.data.items ?? []) as TombstoneItem[]
    items.value = reset ? batch : [...items.value, ...batch]
    hasMore.value = !!res.data.has_more
    page.value = targetPage + 1
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载墓碑失败')
  } finally {
    loading.value = false
  }
}

function openDetail(stone: TombstoneItem) {
  detailStone.value = stone
}

function openAdventurerCard(stone: TombstoneItem) {
  detailAdventurerId.value = stone.adventurer_id
}

function openAdventurerLogs(adventurer: { id: number; name: string }) {
  // The elevated detail card sits above ordinary modals, so close it before
  // opening the log list instead of leaving the requested modal hidden behind it.
  detailAdventurerId.value = null
  logsAdventurer.value = adventurer
}

function openExpeditionLog(logId: number) {
  expeditionLogId.value = logId
}

function closeDetail() {
  detailStone.value = null
}

function onTributePosted(payload: { stoneId: number; flowerSummary: any[] }) {
  const stone = items.value.find(s => s.id === payload.stoneId)
  if (!stone) return
  const total = (payload.flowerSummary ?? []).reduce(
    (sum: number, f: any) => sum + (f.total_quantity || 0), 0,
  )
  stone.tribute_count = total
  stone.tribute_summary = payload.flowerSummary
}

function onMessagePosted(payload: { stoneId: number }) {
  const stone = items.value.find(s => s.id === payload.stoneId)
  if (!stone) return
  stone.message_count = (stone.message_count ?? 0) + 1
}

function setMineOnly(value: boolean) {
  if (mineOnly.value === value) return
  mineOnly.value = value
  page.value = 1
  fetchPage(true)
}

function submitSearch() {
  activeSearch.value = searchDraft.value.trim()
  page.value = 1
  detailStone.value = null
  fetchPage(true)
}

function clearSearch() {
  searchDraft.value = ''
  if (!activeSearch.value) return
  activeSearch.value = ''
  page.value = 1
  detailStone.value = null
  fetchPage(true)
}

function loadMore() {
  if (!hasMore.value || loading.value) return
  fetchPage(false)
}

watch(() => props.buildingId, () => {
  page.value = 1
  items.value = []
  searchDraft.value = ''
  activeSearch.value = ''
  fetchPage(true)
})

onMounted(() => {
  fetchPage(true)
})
</script>

<style scoped>
.tombstone-panel {
  margin-top: 1rem;
}

.tombstone-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.tombstone-filters {
  display: flex;
  gap: 0.35rem;
}

.filter-btn {
  border: 1px solid var(--color-border);
  background: var(--color-bg);
  color: var(--color-text);
  border-radius: 999px;
  padding: 0.2rem 0.65rem;
  font-size: 0.82rem;
  cursor: pointer;
}

.filter-btn.active {
  background: var(--color-surface);
  border-color: var(--color-text-muted, var(--color-border));
}

.filter-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.tombstone-hint {
  margin: 0.5rem 0 0.85rem;
  color: var(--color-text-muted, #888);
  font-size: 0.88rem;
  line-height: 1.5;
}

.tombstone-search {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  margin-bottom: 0.85rem;
  min-width: 0;
}

.tombstone-search input {
  flex: 1;
  min-width: 0;
  padding: 0.42rem 0.65rem;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
}

.search-clear-btn {
  border: 0;
  background: transparent;
  color: var(--color-text-muted, #888);
  cursor: pointer;
  font-size: 0.82rem;
  padding: 0.35rem 0.2rem;
}

.search-clear-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

@media (max-width: 480px) {
  .tombstone-search {
    flex-wrap: wrap;
  }

  .tombstone-search input {
    flex-basis: 100%;
  }
}

.tombstone-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 0.75rem;
}

.tombstone-grid-card {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 0.75rem 0.5rem;
  background: var(--color-bg);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.3rem;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.1s;
  text-align: center;
}

.tombstone-grid-card:hover {
  border-color: var(--color-text-muted, var(--color-border));
  transform: translateY(-2px);
}

.tombstone-grid-icon {
  font-size: 2rem;
  line-height: 1;
}

.tombstone-grid-name {
  display: inline-flex;
  align-items: center;
  gap: 0.28rem;
  max-width: 100%;
  padding: 0.08rem 0.18rem 0.12rem;
  border: 0;
  border-bottom: 1px solid color-mix(in srgb, var(--color-text-muted, #888) 42%, transparent);
  background: transparent;
  color: var(--color-text);
  font-weight: 600;
  font-size: 0.92rem;
  word-break: break-word;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease, text-shadow 0.15s ease;
}

.tombstone-grid-name:hover,
.tombstone-grid-name:focus-visible {
  border-color: var(--color-primary, #6f9bc5);
  color: var(--color-primary, #6f9bc5);
  text-shadow: 0 0 12px color-mix(in srgb, var(--color-primary, #6f9bc5) 22%, transparent);
  outline: none;
}

.tombstone-grid-name-mark {
  flex: none;
  font-size: 0.68rem;
  opacity: 0.66;
  transform: translateY(-0.08rem);
}

.tombstone-grid-sub {
  font-size: 0.78rem;
  color: var(--color-text-muted, #888);
}

.tombstone-grid-badges {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
  justify-content: center;
  margin-top: 0.2rem;
}

.badge {
  font-size: 0.72rem;
  padding: 0.1rem 0.4rem;
  border-radius: 999px;
  background: var(--color-surface, rgba(127, 127, 127, 0.08));
  color: var(--color-text-muted, #888);
}

.tombstone-more {
  margin-top: 0.85rem;
  text-align: center;
}
</style>
