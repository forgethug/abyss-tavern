<template>
  <div class="readonly-story-panel">
    <p class="readonly-note">只读权限仅可按冒险者 ID 查看该角色的个人隐藏线。</p>
    <form class="lookup-row" @submit.prevent="loadThreads">
      <label for="readonly-story-adventurer-id">冒险者 ID</label>
      <input
        id="readonly-story-adventurer-id"
        v-model.trim="adventurerId"
        inputmode="numeric"
        pattern="[0-9]*"
        placeholder="输入冒险者 ID"
      >
      <button type="submit" class="btn-primary btn-sm" :disabled="loading || !adventurerId">
        {{ loading ? '查询中…' : '查询个人隐藏线' }}
      </button>
    </form>

    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <div v-else-if="loaded && threads.length === 0" class="empty">该冒险者暂无隐藏线</div>
    <div v-else-if="threads.length" class="thread-list">
      <article v-for="thread in threads" :key="thread.id" class="thread-card">
        <header>
          <strong>#{{ thread.id }} {{ thread.title }}</strong>
          <span class="status-tag">{{ statusLabel(thread.status) }}</span>
        </header>
        <p>{{ thread.summary }}</p>
        <div v-if="thread.tags?.length" class="tag-row">
          <span v-for="tag in thread.tags" :key="`${thread.id}-${tag}`">{{ tag }}</span>
        </div>
        <small>权重 {{ thread.weight }} · 最近更新 {{ formatTime(thread.last_touched_at) }}</small>
      </article>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'

interface StoryThreadRow {
  id: number
  title: string
  summary: string
  status: string
  weight: number
  tags?: string[]
  last_touched_at?: string | null
}

const store = useAdventurerS1Store()
const adventurerId = ref('')
const threads = ref<StoryThreadRow[]>([])
const loading = ref(false)
const loaded = ref(false)
const error = ref('')

function statusLabel(status: string) {
  return ({ active: '进行中', dormant: '休眠', resolved: '已收束', deleted: '已删除' } as Record<string, string>)[status] || status
}

function formatTime(value?: string | null) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

async function loadThreads() {
  if (!/^\d+$/.test(adventurerId.value) || Number(adventurerId.value) <= 0) {
    error.value = '请输入有效的冒险者 ID'
    return
  }
  loading.value = true
  loaded.value = false
  error.value = ''
  try {
    const response = await store.adminListStoryThreads(Number(adventurerId.value))
    if (response?.code !== 200) {
      error.value = response?.message || '个人隐藏线加载失败'
      threads.value = []
      return
    }
    threads.value = Array.isArray(response.data) ? response.data : []
    loaded.value = true
  } catch (requestError: any) {
    error.value = requestError?.response?.data?.message || '个人隐藏线加载失败'
    threads.value = []
  } finally {
    loading.value = false
  }
}
</script>

<style scoped lang="scss">
.readonly-story-panel {
  display: grid;
  gap: 14px;
}

.readonly-note,
.error-text,
.empty,
.thread-card p,
.thread-card small {
  margin: 0;
}

.readonly-note {
  color: var(--color-text-secondary);
}

.lookup-row {
  display: flex;
  align-items: end;
  gap: 10px;
  flex-wrap: wrap;

  label {
    display: grid;
    gap: 6px;
    color: var(--color-text-secondary);
    font-size: 13px;
  }

  input {
    min-width: 180px;
    padding: 8px 10px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 6px;
  }
}

.error-text {
  color: #ef4444;
}

.empty {
  padding: 24px;
  text-align: center;
  color: var(--color-text-secondary);
}

.thread-list {
  display: grid;
  gap: 10px;
}

.thread-card {
  display: grid;
  gap: 8px;
  padding: 14px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;

  header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  p,
  small {
    color: var(--color-text-secondary);
  }
}

.status-tag,
.tag-row span {
  padding: 2px 7px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  font-size: 12px;
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

@media (max-width: 600px) {
  .lookup-row {
    display: grid;

    input,
    button {
      width: 100%;
      box-sizing: border-box;
    }
  }
}
</style>
