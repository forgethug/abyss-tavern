<template>
  <div class="trait-compress-panel">
    <section class="overview-panel">
      <div>
        <div class="eyebrow">管理员只读诊断</div>
        <h5>实际压缩对比</h5>
        <p>
          特质、随身装备和已携带技能都按主剧情实际使用的原文哈希核对。
          左侧始终是玩家可见全文；右侧是摘要候选。只有审核生效的摘要才会替代主剧情输入，
          特质还需要单独通过场景路由审核。
        </p>
      </div>
      <button
        type="button"
        class="btn-secondary btn-sm refresh-button"
        :disabled="loadingPreview"
        @click="loadPreview()"
      >
        {{ loadingPreview ? '刷新中…' : '↻ 刷新数据' }}
      </button>
    </section>

    <section v-if="summary" class="stats-grid" aria-label="描述压缩概览">
      <article class="stat-card">
        <span>{{ assetKindLabel(assetKind) }}总数</span>
        <strong>{{ formatNumber(summary.total_assets) }}</strong>
        <small>{{ assetScopeHint }}</small>
      </article>
      <article class="stat-card is-success">
        <span>本页可正常使用</span>
        <strong>{{ pageStats.ready }}/{{ items.length }}</strong>
        <small>摘要与适用路由均已就绪</small>
      </article>
      <article class="stat-card is-success">
        <span>本页摘要生效</span>
        <strong>{{ pageStats.summaryReady }}/{{ items.length }}</strong>
        <small>{{ pageStats.noSummaryNeeded }} 条无需压缩</small>
      </article>
      <article :class="['stat-card', pageStats.reviewIssue ? 'is-caution' : 'is-info']">
        <span>{{ assetKind === 'trait' ? '本页路由生效' : '本页审核问题' }}</span>
        <strong>
          {{ assetKind === 'trait' ? `${pageStats.contextReady}/${items.length}` : pageStats.reviewIssue }}
        </strong>
        <small>{{ pageStats.reviewIssue }} 条存在未通过或审核异常</small>
      </article>
      <article class="stat-card">
        <span>本页输入缩减</span>
        <strong>{{ formatNumber(pageStats.savedChars) }} 字</strong>
        <small>相对全文减少 {{ pageStats.savedPercent }}%</small>
      </article>
    </section>

    <section class="toolbar" aria-label="预览筛选">
      <div class="filter-stack">
        <div class="filter-row">
          <span class="filter-label">类型</span>
          <div class="scope-switch" role="group" aria-label="描述类型">
            <button
              v-for="option in assetKindOptions"
              :key="option.value"
              type="button"
              :class="{ active: assetKind === option.value }"
              :disabled="loadingPreview"
              @click="setAssetKind(option.value)"
            >{{ option.label }}</button>
          </div>
        </div>
        <div class="filter-row">
          <span class="filter-label">状态</span>
          <div class="scope-switch status-switch" role="group" aria-label="审核状态">
            <button
              v-for="option in visibleStatusOptions"
              :key="option.value"
              type="button"
              :class="{ active: statusFilter === option.value }"
              :disabled="loadingPreview"
              @click="setStatusFilter(option.value)"
            >{{ option.label }}</button>
          </div>
        </div>
      </div>
      <div class="toolbar-meta">
        <span v-if="summary">
          “{{ currentStatusLabel }}”共 {{ formatNumber(summary.total) }} 条 ·
          第 {{ summary.page }}/{{ Math.max(summary.pages, 1) }} 页
        </span>
        <label>
          每页
          <select v-model.number="perPage" :disabled="loadingPreview" @change="changePageSize">
            <option :value="25">25</option>
            <option :value="50">50</option>
            <option :value="100">100</option>
          </select>
        </label>
      </div>
    </section>

    <div v-if="loadingPreview && !items.length" class="empty-state">
      正在读取最新压缩缓存…
    </div>

    <section v-else-if="items.length" class="compare-list" aria-label="描述压缩对比列表">
      <article
        v-for="row in items"
        :key="row.record_key"
        :class="['compare-card', { expanded: expandedKey === row.record_key }]"
      >
        <header class="card-header">
          <div class="identity-block">
            <span :class="['asset-kind-chip', `is-${row.asset_kind}`]">
              {{ assetKindLabel(row.asset_kind) }}
            </span>
            <span class="adventurer-name">{{ row.adventurer_name || '未知冒险者' }}</span>
            <strong>{{ row.asset_name }}</strong>
            <span class="record-id">#{{ row.asset_id }}</span>
          </div>
          <div class="status-group">
            <span :class="['status-pill', `is-${summaryStatus(row).tone}`]">
              {{ summaryStatus(row).label }}
            </span>
            <span
              v-if="row.context_applicable"
              :class="['status-pill', `is-${contextStatus(row).tone}`]"
            >
              {{ contextStatus(row).label }}
            </span>
          </div>
        </header>

        <div class="card-body">
          <div class="length-flow" aria-label="字符数变化">
            <div>
              <span>玩家全文</span>
              <strong>{{ formatNumber(row.description_len) }}</strong>
              <small>字</small>
            </div>
            <span class="flow-arrow">→</span>
            <div>
              <span>主剧情实际读取</span>
              <strong>{{ formatNumber(row.compressed ? row.description_ai_len : row.description_len) }}</strong>
              <small>字</small>
            </div>
            <div v-if="row.compressed" class="saving-chip">
              -{{ row.saved_percent }}%
            </div>
            <div v-else class="full-source-chip">完整原文</div>
          </div>

          <p v-if="row.candidate_description_ai" class="summary-excerpt">
            {{ row.candidate_description_ai }}
          </p>
          <p v-else class="summary-excerpt is-empty">
            当前原文哈希还没有摘要候选。
          </p>

          <button type="button" class="detail-toggle" @click="toggleExpand(row.record_key)">
            {{ expandedKey === row.record_key ? '收起详细对比' : detailToggleLabel(row) }}
            <span aria-hidden="true">{{ expandedKey === row.record_key ? '↑' : '↓' }}</span>
          </button>
        </div>

        <div v-if="expandedKey === row.record_key" class="detail-panel">
          <div
            v-if="row.candidate_description_ai && !row.cache.summary_effective"
            class="callout is-warning"
          >
            <template v-if="row.cache.summary_approved">
              这条摘要已审核通过，但摘要功能开关当前关闭；主剧情仍会读取完整原文。
            </template>
            <template v-else>
              这条摘要候选目前没有通过统一缓存的生效条件；主剧情仍会读取
              {{ row.effective_summary_source === 'legacy' ? '精确匹配的旧字段摘要' : '完整原文' }}。
            </template>
          </div>

          <div class="compare-columns">
            <section class="text-panel">
              <div class="panel-heading">
                <span>玩家全文</span>
                <small>{{ formatNumber(row.description_len) }} 字</small>
              </div>
              <pre>{{ row.description }}</pre>
            </section>
            <section class="text-panel">
              <div class="panel-heading">
                <span>{{ candidateLabel(row) }}</span>
                <small>{{ formatNumber(row.candidate_description_ai_len) }} 字</small>
              </div>
              <pre :class="{ empty: !row.candidate_description_ai }">{{ row.candidate_description_ai || '（尚未生成摘要候选）' }}</pre>
            </section>
          </div>

          <section
            v-if="row.description_ai && row.description_ai !== row.candidate_description_ai"
            class="effective-summary-panel"
          >
            <div class="panel-heading">
              <span>主剧情当前采用的摘要</span>
              <small>{{ row.description_ai_len }} 字 · {{ effectiveSourceLabel(row) }}</small>
            </div>
            <p>{{ row.description_ai }}</p>
          </section>

          <section v-if="row.context_applicable" class="routing-panel">
            <div class="routing-heading">
              <div>
                <span>特质场景路由</span>
                <small>只决定主剧情是否需要看见这项特质，不裁决能力是否触发</small>
              </div>
              <span :class="['status-pill', `is-${contextStatus(row).tone}`]">
                {{ contextStatus(row).label }}
              </span>
            </div>

            <div v-if="visibleContextRule(row)" class="rule-content">
              <div v-if="visibleContextRule(row)?.always_include" class="always-include">
                始终注入：当前规则无法安全限定到少数场景。
              </div>
              <div v-else-if="contextClauses(row).length" class="rule-clauses">
                <template v-for="(clause, clauseIndex) in contextClauses(row)" :key="clauseIndex">
                  <span v-if="clauseIndex" class="or-label">或</span>
                  <div class="rule-clause">
                    <span v-for="tag in clause" :key="tag" class="tag-chip">{{ tag }}</span>
                    <span v-if="clause.length > 1" class="and-note">同时满足</span>
                  </div>
                </template>
              </div>
              <p v-if="!row.cache.context_effective" class="candidate-note">
                上面是尚未生效的路由候选，仅供管理员排查。
              </p>
            </div>
            <p v-else class="routing-empty">当前原文哈希还没有可展示的路由规则。</p>
          </section>

          <dl class="metadata-grid">
            <div>
              <dt>缓存状态</dt>
              <dd>{{ cacheStatusLabel(row.cache.status) }}</dd>
            </div>
            <div>
              <dt>摘要审核</dt>
              <dd>{{ auditStatusLabel(row.cache.summary_audit_status) }}</dd>
            </div>
            <div v-if="row.context_applicable">
              <dt>路由审核</dt>
              <dd>{{ auditStatusLabel(row.cache.context_audit_status) }}</dd>
            </div>
            <div v-else>
              <dt>场景路由</dt>
              <dd>不适用</dd>
            </div>
            <div>
              <dt>模型</dt>
              <dd>{{ row.cache.model_used || '—' }}</dd>
            </div>
            <div>
              <dt>基础失败 / 后台复查</dt>
              <dd>{{ row.cache.failure_count }} / {{ row.cache.terminal_retry_count }}</dd>
            </div>
            <div>
              <dt>缓存更新时间</dt>
              <dd>{{ formatTimestamp(row.cache.updated_at) }}</dd>
            </div>
            <div>
              <dt>原文哈希</dt>
              <dd class="hash-value" :title="row.cache.source_hash">{{ shortHash(row.cache.source_hash) }}</dd>
            </div>
          </dl>
        </div>
      </article>
    </section>

    <div v-else-if="!loadingPreview" class="empty-state">
      当前类型和状态下没有可展示的记录。
    </div>

    <nav v-if="summary && summary.pages > 1" class="pagination" aria-label="描述压缩分页">
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="loadingPreview || !summary.has_prev"
        @click="goToPage(summary.page - 1)"
      >上一页</button>
      <span>第 {{ summary.page }} / {{ summary.pages }} 页</span>
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="loadingPreview || !summary.has_next"
        @click="goToPage(summary.page + 1)"
      >下一页</button>
    </nav>

    <details class="compat-tools">
      <summary>兼容批处理工具</summary>
      <p>
        这是原有的旧字段摘要入口：每次最多入队 50 条，不改玩家全文，也不代表新系统的特质路由已经生成或审核完成。
        正常探险仍会按现有逻辑自动预热。
      </p>
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="compressing"
        @click="runCompress"
      >
        {{ compressing ? '正在入队…' : '入队 50 条旧字段摘要' }}
      </button>
    </details>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useAdventurerS1Store } from '../stores/adventurerS1'

