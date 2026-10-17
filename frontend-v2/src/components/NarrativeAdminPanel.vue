<script setup lang="ts">
import axios from 'axios'
import { computed, ref, watch } from 'vue'

import apiClient, { type ApiEnvelope } from '@/api/client'
import type {
  NarrativeCandidate,
  NarrativeDescriptor,
  NarrativeOverview,
  NarrativePage,
  NarrativeRecord,
} from '@/types/platform'

const props = defineProps<{
  tavernId: number
  tavernName: string
  tavernSlug: string
}>()

type TabKey =
  | 'entities'
  | 'facts'
  | 'story-threads'
  | 'candidates'
  | 'snapshots'
  | 'revisions'

const descriptor = ref<NarrativeDescriptor | null>(null)
const overview = ref<NarrativeOverview | null>(null)
const activeTab = ref<TabKey>('entities')
const rows = ref<NarrativeRecord[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const preview = ref<NarrativeRecord | null>(null)
const runKey = ref('admin-preview')
const scopeKeys = ref('')
const queryTerms = ref('')
const candidateType = ref('')
const candidateJson = ref(`{
  "operations": [
    {
      "op": "upsert_entity",
      "data": {
        "external_key": "entity:example",
        "kind": "personnel",
        "name": "示例实体",
        "summary": "请替换为已审核的世界信息"
      }
    }
  ]
}`)
let loadSequence = 0

const tabs = computed(() => [
  {
    key: 'entities' as const,
    label: descriptor.value?.labels.entity || '世界实体',
  },
  {
    key: 'facts' as const,
    label: descriptor.value?.labels.fact || '世界事实',
  },
  {
    key: 'story-threads' as const,
    label: descriptor.value?.labels.story_thread || '故事线',
  },
  {
    key: 'candidates' as const,
    label: descriptor.value?.labels.candidate || '维护候选',
  },
  {
    key: 'snapshots' as const,
    label: descriptor.value?.labels.snapshot || '注入快照',
  },
  { key: 'revisions' as const, label: '版本历史' },
])

const endpoint = computed(
  () => `/admin/taverns/${props.tavernId}/narrative`,
)

function setError(caught: unknown) {
  error.value = axios.isAxiosError(caught)
    ? caught.response?.data?.message || '叙事管理请求失败'
    : caught instanceof Error
      ? caught.message
      : '叙事管理请求失败'
}

function clearFeedback() {
  error.value = ''
  notice.value = ''
}

function splitTerms(value: string) {
  return value
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter((item, index, values) => item && values.indexOf(item) === index)
}

function titleOf(row: NarrativeRecord) {
  return String(
    row.name
      || row.title
      || row.external_key
      || row.key
      || row.snapshot_key
      || `#${row.id || ''}`,
  )
}

function summaryOf(row: NarrativeRecord) {
  const value = row.summary ?? row.value ?? row.decision_reason ?? ''
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function metadataOf(row: NarrativeRecord) {
  return [
    row.kind,
    row.status,
    row.visibility,
    row.source,
    row.revision ? `v${row.revision}` : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

function customViewPath(path?: string) {
  if (!path) return ''
  const resolved = path.replace(
    '{tavern_slug}',
    encodeURIComponent(props.tavernSlug),
  )
  return resolved.startsWith('/play/') ? resolved : ''
}

async function loadSummary() {
  const descriptorResponse = await apiClient.get<
    ApiEnvelope<NarrativeDescriptor>
  >(`${endpoint.value}/descriptor`, { timeout: 60_000 })
  descriptor.value = descriptorResponse.data.data
  const overviewResponse = await apiClient.get<
    ApiEnvelope<NarrativeOverview>
  >(`${endpoint.value}/overview`)
  overview.value = overviewResponse.data.data
  if (
    !candidateType.value
    || !descriptor.value.candidate_types.includes(candidateType.value)
  ) {
    candidateType.value = descriptor.value.candidate_types[0] || ''
  }
}

async function loadRows() {
  const sequence = ++loadSequence
  loading.value = true
  error.value = ''
  try {
    const response = await apiClient.get<
      ApiEnvelope<NarrativePage<NarrativeRecord>>
    >(`${endpoint.value}/${activeTab.value}`, {
      params: { page: 1, page_size: 100 },
    })
    if (sequence !== loadSequence) return
    rows.value = response.data.data.items
    total.value = response.data.data.total
  } catch (caught) {
    if (sequence === loadSequence) setError(caught)
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

async function loadAll() {
  clearFeedback()
  descriptor.value = null
  overview.value = null
  rows.value = []
  preview.value = null
  try {
    await loadSummary()
    await loadRows()
  } catch (caught) {
    setError(caught)
  }
}

async function selectTab(tab: TabKey) {
  activeTab.value = tab
  await loadRows()
}

function contextPayload() {
  return {
    run_key: runKey.value.trim(),
    scope_keys: splitTerms(scopeKeys.value),
    query_terms: splitTerms(queryTerms.value),
    allowed_visibility: ['public', 'members', 'admin', 'secret'],
  }
}

async function previewInjection() {
  clearFeedback()
  try {
    const response = await apiClient.post<ApiEnvelope<NarrativeRecord>>(
      `${endpoint.value}/preview`,
      contextPayload(),
    )
    preview.value = response.data.data
  } catch (caught) {
    setError(caught)
  }
}

async function freezeInjection() {
  clearFeedback()
  try {
    const response = await apiClient.post<
      ApiEnvelope<{ created: boolean }>
    >(`${endpoint.value}/snapshots`, contextPayload())
    notice.value = response.data.data.created
      ? '已冻结新的注入快照'
      : '该运行键已存在相同快照'
    await loadSummary()
    if (activeTab.value === 'snapshots') await loadRows()
  } catch (caught) {
    setError(caught)
  }
}

async function createCandidate() {
  clearFeedback()
  try {
    const payload = JSON.parse(candidateJson.value) as Record<string, unknown>
    await apiClient.post(`${endpoint.value}/candidates`, {
      idempotency_key: `admin:${crypto.randomUUID()}`,
      candidate_type: candidateType.value,
      source_stage_key: 'admin_manual',
      payload,
    })
    notice.value = '维护候选已创建，尚未写入世界状态'
    activeTab.value = 'candidates'
    await Promise.all([loadSummary(), loadRows()])
  } catch (caught) {
    setError(caught)
  }
}

async function acceptCandidate(row: NarrativeRecord) {
  clearFeedback()
  try {
    await apiClient.post(
      `${endpoint.value}/candidates/${row.id}/accept`,
    )
    notice.value = '候选已通过事务写入'
    await Promise.all([loadSummary(), loadRows()])
  } catch (caught) {
    setError(caught)
  }
}

async function rejectCandidate(row: NarrativeRecord) {
  const reason = window.prompt('请输入拒绝原因')
  if (!reason?.trim()) return
  clearFeedback()
  try {
    await apiClient.post(
      `${endpoint.value}/candidates/${row.id}/reject`,
      { reason: reason.trim() },
    )
    notice.value = '候选已拒绝'
    await Promise.all([loadSummary(), loadRows()])
  } catch (caught) {
    setError(caught)
  }
}

function asCandidate(row: NarrativeRecord) {
  return row as NarrativeCandidate
}

watch(
  () => props.tavernId,
  () => {
    void loadAll()
  },
  { immediate: true },
)
</script>

<template>
  <section class="panel narrative-admin">
    <header class="narrative-header">
      <div>
        <p class="eyebrow">共享世界剧情</p>
        <h2>{{ tavernName }} · 世界叙事</h2>
        <small v-if="descriptor">
          {{ descriptor.pack_id }} · contract {{ descriptor.contract_version }}
          <template v-if="descriptor.compatibility_mode">
            · {{ descriptor.compatibility_mode }}
          </template>
        </small>
      </div>
      <button class="button button-ghost" type="button" @click="loadAll">
        刷新
      </button>
    </header>

    <p v-if="error" class="error-message">{{ error }}</p>
    <p v-if="notice" class="success-message">{{ notice }}</p>

    <div v-if="overview" class="narrative-metrics">
      <article>
        <strong>{{ overview.entity_count }}</strong>
        <span>{{ descriptor?.labels.entity }}</span>
      </article>
      <article>
        <strong>{{ overview.fact_count }}</strong>
        <span>{{ descriptor?.labels.fact }}</span>
      </article>
      <article>
        <strong>{{ overview.story_thread_count }}</strong>
        <span>{{ descriptor?.labels.story_thread }}</span>
      </article>
      <article>
        <strong>{{ overview.pending_candidate_count }}</strong>
        <span>待审核</span>
      </article>
    </div>

    <div class="narrative-tools">
      <form class="narrative-tool" @submit.prevent="previewInjection">
        <h3>注入预览</h3>
        <label>
          运行键
          <input v-model.trim="runKey" required />
        </label>
        <label>
          作用域（逗号分隔）
          <input v-model="scopeKeys" placeholder="site-a, department-3" />
        </label>
        <label>
          查询词（逗号分隔）
          <input v-model="queryTerms" placeholder="红门, 值班主管" />
        </label>
        <div class="tool-actions">
          <button class="button button-primary" type="submit">生成预览</button>
          <button class="button button-ghost" type="button" @click="freezeInjection">
            冻结快照
          </button>
        </div>
      </form>

      <form
        v-if="descriptor?.supports_generic_writes"
        class="narrative-tool"
        @submit.prevent="createCandidate"
      >
        <h3>创建维护候选</h3>
        <label>
          候选类型
          <select v-model="candidateType" required>
            <option
              v-for="candidate in descriptor.candidate_types"
              :key="candidate"
              :value="candidate"
            >
              {{ candidate }}
            </option>
          </select>
        </label>
        <label>
          审核前结构化内容
          <textarea v-model="candidateJson" rows="9" spellcheck="false" />
        </label>
        <button class="button button-primary" type="submit">
          仅创建候选
        </button>
      </form>
      <div v-else-if="descriptor" class="narrative-tool compatibility-note">
        <h3>兼容模式</h3>
        <p>
          当前 Pack 的既有表和写回链仍是权威来源；共享面板只做统一查询与注入预览，
          不绕过它自己的 Reviewer、validator 或事务写回。
        </p>
        <ul v-if="descriptor.custom_views?.length">
          <li v-for="view in descriptor.custom_views" :key="view.key">
            <RouterLink
              v-if="customViewPath(view.path)"
              :to="customViewPath(view.path)"
            >
              {{ view.label }}
            </RouterLink>
            <span v-else>{{ view.label }}</span>
          </li>
        </ul>
      </div>
    </div>

    <details v-if="preview" class="preview-output" open>
      <summary>冻结前预览</summary>
      <pre>{{ JSON.stringify(preview, null, 2) }}</pre>
    </details>

    <nav class="narrative-tabs" aria-label="叙事数据分类">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="button"
        :class="activeTab === tab.key ? 'button-primary' : 'button-ghost'"
        type="button"
        @click="selectTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="loading">正在读取独立 Runtime…</p>
    <p v-else-if="rows.length === 0" class="empty-state">
      当前分类暂无记录。
    </p>
    <div v-else class="narrative-list">
      <article v-for="(row, index) in rows" :key="String(row.id || index)">
        <div>
          <strong>{{ titleOf(row) }}</strong>
          <small>{{ metadataOf(row) }}</small>
          <p v-if="summaryOf(row)">{{ summaryOf(row) }}</p>
          <details v-if="activeTab === 'candidates'">
            <summary>规范化候选内容</summary>
            <pre>{{ JSON.stringify(asCandidate(row).normalized_payload, null, 2) }}</pre>
          </details>
          <details v-else-if="row.attributes">
            <summary>Pack 专属字段</summary>
            <pre>{{ JSON.stringify(row.attributes, null, 2) }}</pre>
          </details>
        </div>
        <div
          v-if="activeTab === 'candidates' && row.status === 'pending'"
          class="tool-actions"
        >
          <button
            class="button button-primary"
            type="button"
            @click="acceptCandidate(row)"
          >
            接受
          </button>
          <button
            class="button button-ghost"
            type="button"
            @click="rejectCandidate(row)"
          >
            拒绝
          </button>
        </div>
      </article>
    </div>
    <small v-if="total > rows.length">仅显示前 {{ rows.length }} / {{ total }} 条</small>
  </section>
</template>

<style scoped>
.narrative-admin {
  display: grid;
  gap: 18px;
  margin-top: 20px;
}

.narrative-header,
.narrative-list article {
  align-items: flex-start;
  display: flex;
  gap: 16px;
  justify-content: space-between;
}

.narrative-header h2,
.narrative-tool h3 {
  margin: 0;
}

.narrative-header small,
.narrative-list small,
.empty-state {
  color: var(--muted);
}

.narrative-metrics {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.narrative-metrics article,
.narrative-tool,
.preview-output {
  background: rgba(255, 255, 255, 0.035);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 12px;
  padding: 14px;
}

.narrative-metrics article {
  display: grid;
  gap: 4px;
}

.narrative-metrics strong {
  font-size: 24px;
}

.narrative-tools {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.narrative-tool {
  display: grid;
  gap: 10px;
}

.narrative-tool label {
  display: grid;
  gap: 6px;
}

.narrative-tool textarea {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  resize: vertical;
}

.tool-actions,
.narrative-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.preview-output pre,
.narrative-list pre {
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
}

.narrative-list {
  display: grid;
  gap: 10px;
}

.narrative-list article {
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  padding: 0 0 12px;
}

.narrative-list article > div:first-child {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.narrative-list p {
  margin: 0;
  overflow-wrap: anywhere;
}

.compatibility-note p {
  color: var(--muted);
  line-height: 1.6;
}

@media (max-width: 800px) {
  .narrative-metrics,
  .narrative-tools {
    grid-template-columns: 1fr;
  }

  .narrative-header,
  .narrative-list article {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
