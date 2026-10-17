<template>
  <section class="appearance-history" aria-label="我的出场历程" :aria-busy="loading">
    <header class="history-header">
      <div>
        <h3>我的出场历程 <span v-if="history">{{ history.total }} 次</span></h3>
        <p>等待入驻的日子里，你也可能与探险队伍在旅途中相遇。</p>
      </div>
      <button type="button" :disabled="loading" @click="load(history?.page ?? 1)">
        {{ loading ? '翻阅中…' : '刷新' }}
      </button>
    </header>

    <div v-if="error" class="history-state" role="alert">
      {{ error }}
      <button type="button" :disabled="loading" @click="load(requestedPage)">重试</button>
    </div>
    <p v-else-if="loading && !history" class="history-state" role="status">正在翻阅旅途记录…</p>
    <p v-else-if="history?.total === 0" class="history-state">
      暂无出场记录。旅途结束后，属于你的相遇片段会记在这里。
    </p>

    <ol v-if="history?.items.length" class="history-timeline">
      <li v-for="(appearance, index) in history.items" :key="`${history.page}:${index}`">
        <div class="appearance-heading">
          <strong>{{ appearance.location }}</strong>
          <time :datetime="appearance.appeared_at">{{ formatTime(appearance.appeared_at) }}</time>
        </div>
        <div class="appearance-meta">
          <span v-if="appearance.party_name">相遇队伍：{{ appearance.party_name }}</span>
          <span>第 {{ appearance.incarnation }} 世</span>
        </div>
        <blockquote v-for="(excerpt, excerptIndex) in appearance.excerpts" :key="excerptIndex">
          {{ excerpt }}
        </blockquote>
      </li>
    </ol>

    <footer v-if="history && history.pages > 1" class="history-pagination">
      <button type="button" :disabled="loading || !history.has_prev" @click="load(history.page - 1)">上一页</button>
      <span>第 {{ history.page }} / {{ history.pages }} 页</span>
      <button type="button" :disabled="loading || !history.has_next" @click="load(history.page + 1)">下一页</button>
    </footer>
    <p v-if="history && history.total > 0" class="history-note">按探险时间排列，同一场相遇的片段合并展示。</p>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useAdventurerS1Store, type ReservationAppearanceHistory } from '../stores/adventurerS1'

const store = useAdventurerS1Store()
const history = ref<ReservationAppearanceHistory | null>(null)
const loading = ref(false)
const error = ref('')
const requestedPage = ref(1)
let disposed = false

async function load(page: number) {
  if (loading.value) return
  loading.value = true
  error.value = ''
  requestedPage.value = page
  const owner = store.squadSessionUserId
  const realm = store.realmViewCode
  const reservation = store.myReservation?.id
  const isCurrent = () => !disposed && owner === store.squadSessionUserId
    && realm === store.realmViewCode && reservation === store.myReservation?.id
  try {
    const response = await store.fetchReservationAppearances(page)
    if (!isCurrent()) return
    if (response.code === 200 && response.data) {
      history.value = response.data
    } else {
      error.value = '暂时无法翻阅出场历程，请稍后重试。'
    }
  } catch {
    if (isCurrent()) error.value = '暂时无法翻阅出场历程，请稍后重试。'
  } finally {
    if (!disposed) loading.value = false
  }
}

function formatTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未记载' : date.toLocaleString('zh-CN', { hour12: false })
}

onMounted(() => load(1))
onBeforeUnmount(() => { disposed = true })
</script>

<style scoped lang="scss">
.appearance-history {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--color-border);
  text-align: left;
  color: var(--color-text);
  font-size: 14px;
  button {
    border: 1px solid var(--color-border);
    border-radius: 6px;
    padding: 7px 12px;
    background: var(--color-surface);
    color: var(--color-text);
    cursor: pointer;
    white-space: nowrap;
    &:disabled { opacity: .5; cursor: default; }
    &:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
  }
}
.history-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  h3 { margin: 0; font-size: 16px; }
  h3 span { margin-left: 6px; color: var(--color-primary); font-size: 13px; }
  p { margin: 8px 0 0; line-height: 1.6; color: var(--color-text-secondary); }
}
.history-state { padding: 20px 0; color: var(--color-text-secondary); line-height: 1.8; }
.history-timeline {
  list-style: none;
  padding: 0 0 0 16px;
  margin: 24px 0 12px 6px;
  border-left: 2px solid var(--color-border);
  li {
    position: relative;
    padding-bottom: 24px;
    &::before {
      content: '';
      position: absolute;
      left: -22px;
      top: 5px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--color-primary);
    }
    &:last-child { padding-bottom: 0; }
  }
  blockquote {
    margin: 12px 0 0;
    padding: 12px;
    border-radius: 6px;
    background: var(--color-bg);
    line-height: 1.8;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
}
.appearance-heading {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 12px;
  strong { overflow-wrap: anywhere; }
  time { color: var(--color-text-secondary); font-size: 12px; }
}
.appearance-meta { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 6px; color: var(--color-text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.history-pagination { display: flex; align-items: center; justify-content: center; gap: 12px; margin-top: 20px; }
.history-note { margin: 16px 0 0; color: var(--color-text-secondary); font-size: 12px; }
@media (max-width: 480px) {
  .history-header { flex-wrap: wrap; }
  .appearance-heading { flex-direction: column; }
}
</style>
