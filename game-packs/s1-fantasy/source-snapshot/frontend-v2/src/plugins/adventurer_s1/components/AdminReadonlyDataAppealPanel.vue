<template>
  <div class="readonly-appeal-panel">
    <div class="filter-bar">
      <select v-model="statusFilter" @change="fetchList(1)">
        <option value="">全部状态</option>
        <option value="processing">AI 处理中</option>
        <option value="awaiting_review">待审核</option>
        <option value="approved">已通过</option>
        <option value="rejected">已驳回</option>
        <option value="cancelled">已结束</option>
        <option value="reverted">已还原</option>
      </select>
      <input
        v-model="searchQuery"
        type="search"
        placeholder="申诉 ID / 玩家 / 冒险者 / 描述"
        @keydown.enter.prevent="applySearch"
      >
      <button type="button" class="btn-primary btn-sm" :disabled="loading" @click="applySearch">搜索</button>
      <button
        v-if="activeSearch"
        type="button"
        class="btn-secondary btn-sm"
        :disabled="loading"
        @click="clearSearch"
      >清除</button>
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="fetchList(page)">刷新</button>
    </div>

    <p v-if="activeSearch" class="search-hint">
      当前搜索：<strong>{{ activeSearch }}</strong>
      <span v-if="pagination"> · 共 {{ pagination.total }} 条</span>
    </p>

    <div v-if="loading && !listLoaded" class="empty">加载中…</div>
    <div v-else-if="listError && !listLoaded" class="empty">{{ listError }}</div>
    <div v-else-if="items.length === 0" class="empty">
      {{ activeSearch ? `未找到与「${activeSearch}」匹配的申诉` : '暂无申诉' }}
    </div>
    <div v-else class="table-wrap">
      <table class="appeal-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>玩家</th>
            <th>冒险者</th>
            <th>状态</th>
            <th>AI</th>
            <th>时间</th>
            <th>查看</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="appeal in items" :key="appeal.id">
            <td data-label="申诉 ID">#{{ appeal.id }}</td>
            <td data-label="玩家">{{ appeal.nickname || appeal.username || '—' }}</td>
            <td data-label="冒险者">{{ appeal.adventurer_name || '—' }}</td>
            <td data-label="状态">
              <span :class="['status-tag', `status-${appeal.status}`]">
                {{ appeal.status_label || statusLabel(appeal.status) }}
              </span>
            </td>
            <td data-label="AI">{{ aiStatusLabel(appeal.ai_status) }}</td>
            <td data-label="时间">{{ formatTime(appeal.created_at) }}</td>
            <td data-label="查看">
              <button
                type="button"
                class="btn-secondary btn-xs"
                :disabled="detailLoading"
                @click="openDetail(appeal.id)"
              >详情</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="listError && listLoaded" class="muted" role="status">{{ listError }}</p>
    <Pagination v-if="pagination" :pagination="pagination" @go="fetchList" />

    <Teleport to="body">
      <div v-if="detail" class="detail-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeDetail">
        <article class="detail-modal s1-mobile-dialog">
          <header class="s1-mobile-dialog-header">
            <div>
              <small>READ ONLY REVIEW</small>
              <h5>申诉 #{{ detail.id }} · {{ detail.adventurer_name }}</h5>
            </div>
            <button type="button" class="modal-close" aria-label="关闭详情" @click="closeDetail">×</button>
          </header>

          <div class="detail-body s1-mobile-dialog-body">
            <dl class="detail-summary">
              <div>
                <dt>玩家</dt>
                <dd>{{ detail.nickname || detail.username || '—' }}</dd>
              </div>
              <div>
                <dt>状态</dt>
                <dd>{{ detail.status_label || statusLabel(detail.status) }}</dd>
              </div>
              <div>
                <dt>AI 状态</dt>
                <dd>{{ aiStatusLabel(detail.ai_status) }}</dd>
              </div>
              <div>
                <dt>提交时间</dt>
                <dd>{{ formatTime(detail.created_at) }}</dd>
              </div>
            </dl>

            <section>
              <h6>玩家描述</h6>
              <p class="preserve-text">{{ detail.player_note || '未填写' }}</p>
              <p v-if="detail.target_labels?.length" class="muted">
                申诉条目：{{ detail.target_labels.join('、') }}
              </p>
            </section>

            <section v-if="detail.expedition_evidence">
              <h6>关联探险证据</h6>
              <p class="muted">
                {{ detail.expedition_evidence.target_dungeon || '未知副本' }}
                <span v-if="detail.expedition_evidence.party_name">
                  · {{ detail.expedition_evidence.party_name }}
                </span>
              </p>
              <blockquote v-if="detail.expedition_evidence.event_text" class="evidence">
                {{ detail.expedition_evidence.event_text }}
              </blockquote>
              <details v-if="detail.expedition_evidence.member_update" class="json-block">
                <summary>探险结算摘要</summary>
                <pre>{{ stringify(detail.expedition_evidence.member_update) }}</pre>
              </details>
            </section>

            <section>
              <h6>AI 审核</h6>
              <p v-if="detail.ai_summary" class="preserve-text">{{ detail.ai_summary }}</p>
              <p v-else class="muted">暂无一审摘要。</p>
              <p v-if="detail.review_ai_summary" class="preserve-text">
                <strong>二次复核：</strong>{{ detail.review_ai_summary }}
              </p>
              <p v-if="detail.reject_reason" class="preserve-text">
                <strong>{{ detail.reject_reason_label || '最终未通过原因' }}：</strong>
                {{ detail.reject_reason }}
              </p>
              <details v-if="detail.ai_raw_response" class="json-block">
                <summary>一审完整输出</summary>
                <pre>{{ stringify(parseJson(detail.ai_raw_response)) }}</pre>
              </details>
              <details v-if="detail.review_ai_raw_response" class="json-block">
                <summary>二次复核完整输出</summary>
                <pre>{{ stringify(parseJson(detail.review_ai_raw_response)) }}</pre>
              </details>
            </section>

            <section>
              <h6>变更与落库证据</h6>
              <p class="execution-state">
                {{ executionStateLabel(detail.execution_state) }}
                <span v-if="detail.applied_change_count != null">
                  · {{ detail.applied_change_count }} 条变更
                </span>
              </p>
              <details v-if="detail.preview" class="json-block">
                <summary>AI 拟议变更预览</summary>
                <pre>{{ stringify(detail.preview) }}</pre>
              </details>
              <details v-if="detail.proposed_patch" class="json-block">
                <summary>原始 Patch</summary>
                <pre>{{ stringify(detail.proposed_patch) }}</pre>
              </details>
              <details v-if="detail.audit_logs?.length" class="json-block" open>
                <summary>落库变更台账</summary>
                <pre>{{ stringify(detail.audit_logs) }}</pre>
              </details>
              <p v-if="!detail.preview && !detail.proposed_patch && !detail.audit_logs?.length" class="muted">
                暂无变更记录。
              </p>
            </section>
          </div>
        </article>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Pagination from '@/components/Pagination.vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatDateTime } from '@/utils/time'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { useS1AppealRefresh } from '../composables/useS1AppealRefresh'

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const { refreshing: loading, refresh: refreshList } = useS1AppealRefresh(fetchListSnapshot)
const listLoaded = ref(false)
const listError = ref('')
const detailLoading = ref(false)
const items = ref<any[]>([])
const pagination = ref<any>(null)
const page = ref(1)
const statusFilter = ref('awaiting_review')
const searchQuery = ref('')
const activeSearch = ref('')
const detail = ref<any>(null)

