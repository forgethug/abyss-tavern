<template>
  <Teleport to="body">
  <div class="modal-overlay s1-mobile-surface s1-mobile-overlay" :class="{ elevated }" @click.self="emit('close')">
    <div class="modal-card s1-mobile-dialog">
      <header class="modal-head s1-mobile-dialog-header">
        <h3 class="modal-title">📜 {{ advName }} 的探险日志</h3>
        <button type="button" class="modal-close" @click="emit('close')">✕</button>
      </header>

      <div class="modal-body s1-mobile-dialog-body">
        <div class="log-toolbar">
          <input
            v-model="search"
            type="search"
            class="log-search-input"
            placeholder="搜索日志…"
            aria-label="搜索探险日志"
            @input="onSearchInput"
          />
          <select v-model="statusFilter" class="log-status-select" aria-label="日志状态" @change="fetchPage(1)">
            <option value="exclude_failed">隐藏失败</option>
            <option value="">全部状态（含失败）</option>
            <option value="completed">已完成</option>
            <option value="failed">失败</option>
            <option value="in_progress">进行中</option>
            <option value="generating">生成中</option>
          </select>
          <select v-model="categoryFilter" class="log-status-select" aria-label="日志分类" @change="fetchPage(1)">
            <option value="">全部日志</option>
            <option value="special">✨ 特殊事件</option>
            <option value="favorite">⭐ 我的收藏</option>
          </select>
        </div>

        <div v-if="loading" class="state">加载中…</div>
        <div v-else-if="logs.length === 0" class="state">
          {{ statusFilter === 'exclude_failed' ? '暂无非失败日志，可从状态选项查看失败日志' : '暂无探险日志' }}
        </div>
        <div v-else class="log-list">
          <div
            v-for="log in logs"
            :key="log.id"
            class="log-card"
            :class="isSpecialEventPresentationStatus(log.status) ? specialEventCardClass(log.special_event_theme) : null"
            :style="isSpecialEventPresentationStatus(log.status) ? specialEventStyle(log.special_event_theme) : undefined"
            @click="emit('open-log', log.id)"
          >
            <div class="log-card-head">
              <span class="log-where">
                <span v-if="log.party_name" class="log-party">🏰 {{ log.party_name }}</span>
                <span v-if="log.status === 'in_progress' || log.status === 'completed'">🗺️ {{ log.temporary_location_name || log.hidden_dungeon || log.target_dungeon }}</span>
              </span>
              <span class="log-status-wrap">
                <span class="log-status" :class="'st-' + log.status">{{ logStatusLabel(log.status) }}</span>
              </span>
            </div>
            <span
              v-if="isSpecialEventPresentationStatus(log.status) && getSpecialEventPresentation(log.special_event_theme)"
              class="log-special-event"
            >
              {{ getSpecialEventPresentation(log.special_event_theme)?.icon }}
              {{ getSpecialEventPresentation(log.special_event_theme)?.label }}
            </span>
            <p v-if="log.result_summary" class="log-summary">{{ log.result_summary }}</p>
            <div class="log-foot">
              <span>日志 #{{ log.id }}</span>
              <span>{{ log.created_at ? formatDateTime(log.created_at) : '' }}</span>
              <button
                type="button"
                class="log-favorite-btn"
                :class="{ active: log.is_favorited }"
                :disabled="favoriteLoading === log.id"
                :aria-pressed="log.is_favorited"
                :title="log.is_favorited ? '取消收藏' : '收藏日志'"
                @click.stop="toggleFavorite(log)"
              >
                {{ favoriteLoading === log.id ? '…' : (log.is_favorited ? '★ 已收藏' : '☆ 收藏') }}
              </button>
            </div>
          </div>
        </div>
        <Pagination v-if="pagination" :pagination="pagination" @go="fetchPage" />
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useAdventurerS1Store, type ExpeditionLogSummary, type PaginationMeta } from '../stores/adventurerS1'
import {
  getSpecialEventPresentation,
  isSpecialEventPresentationStatus,
  logStatusLabel,
  specialEventCardClass,
  specialEventStyle,
} from '../utils'
import { formatDateTime } from '@/utils/time'
import Pagination from '@/components/Pagination.vue'

const props = withDefaults(defineProps<{
  advId: number
  advName: string
  /** 叠在探险日志内打开的冒险者名片之上 */
  elevated?: boolean
}>(), {
  elevated: false,
})
const emit = defineEmits<{ close: []; 'open-log': [number] }>()

const store = useAdventurerS1Store()
const logs = ref<ExpeditionLogSummary[]>([])
const pagination = ref<PaginationMeta | null>(null)
const loading = ref(true)
const search = ref('')
type LogStatusFilter = 'exclude_failed' | '' | 'completed' | 'failed' | 'in_progress' | 'generating'
const statusFilter = ref<LogStatusFilter>('exclude_failed')
const categoryFilter = ref<'' | 'special' | 'favorite'>('')
const favoriteLoading = ref<number | null>(null)
let searchTimer: ReturnType<typeof setTimeout> | null = null
let fetchSeq = 0