type ContextRule = {
  schema_version?: string
  classification_source_hash?: string
  always_include?: boolean
  match_any?: string[][]
}

type CacheState = {
  exists: boolean
  source_hash: string
  status: string
  summary_audit_status: string
  context_audit_status: string
  summary_approved: boolean
  summary_effective: boolean
  context_rule: ContextRule | null
  effective_context_rule: ContextRule | null
  context_effective: boolean
  model_used: string | null
  failure_count: number
  terminal_retry_count: number
  next_retry_at: string | null
  audited_at: string | null
  updated_at: string | null
}

type AssetKind = 'trait' | 'equipment' | 'skill'
type StatusFilter = 'all' | 'ready' | 'summary_issue' | 'route_issue' | 'pending' | 'kept_original' | 'full_fallback'

type StatusFlags = {
  ready: boolean
  summary_issue: boolean
  route_issue: boolean
  pending: boolean
  kept_original: boolean
  full_fallback: boolean
}

type CompareRow = {
  record_key: string
  asset_kind: AssetKind
  asset_id: number
  asset_name: string
  asset_type_detail: string
  source_kind: string
  trait_id: number | null
  adventurer_id: number
  adventurer_name: string
  equipped: boolean
  context_applicable: boolean
  requires_summary: boolean
  description_len: number
  description_ai_len: number
  candidate_description_ai_len: number
  compressed: boolean
  needs_compression: boolean
  description: string
  description_ai: string | null
  candidate_description_ai: string | null
  candidate_summary_source: 'cache' | 'legacy' | null
  effective_summary_source: 'cache' | 'legacy' | 'full'
  main_ai_uses_full_description: boolean
  saved_chars: number
  saved_percent: number
  description_ai_at: string | null
  legacy_summary_current: boolean
  status_flags: StatusFlags
  cache: CacheState
}

