<template>
  <div class="ai-feedback-admin">
    <div class="feedback-admin-head">
      <div>
        <h6>✨ AI 玩家反馈</h6>
        <p>两项都是 1–5 分；模型、供应商与目标站点仅本管理页可见。</p>
      </div>
      <div class="feedback-filters">
        <select v-model="days" aria-label="统计时间范围" @change="reloadFromFirstPage">
          <option value="7">近 7 天</option>
          <option value="30">近 30 天</option>
          <option value="90">近 90 天</option>
          <option value="all">全部</option>
        </select>
        <select v-model="targetType" aria-label="AI 内容类型" @change="reloadFromFirstPage">
          <option value="all">全部内容</option>
          <option v-for="option in targetOptions" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
        <button type="button" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
      </div>
    </div>

    <div v-if="dashboard" class="feedback-summary">
      <div><span>评分数</span><strong>{{ dashboard.summary.total_feedback }}</strong></div>
      <div><span>参与玩家</span><strong>{{ dashboard.summary.unique_players }}</strong></div>
      <div><span>AI 质量</span><strong>{{ scoreText(dashboard.summary.avg_quality) }}</strong></div>
      <div><span>个人喜好</span><strong>{{ scoreText(dashboard.summary.avg_preference) }}</strong></div>
    </div>

    <p v-if="loading && !dashboard" class="feedback-state">正在读取反馈…</p>
    <p v-else-if="!dashboard?.summary.total_feedback" class="feedback-state">当前范围还没有玩家评分。</p>

    <template v-if="dashboard?.summary.total_feedback">
      <section class="feedback-section">
        <h6>按内容</h6>
        <div class="feedback-table-wrap">
          <table>
            <thead><tr><th>内容</th><th>样本</th><th>质量</th><th>喜好</th></tr></thead>
            <tbody>
              <tr v-for="row in dashboard.by_target" :key="row.target_type">
                <td>{{ row.content_label }}</td>
                <td>{{ row.count }}</td>
                <td>{{ scoreText(row.avg_quality) }}</td>
                <td>{{ scoreText(row.avg_preference) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="feedback-section">
        <h6>按实际 AI 路由</h6>
        <div class="feedback-table-wrap">
          <table>
            <thead><tr><th>调用</th><th>模型</th><th>供应商 / 站点</th><th>样本</th><th>质量</th><th>喜好</th></tr></thead>
            <tbody>
              <tr v-for="(row, index) in dashboard.by_route" :key="`${row.call_type}-${row.model}-${row.endpoint_host}-${index}`">
                <td>{{ row.call_type_label }}</td>
                <td><code>{{ row.model || '未记录' }}</code></td>
                <td>
                  {{ row.provider || '未记录' }}
                  <small>{{ row.endpoint_host || '旧记录未保存站点' }}</small>
                  <small>{{ row.profile_id || '未记录 profile' }} · endpoint={{ row.api_endpoint_slot || '-' }} · key={{ row.api_key_slot || '-' }}</small>
                </td>
                <td>{{ row.count }}</td>
                <td>{{ scoreText(row.avg_quality) }}</td>
                <td>{{ scoreText(row.avg_preference) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="feedback-section">
        <h6>最近评分</h6>
        <div class="feedback-table-wrap">
          <table>
            <thead><tr><th>玩家</th><th>内容</th><th>质量 / 喜好</th><th>来源回执</th><th>更新时间</th></tr></thead>
            <tbody>
              <tr v-for="item in dashboard.recent.items" :key="item.id">
                <td>{{ item.player_name }}</td>
                <td>{{ item.content_label }} #{{ item.target_id }}</td>
                <td><strong>{{ item.quality_score }}</strong> / <strong>{{ item.preference_score }}</strong></td>
                <td class="route-receipt">
                  <code>{{ item.model || '未记录模型' }}</code>
                  <small>{{ item.provider || '未记录供应商' }} · {{ item.endpoint_host || '未记录站点' }}</small>
                  <small>endpoint={{ item.api_endpoint_slot || '-' }} · key={{ item.api_key_slot || '-' }}</small>
                </td>
                <td>{{ item.updated_at ? formatDateTime(item.updated_at) : '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="dashboard.recent.pages > 1" class="feedback-pager">
          <button type="button" :disabled="loading || page <= 1" @click="changePage(page - 1)">上一页</button>
          <span>{{ page }} / {{ dashboard.recent.pages }}</span>
          <button type="button" :disabled="loading || page >= dashboard.recent.pages" @click="changePage(page + 1)">下一页</button>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatDateTime } from '@/utils/time'
import {
  useAdventurerS1Store,
  type AiFeedbackTargetType,
} from '../stores/adventurerS1'

interface FeedbackAggregateRow {
  target_type?: AiFeedbackTargetType
  content_label?: string
  call_type?: string
  call_type_label?: string
  model?: string | null
  profile_id?: string | null
  provider?: string | null
  endpoint_host?: string | null
  api_key_slot?: string | null
  api_endpoint_slot?: string | null
  count: number
  avg_quality: number | null
  avg_preference: number | null
}

interface FeedbackRecentItem {
  id: number
  player_name: string
  content_label: string
  target_id: number
  quality_score: number
  preference_score: number
  model: string | null
  provider: string | null
  endpoint_host: string | null
  api_endpoint_slot: string | null
  api_key_slot: string | null
  updated_at: string | null
}

interface FeedbackDashboard {
  target_options: Array<{ value: AiFeedbackTargetType; label: string }>
  summary: {
    total_feedback: number
    unique_players: number
    avg_quality: number | null
    avg_preference: number | null
  }
  by_target: FeedbackAggregateRow[]
  by_route: FeedbackAggregateRow[]
  recent: {
    items: FeedbackRecentItem[]
    total: number
    page: number
    per_page: number
    pages: number
  }
}

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const loading = ref(false)
const days = ref<'7' | '30' | '90' | 'all'>('30')
const targetType = ref<AiFeedbackTargetType | 'all'>('all')
const page = ref(1)
const dashboard = ref<FeedbackDashboard | null>(null)
const targetOptions = computed(() => dashboard.value?.target_options ?? [
  { value: 'expedition_log' as const, label: '探险日志' },
  { value: 'item_assembly' as const, label: '构装结果' },
  { value: 'skill_training' as const, label: '导师授艺' },
])

function scoreText(value: number | null | undefined): string {
  return value == null ? '—' : `${Number(value).toFixed(2)} / 5`
}

async function load() {
  loading.value = true
  try {
    const response = await store.adminFetchAiFeedback({
      days: days.value === 'all' ? 'all' : Number(days.value),
      target_type: targetType.value,
      page: page.value,
      per_page: 20,
    })
    if (response.code !== 200 || !response.data) {
      message.error(response.message || 'AI 反馈读取失败')
      return
    }
    dashboard.value = response.data as FeedbackDashboard
    page.value = dashboard.value.recent.page
  } catch (error: any) {
    message.error(error?.response?.data?.message || 'AI 反馈读取失败')
  } finally {
    loading.value = false
  }
}

function reloadFromFirstPage() {
  page.value = 1
  void load()
}

function changePage(nextPage: number) {
  page.value = nextPage
  void load()
}

onMounted(load)
</script>

<style scoped>
.ai-feedback-admin {
  min-width: 0;
}

.feedback-admin-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.feedback-admin-head h6,
.feedback-section h6 {
  margin: 0;
  color: var(--color-text);
}

.feedback-admin-head p {
  margin: 5px 0 0;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.feedback-filters,
.feedback-pager {
  display: flex;
  align-items: center;
  gap: 7px;
}

.feedback-filters select,
.feedback-filters button,
.feedback-pager button {
  min-height: 32px;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  background: var(--color-bg);
  color: var(--color-text);
  padding: 4px 9px;
}

.feedback-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(110px, 1fr));
  gap: 8px;
  margin-top: 14px;
}

.feedback-summary > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 9px;
  background: color-mix(in srgb, var(--color-primary) 5%, var(--color-bg));
}

.feedback-summary span,
.feedback-table-wrap small {
  color: var(--color-text-secondary);
  font-size: 11px;
}

.feedback-summary strong {
  font-size: 17px;
  color: var(--color-text);
}

.feedback-state {
  padding: 22px 0;
  color: var(--color-text-secondary);
  text-align: center;
}

.feedback-section {
  margin-top: 16px;
}

.feedback-table-wrap {
  overflow-x: auto;
  margin-top: 7px;
  border: 1px solid var(--color-border);
  border-radius: 9px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

th,
td {
  padding: 8px 9px;
  border-bottom: 1px solid var(--color-border);
  text-align: left;
  white-space: nowrap;
}

tbody tr:last-child td {
  border-bottom: 0;
}

td small,
.route-receipt {
  display: block;
}

.route-receipt small {
  margin-top: 2px;
}

code {
  color: var(--color-primary);
  font-size: 11px;
}

.feedback-pager {
  justify-content: flex-end;
  margin-top: 8px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

@media (max-width: 720px) {
  .feedback-summary {
    grid-template-columns: repeat(2, minmax(100px, 1fr));
  }
}
</style>