function statusRequestParams(): { status?: string; exclude_failed?: boolean } {
  if (statusFilter.value === 'exclude_failed') return { exclude_failed: true }
  return { status: statusFilter.value || undefined }
}

async function fetchPage(page: number) {
  const seq = ++fetchSeq
  loading.value = true
  try {
    const res = await store.fetchAdventurerLogs({
      page,
      adventurer_id: props.advId,
      search: search.value.trim() || undefined,
      ...statusRequestParams(),
      category: categoryFilter.value || undefined,
    })
    if (seq === fetchSeq) {
      logs.value = res.items
      pagination.value = res.pagination
    }
  } finally {
    if (seq === fetchSeq) loading.value = false
  }
}

async function toggleFavorite(log: ExpeditionLogSummary) {
  if (favoriteLoading.value !== null) return
  favoriteLoading.value = log.id
  const nextValue = !log.is_favorited
  try {
    const res = await store.setExpeditionLogFavorite(log.id, nextValue)
    if (res.code !== 200) return
    log.is_favorited = nextValue
    if (categoryFilter.value === 'favorite' && !nextValue) {
      const page = pagination.value?.current_page ?? 1
      await fetchPage(page)
      if (logs.value.length === 0 && page > 1) await fetchPage(page - 1)
    }
  } finally {
    favoriteLoading.value = null
  }
}

function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    searchTimer = null
    void fetchPage(1)
  }, 300)
}

onMounted(() => fetchPage(1))
onUnmounted(() => {
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<style scoped lang="scss">
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(0, 0, 0, 0.55);

  &.elevated {
    z-index: 1500;
  }
}

.modal-card {
  width: 100%;
  max-width: 720px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  overflow: hidden;
}

.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border);
}

.modal-title {
  margin: 0;
  font-size: 16px;
  color: var(--color-text);
}

.modal-close {
  border: none;
  background: transparent;
  font-size: 18px;
  color: var(--color-text-secondary);
  cursor: pointer;

  &:hover {
    color: var(--color-text);
  }
}

.modal-body {
  padding: 16px 18px;
  overflow-y: auto;
}

.state {
  padding: 24px 0;
  text-align: center;
  color: var(--color-text-secondary);
}

.log-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.log-search-input {
  flex: 1;
  min-width: 0;
  padding: 6px 12px;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-text);
  outline: none;
  transition: border-color 0.15s;

  &:focus {
    border-color: var(--color-primary);
  }

  &::placeholder {
    color: var(--color-text-secondary);
  }
}

.log-status-select {
  padding: 6px 10px;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-text);
  outline: none;
  cursor: pointer;
}

.log-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 10px;
}

.log-card {
  padding: 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 0.15s;

  &:hover {
    border-color: var(--color-primary);
  }
}

.log-card[class*='se-theme-'] {
  position: relative;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--se-accent) 58%, var(--color-border));
  background:
    linear-gradient(105deg, color-mix(in srgb, var(--se-accent) 10%, transparent), transparent 52%),
    var(--color-bg);
  box-shadow: inset 4px 0 0 var(--se-accent), 0 4px 14px color-mix(in srgb, var(--se-accent) 10%, transparent);
}

.log-special-event {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 6px;
  padding: 2px 8px;
  border: 1px solid color-mix(in srgb, var(--se-accent) 50%, var(--color-border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--se-accent) 10%, var(--color-surface));
  color: var(--se-accent);
  font-size: 11px;
  font-weight: 700;
}

.log-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}

.log-where {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
  color: var(--color-text);
}

.log-party {
  color: var(--color-text-secondary);
}

.log-status {
  flex: none;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);

  &.st-completed {
    color: var(--color-success);
    border-color: var(--color-success);
  }

  &.st-failed {
    color: var(--color-danger);
    border-color: var(--color-danger);
  }

  &.st-generating,
  &.st-in_progress {
    color: var(--color-primary);
    border-color: var(--color-primary);
  }
}

.log-status-wrap {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 4px;
}

.log-summary {
  margin: 0 0 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.log-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.log-favorite-btn {
  margin-left: auto;
  padding: 1px 8px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;

  &.active {
    border-color: #d6a318;
    color: #d6a318;
  }

  &:disabled {
    cursor: wait;
    opacity: 0.65;
  }
}

/* ── 移动端适配 ── */
@media (max-width: 640px) {
  .modal-overlay {
    padding: 8px;
  }

  .modal-card {
    max-height: 92vh;
  }

  .modal-body {
    padding: 12px 14px;
  }

  .log-toolbar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .log-search-input { grid-column: 1 / -1; }
  .log-foot { flex-wrap: wrap; gap: 6px 10px; }

  /* iOS Safari 表单控件字号 <16px 聚焦时会强制放大页面 */
  .log-search-input,
  .log-status-select {
    width: 100%;
    min-width: 0;
    min-height: 36px;
    padding: 7px 10px;
    font-size: 16px;
  }
}
</style>