type PreviewSummary = {
  asset_kind: AssetKind
  status_filter: StatusFilter
  total_assets: number
  total_traits: number
  total_missing: number
  preview_count: number
  page: number
  per_page: number
  total: number
  pages: number
  has_next: boolean
  has_prev: boolean
}

type StatusView = { label: string; tone: 'success' | 'info' | 'warning' | 'danger' | 'muted' }
type FilterOption<T extends string> = { value: T; label: string }

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const numberFormatter = new Intl.NumberFormat('zh-CN')

const loadingPreview = ref(false)
const compressing = ref(false)
const summary = ref<PreviewSummary | null>(null)
const items = ref<CompareRow[]>([])
const expandedKey = ref<string | null>(null)
const assetKind = ref<AssetKind>('trait')
const statusFilter = ref<StatusFilter>('all')
const currentPage = ref(1)
const perPage = ref(50)

const assetKindOptions: FilterOption<AssetKind>[] = [
  { value: 'trait', label: '特质' },
  { value: 'equipment', label: '装备' },
  { value: 'skill', label: '技能' },
]

const statusOptions: FilterOption<StatusFilter>[] = [
  { value: 'all', label: '全部' },
  { value: 'ready', label: '全部通过' },
  { value: 'summary_issue', label: '摘要未通过 / 异常' },
  { value: 'route_issue', label: '路由未通过 / 异常' },
  { value: 'pending', label: '待处理' },
  { value: 'kept_original', label: '审核保留原文' },
  { value: 'full_fallback', label: '回退全文' },
]