function statusLabel(status: string): string {
  return ({
    processing: 'AI 处理中',
    awaiting_review: '待审核',
    approved: '已通过',
    rejected: '已驳回',
    cancelled: '已结束',
    reverted: '已还原',
  } as Record<string, string>)[status] || status || '未知'
}

function aiStatusLabel(status: string): string {
  return ({
    pending: '排队',
    processing: '分析中',
    done: '完成',
    failed: '失败',
    cancelled: '已取消',
  } as Record<string, string>)[status] || status || '未开始'
}

function executionStateLabel(state: string): string {
  return ({
    not_applied: '未落库',
    partially_applied: '部分变更已落库',
    applied: '变更已落库',
    reverted: '落库变更已撤销',
  } as Record<string, string>)[state] || '落库状态未知'
}

function formatTime(value: string | null | undefined): string {
  return value ? formatDateTime(value) : '—'
}

function parseJson(value: unknown): unknown {
  if (typeof value !== 'string') return value
  try {
    return JSON.parse(value)
  } catch {
    return value
  }
}

function stringify(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2)
}

function applySearch() {
  activeSearch.value = searchQuery.value.trim()
  void fetchList(1)
}

function clearSearch() {
  searchQuery.value = ''
  activeSearch.value = ''
  void fetchList(1)
}

function fetchList(targetPage = 1) {
  page.value = targetPage
  return refreshList()
}