const visibleStatusOptions = computed(() => (
  assetKind.value === 'trait'
    ? statusOptions
    : statusOptions.filter((option) => option.value !== 'route_issue')
))

const currentStatusLabel = computed(() => (
  statusOptions.find((option) => option.value === statusFilter.value)?.label || '全部'
))

const assetScopeHint = computed(() => ({
  trait: '全部未归档特质',
  equipment: '当前冒险者随身装备实例',
  skill: '当前已携带技能',
})[assetKind.value])

const pageStats = computed(() => {
  const sourceChars = items.value.reduce((sum, row) => sum + row.description_len, 0)
  const effectiveChars = items.value.reduce(
    (sum, row) => sum + (row.compressed ? row.description_ai_len : row.description_len),
    0,
  )
  const savedChars = Math.max(0, sourceChars - effectiveChars)
  return {
    ready: items.value.filter((row) => row.status_flags.ready).length,
    summaryReady: items.value.filter((row) => row.compressed).length,
    noSummaryNeeded: items.value.filter((row) => !row.requires_summary).length,
    contextReady: items.value.filter((row) => row.cache.context_effective).length,
    reviewIssue: items.value.filter((row) => (
      row.status_flags.summary_issue || row.status_flags.route_issue
    )).length,
    savedChars,
    savedPercent: sourceChars ? Math.round(savedChars * 1000 / sourceChars) / 10 : 0,
  }
})

function formatNumber(value: number) {
  return numberFormatter.format(Number(value || 0))
}

function formatTimestamp(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('zh-CN', { hour12: false })
}

function shortHash(value: string) {
  return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : '—'
}

function assetKindLabel(value: AssetKind) {
  return ({ trait: '特质', equipment: '装备', skill: '技能' })[value]
}

function detailToggleLabel(row: CompareRow) {
  return row.context_applicable ? '查看全文、摘要与路由' : '查看全文与摘要'
}

function cacheStatusLabel(status: string) {
  return ({
    active: '摘要已激活',
    keep_original: '保留原文',
    pending: '等待处理',
    processing: '处理中',
    retryable: '等待重试',
    audit_rejected: '摘要审核未通过',
    missing: '尚无缓存',
  } as Record<string, string>)[status] || status || '未知'
}

function auditStatusLabel(status: string) {
  return ({
    passed: '已通过',
    rejected: '未通过',
    error: '审核异常',
    not_sampled: '待审核 / 不适用',
    not_applicable: '不适用',
    missing: '尚无记录',
  } as Record<string, string>)[status] || status || '未知'
}

function summaryStatus(row: CompareRow): StatusView {
  if (!row.requires_summary) return { label: '原文较短，无需压缩', tone: 'success' }
  if (row.compressed) {
    return row.effective_summary_source === 'cache'
      ? { label: '摘要已生效', tone: 'success' }
      : { label: '旧摘要生效', tone: 'info' }
  }
  if (row.cache.status === 'keep_original') return { label: '审核决定保留原文', tone: 'muted' }
  if (row.cache.status === 'processing') return { label: '正在压缩', tone: 'info' }
  if (row.cache.status === 'pending') return { label: '等待处理', tone: 'warning' }
  if (row.cache.status === 'retryable') return { label: '等待重试', tone: 'warning' }
  if (row.cache.status === 'audit_rejected' || row.cache.summary_audit_status === 'rejected') {
    return { label: '摘要未通过', tone: 'danger' }
  }
  if (row.cache.summary_audit_status === 'error') return { label: '摘要审核异常', tone: 'danger' }
  if (row.cache.summary_approved && !row.cache.summary_effective) {
    return { label: '摘要已通过，功能当前关闭', tone: 'info' }
  }
  return { label: '尚无摘要缓存', tone: 'muted' }
}

function contextStatus(row: CompareRow): StatusView {
  if (row.cache.context_effective) return { label: '路由已生效', tone: 'success' }
  if (row.cache.context_audit_status === 'passed') return { label: '路由格式不可用', tone: 'danger' }
  if (row.cache.context_audit_status === 'rejected') return { label: '路由未通过', tone: 'danger' }
  if (row.cache.context_audit_status === 'error') return { label: '路由审核异常', tone: 'danger' }
  if (row.cache.exists && row.cache.context_rule) return { label: '路由待审核', tone: 'warning' }
  return { label: '路由未生成', tone: 'muted' }
}

function visibleContextRule(row: CompareRow) {
  return row.cache.effective_context_rule || row.cache.context_rule
}

function contextClauses(row: CompareRow) {
  const clauses = visibleContextRule(row)?.match_any
  if (!Array.isArray(clauses)) return []
  return clauses.filter((clause): clause is string[] => (
    Array.isArray(clause) && clause.every((tag) => typeof tag === 'string')
  ))
}

function candidateLabel(row: CompareRow) {
  if (row.candidate_summary_source === 'cache') return '统一缓存中的摘要候选'
  if (row.candidate_summary_source === 'legacy') return '旧字段摘要'
  return '摘要候选'
}

function effectiveSourceLabel(row: CompareRow) {
  return row.effective_summary_source === 'cache' ? '统一缓存' : '旧字段精确哈希'
}

async function loadPreview(options: { resetPage?: boolean } = {}) {
  if (loadingPreview.value) return
  if (options.resetPage) {
    currentPage.value = 1
    expandedKey.value = null
    summary.value = null
    items.value = []
  }
  loadingPreview.value = true
  try {
    const res = await store.adminDescriptionCompressPreview({
      assetKind: assetKind.value,
      statusFilter: statusFilter.value,
      page: currentPage.value,
      perPage: perPage.value,
    })
    if (res.code !== 200) {
      message.error(res.message || '预览失败')
      return
    }
    const data = res.data || {}
    summary.value = {
      asset_kind: data.asset_kind ?? assetKind.value,
      status_filter: data.status_filter ?? statusFilter.value,
      total_assets: data.total_assets ?? 0,
      total_traits: data.total_traits ?? 0,
      total_missing: data.total_missing ?? 0,
      preview_count: data.preview_count ?? 0,
      page: data.page ?? 1,
      per_page: data.per_page ?? perPage.value,
      total: data.total ?? 0,
      pages: data.pages ?? 0,
      has_next: Boolean(data.has_next),
      has_prev: Boolean(data.has_prev),
    }
    currentPage.value = summary.value.page
    items.value = data.items ?? []
    if (expandedKey.value && !items.value.some((row) => row.record_key === expandedKey.value)) {
      expandedKey.value = null
    }
  } catch (error: any) {
    message.error(error?.response?.data?.message || '预览失败')
  } finally {
    loadingPreview.value = false
  }
}