async function fetchListSnapshot(isActive: () => boolean) {
  const params = {
    status: statusFilter.value || undefined,
    search: activeSearch.value || undefined,
    page: page.value,
    per_page: 20,
  }
  const isCurrent = () => isActive()
    && params.page === page.value
    && params.status === (statusFilter.value || undefined)
    && params.search === (activeSearch.value || undefined)
  try {
    const res = await store.adminListDataAppeals(params)
    if (!isCurrent()) return
    if (res?.code !== 200) {
      listError.value = res?.message || '加载申诉列表失败'
      return
    }
    items.value = res.data?.items || []
    listLoaded.value = true
    listError.value = ''
    pagination.value = {
      current_page: res.data?.page || 1,
      pages: Math.max(1, Math.ceil((res.data?.total || 0) / (res.data?.per_page || 20))),
      total: res.data?.total || 0,
    }
  } catch (error: any) {
    if (isCurrent()) listError.value = error?.response?.data?.message || '加载申诉列表失败'
  }
}

async function openDetail(appealId: number) {
  detailLoading.value = true
  try {
    const res = await store.adminGetDataAppeal(appealId)
    if (res?.code !== 200) {
      message.error(res?.message || '加载申诉详情失败')
      return
    }
    detail.value = res.data
  } catch (error: any) {
    message.error(error?.response?.data?.message || '加载申诉详情失败')
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detail.value = null
}

onMounted(() => {
  void fetchList(1)
})
</script>

<style scoped lang="scss">
.readonly-appeal-panel { min-width: 0; }

.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;

  select,
  input {
    min-height: 36px;
    padding: 7px 10px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 7px;
  }

  input {
    flex: 1;
    min-width: 220px;
  }
}

.search-hint,
.muted {
  color: var(--color-text-secondary);
  font-size: 13px;
}

.empty {
  padding: 24px;
  color: var(--color-text-secondary);
  text-align: center;
}

.table-wrap {
  max-width: 100%;
  overflow-x: auto;
}

.appeal-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;

  th,
  td {
    padding: 8px;
    text-align: left;
    border-bottom: 1px solid var(--color-border);
  }
}

.status-tag {
  display: inline-flex;
  padding: 2px 7px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
}

.status-awaiting_review { color: #d19a43; }
.status-approved { color: #57a773; }
.status-rejected { color: #d26b6b; }

.detail-overlay {
  position: fixed;
  z-index: 1200;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 18px;
  background: rgba(0, 0, 0, 0.68);
}

.detail-modal {
  display: flex;
  flex-direction: column;
  width: min(920px, 100%);
  max-height: min(92vh, 980px);
  overflow: hidden;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 14px;

  > header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 16px 18px;
    border-bottom: 1px solid var(--color-border);

    small {
      color: var(--color-text-secondary);
      letter-spacing: .12em;
    }

    h5 { margin: 4px 0 0; }
  }
}

.modal-close {
  width: 36px;
  height: 36px;
  color: var(--color-text);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 50%;
  cursor: pointer;
}

.detail-body {
  min-height: 0;
  padding: 18px;
  overflow-y: auto;

  section + section {
    margin-top: 18px;
    padding-top: 18px;
    border-top: 1px solid var(--color-border);
  }

  h6 { margin: 0 0 8px; }
}

.detail-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 0 0 18px;

  div {
    padding: 10px;
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 8px;
  }

  dt {
    color: var(--color-text-secondary);
    font-size: 11px;
  }

  dd {
    margin: 4px 0 0;
    overflow-wrap: anywhere;
  }
}

.preserve-text,
.evidence {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.evidence {
  margin: 8px 0;
  padding: 10px 12px;
  background: var(--color-bg);
  border-left: 3px solid var(--color-primary);
}

.execution-state {
  padding: 10px 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.json-block {
  margin-top: 8px;

  summary {
    color: var(--color-primary);
    cursor: pointer;
  }

  pre {
    max-height: 360px;
    padding: 12px;
    overflow: auto;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
}

@media (max-width: 720px) {
  .filter-bar {
    display: grid;
    grid-template-columns: 1fr;

    input {
      min-width: 0;
      font-size: 16px;
    }
  }

  .appeal-table,
  .appeal-table tbody,
  .appeal-table tr,
  .appeal-table td {
    display: block;
    width: 100%;
    box-sizing: border-box;
  }

  .appeal-table thead { display: none; }

  .appeal-table tr {
    margin-bottom: 10px;
    overflow: hidden;
    border: 1px solid var(--color-border);
    border-radius: 9px;
  }

  .appeal-table td {
    display: grid;
    grid-template-columns: 84px minmax(0, 1fr);
    gap: 10px;
  }

  .appeal-table td::before {
    content: attr(data-label);
    color: var(--color-text-secondary);
    font-weight: 600;
  }

  .detail-overlay { padding: 8px; }
  .detail-modal { max-height: 96dvh; }
  .detail-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