function setAssetKind(value: AssetKind) {
  if (assetKind.value === value) return
  assetKind.value = value
  if (value !== 'trait' && statusFilter.value === 'route_issue') {
    statusFilter.value = 'all'
  }
  expandedKey.value = null
  void loadPreview({ resetPage: true })
}

function setStatusFilter(value: StatusFilter) {
  if (statusFilter.value === value) return
  statusFilter.value = value
  expandedKey.value = null
  void loadPreview({ resetPage: true })
}

function changePageSize() {
  void loadPreview({ resetPage: true })
}

function goToPage(page: number) {
  if (!summary.value || page < 1 || page > summary.value.pages) return
  currentPage.value = page
  expandedKey.value = null
  void loadPreview()
}

async function runCompress() {
  if (compressing.value) return
  const count = summary.value?.total_missing ?? 0
  if (count === 0) {
    message.info('没有旧字段未写入的特质')
    return
  }
  if (!confirm(
    `这是旧兼容入口：将为最多 50 条旧字段未写入特质调用摘要 AI，`
    + `不会代表新路由审核完成。当前旧字段未写入约 ${count} 条。确定入队吗？`,
  )) return
  compressing.value = true
  try {
    const res = await store.adminTraitCompress({ limit: 50, only_missing: true })
    if (res.code !== 200) {
      message.error(res.message || '入队失败')
      return
    }
    message.success(res.message || '任务已入队；处理完成后请手动刷新数据')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '入队失败')
  } finally {
    compressing.value = false
  }
}

function toggleExpand(recordKey: string) {
  expandedKey.value = expandedKey.value === recordKey ? null : recordKey
}

void loadPreview()
</script>

<style scoped>
.trait-compress-panel { display: grid; gap: 14px; }
.overview-panel {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 20px;
  padding: 18px 20px; border: 1px solid var(--border-color, rgba(128, 128, 128, 0.24));
  border-radius: 14px; background: linear-gradient(135deg, rgba(74, 158, 255, 0.11), rgba(130, 92, 246, 0.06));
}
.overview-panel h5 { margin: 2px 0 7px; font-size: 1.08rem; }
.overview-panel p {
  max-width: 850px; margin: 0; color: var(--text-secondary, #6b7280);
  font-size: 0.88rem; line-height: 1.65;
}
.eyebrow { color: var(--primary-color, #4a9eff); font-size: 0.72rem; font-weight: 700; letter-spacing: 0.12em; }
.refresh-button { flex: 0 0 auto; }
.stats-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
.stat-card {
  display: grid; gap: 4px; min-width: 0; padding: 13px 14px;
  border: 1px solid var(--border-color, rgba(128, 128, 128, 0.22));
  border-radius: 12px; background: var(--surface-color, rgba(127, 127, 127, 0.04));
}
.stat-card span, .stat-card small { color: var(--text-secondary, #6b7280); font-size: 0.75rem; }
.stat-card strong { font-size: 1.25rem; line-height: 1.15; }
.stat-card.is-caution strong { color: #b7791f; }
.stat-card.is-success strong { color: #2f855a; }
.stat-card.is-info strong { color: var(--primary-color, #3182ce); }
.toolbar {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 14px;
  padding: 10px 12px; border: 1px solid var(--border-color, rgba(128, 128, 128, 0.2)); border-radius: 10px;
}
.filter-stack { display: grid; gap: 8px; min-width: 0; }
.filter-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.filter-label {
  flex: 0 0 30px; color: var(--text-secondary, #6b7280);
  font-size: 0.7rem; font-weight: 650;
}
.scope-switch { display: inline-flex; padding: 3px; border-radius: 9px; background: var(--surface-muted, rgba(127, 127, 127, 0.1)); }
.status-switch { flex-wrap: wrap; }
.scope-switch button {
  border: 0; border-radius: 7px; padding: 6px 11px; background: transparent;
  color: inherit; cursor: pointer; font-size: 0.8rem;
}
.scope-switch button.active {
  background: var(--surface-color, #fff); color: var(--primary-color, #3182ce);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12); font-weight: 650;
}
.toolbar-meta { display: flex; align-items: center; gap: 16px; color: var(--text-secondary, #6b7280); font-size: 0.78rem; }
.toolbar-meta label { display: inline-flex; align-items: center; gap: 6px; }
.toolbar-meta select {
  min-width: 64px; padding: 4px 6px; border: 1px solid var(--border-color, rgba(128, 128, 128, 0.3));
  border-radius: 6px; background: var(--surface-color, #fff); color: inherit;
}
.compare-list { display: grid; gap: 10px; }
.compare-card {
  overflow: hidden; border: 1px solid var(--border-color, rgba(128, 128, 128, 0.22));
  border-radius: 12px; background: var(--surface-color, rgba(127, 127, 127, 0.025));
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}
.compare-card.expanded { border-color: rgba(74, 158, 255, 0.5); box-shadow: 0 8px 24px rgba(20, 80, 140, 0.08); }
.card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 13px 15px 8px; }
.identity-block { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 10px; min-width: 0; }
.identity-block strong { font-size: 0.95rem; }
.asset-kind-chip {
  padding: 2px 7px; border-radius: 999px; font-size: 0.67rem; font-weight: 700;
  background: rgba(113, 128, 150, 0.13); color: var(--text-secondary, #6b7280);
}
.asset-kind-chip.is-trait { background: rgba(128, 90, 213, 0.13); color: #805ad5; }
.asset-kind-chip.is-equipment { background: rgba(183, 121, 31, 0.14); color: #b7791f; }
.asset-kind-chip.is-skill { background: rgba(49, 130, 206, 0.13); color: #3182ce; }
.adventurer-name { color: var(--primary-color, #3182ce); font-size: 0.8rem; font-weight: 650; }
.record-id { color: var(--text-secondary, #6b7280); font-size: 0.7rem; }
.status-group { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.status-pill {
  display: inline-flex; align-items: center; min-height: 23px; padding: 2px 8px;
  border-radius: 999px; font-size: 0.7rem; font-weight: 650; white-space: nowrap;
}
.status-pill.is-success { background: rgba(47, 133, 90, 0.13); color: #2f855a; }
.status-pill.is-info { background: rgba(49, 130, 206, 0.13); color: #3182ce; }
.status-pill.is-warning { background: rgba(183, 121, 31, 0.14); color: #b7791f; }
.status-pill.is-danger { background: rgba(197, 48, 48, 0.13); color: #c53030; }
.status-pill.is-muted { background: rgba(113, 128, 150, 0.13); color: var(--text-secondary, #6b7280); }
.card-body {
  display: grid; grid-template-columns: minmax(270px, auto) minmax(240px, 1fr) auto;
  align-items: center; gap: 14px; padding: 5px 15px 13px;
}
.length-flow { display: flex; align-items: center; gap: 9px; }
.length-flow > div:not(.saving-chip):not(.full-source-chip) {
  display: grid; grid-template-columns: auto auto auto; align-items: baseline; gap: 4px;
}
.length-flow span, .length-flow small { color: var(--text-secondary, #6b7280); font-size: 0.7rem; }
.length-flow strong { font-size: 0.98rem; }
.flow-arrow { opacity: 0.5; }
.saving-chip, .full-source-chip { padding: 3px 7px; border-radius: 999px; font-size: 0.7rem; font-weight: 700; }
.saving-chip { background: rgba(47, 133, 90, 0.13); color: #2f855a; }
.full-source-chip { background: rgba(113, 128, 150, 0.12); color: var(--text-secondary, #6b7280); }
.summary-excerpt {
  overflow: hidden; margin: 0; color: var(--text-secondary, #6b7280); font-size: 0.78rem;
  line-height: 1.5; text-overflow: ellipsis; white-space: nowrap;
}
.summary-excerpt.is-empty { opacity: 0.65; font-style: italic; }
.detail-toggle { border: 0; background: transparent; color: var(--primary-color, #3182ce); cursor: pointer; font-size: 0.76rem; white-space: nowrap; }
.detail-toggle span { margin-left: 4px; }
.detail-panel {
  display: grid; gap: 12px; padding: 14px 15px 16px;
  border-top: 1px solid var(--border-color, rgba(128, 128, 128, 0.18));
  background: var(--surface-muted, rgba(127, 127, 127, 0.04));
}
.callout { padding: 9px 11px; border-radius: 8px; font-size: 0.78rem; line-height: 1.55; }
.callout.is-warning { border: 1px solid rgba(183, 121, 31, 0.24); background: rgba(183, 121, 31, 0.08); color: #9c6515; }
.compare-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.text-panel, .effective-summary-panel, .routing-panel {
  min-width: 0; padding: 12px; border: 1px solid var(--border-color, rgba(128, 128, 128, 0.2));
  border-radius: 10px; background: var(--surface-color, rgba(127, 127, 127, 0.025));
}
.panel-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 8px; font-size: 0.8rem; font-weight: 700; }
.panel-heading small { color: var(--text-secondary, #6b7280); font-size: 0.7rem; font-weight: 400; }
.text-panel pre {
  max-height: 360px; overflow: auto; margin: 0; padding: 10px; border-radius: 8px;
  background: rgba(127, 127, 127, 0.07); font-family: inherit; font-size: 0.79rem;
  line-height: 1.65; white-space: pre-wrap; word-break: break-word;
}
.text-panel pre.empty { color: var(--text-secondary, #6b7280); font-style: italic; }
.effective-summary-panel p { margin: 0; font-size: 0.8rem; line-height: 1.6; }
.routing-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.routing-heading > div { display: grid; gap: 3px; }
.routing-heading > div > span { font-size: 0.82rem; font-weight: 700; }
.routing-heading small, .candidate-note, .routing-empty { color: var(--text-secondary, #6b7280); font-size: 0.72rem; }
.rule-content { margin-top: 12px; }
.always-include { padding: 8px 10px; border-radius: 8px; background: rgba(49, 130, 206, 0.09); font-size: 0.78rem; }
.rule-clauses { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.rule-clause {
  display: inline-flex; flex-wrap: wrap; align-items: center; gap: 5px; padding: 6px 8px;
  border: 1px solid rgba(49, 130, 206, 0.2); border-radius: 8px; background: rgba(49, 130, 206, 0.06);
}
.tag-chip {
  padding: 2px 6px; border-radius: 5px; background: rgba(49, 130, 206, 0.12);
  color: var(--primary-color, #3182ce); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.68rem;
}
.and-note, .or-label { color: var(--text-secondary, #6b7280); font-size: 0.65rem; }
.candidate-note, .routing-empty { margin: 9px 0 0; }
.metadata-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0; }
.metadata-grid > div { min-width: 0; padding: 8px 10px; border-radius: 8px; background: rgba(127, 127, 127, 0.065); }
.metadata-grid dt { color: var(--text-secondary, #6b7280); font-size: 0.67rem; }
.metadata-grid dd { overflow: hidden; margin: 3px 0 0; font-size: 0.75rem; text-overflow: ellipsis; white-space: nowrap; }
.hash-value { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.empty-state {
  padding: 34px 18px; border: 1px dashed var(--border-color, rgba(128, 128, 128, 0.32));
  border-radius: 12px; color: var(--text-secondary, #6b7280); text-align: center; font-size: 0.84rem;
}
.pagination { display: flex; align-items: center; justify-content: center; gap: 14px; color: var(--text-secondary, #6b7280); font-size: 0.78rem; }
.compat-tools {
  padding: 10px 12px; border: 1px dashed var(--border-color, rgba(128, 128, 128, 0.3));
  border-radius: 10px; color: var(--text-secondary, #6b7280); font-size: 0.78rem;
}
.compat-tools summary { color: inherit; cursor: pointer; font-weight: 650; }
.compat-tools p { max-width: 850px; margin: 9px 0; line-height: 1.55; }
@media (max-width: 1180px) {
  .stats-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .card-body { grid-template-columns: minmax(270px, auto) 1fr; }
  .detail-toggle { grid-column: 1 / -1; justify-self: end; }
}
@media (max-width: 820px) {
  .overview-panel, .toolbar, .card-header { align-items: stretch; flex-direction: column; }
  .stats-grid, .metadata-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .toolbar-meta { justify-content: space-between; }
  .filter-row { align-items: flex-start; }
  .card-body, .compare-columns { grid-template-columns: 1fr; }
  .detail-toggle { justify-self: start; }
  .status-group { justify-content: flex-start; }
}
@media (max-width: 520px) {
  .stats-grid, .metadata-grid { grid-template-columns: 1fr; }
  .toolbar-meta, .length-flow { align-items: flex-start; flex-direction: column; }
  .flow-arrow { display: none; }
}
</style>
