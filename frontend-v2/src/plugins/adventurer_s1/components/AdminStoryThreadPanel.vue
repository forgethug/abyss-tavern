<template>
  <div class="story-thread-admin">
    <div v-if="stats" class="stats-bar">
      <span class="stat-chip">全服 {{ stats.total_threads }} 条线索</span>
      <span class="stat-chip highlight">{{ stats.adventurers_with_threads }} 名冒险者有隐藏线</span>
      <span class="stat-chip">覆盖率 {{ stats.coverage_pct }}%</span>
      <span class="stat-chip">人均 {{ stats.avg_threads_per_holder }} 条</span>
      <span class="stat-chip st-active">进行中 {{ stats.by_status?.active ?? 0 }}</span>
      <span class="stat-chip st-dormant">休眠 {{ stats.by_status?.dormant ?? 0 }}</span>
      <span class="stat-chip st-resolved">已收束 {{ stats.by_status?.resolved ?? 0 }}</span>
    </div>

    <div class="toolbar">
      <input
        v-model="search"
        type="text"
        class="search-input"
        placeholder="搜索标题、摘要、标签、冒险者、玩家…"
        @keydown.enter="fetchList(1)"
      >
      <select v-model="statusFilter" class="filter-select" @change="fetchList(1)">
        <option value="">全部状态</option>
        <option value="active">进行中</option>
        <option value="dormant">休眠</option>
        <option value="resolved">已收束</option>
      </select>
      <input
        v-model="advIdFilter"
        type="text"
        class="adv-id-input"
        placeholder="冒险者 ID"
        @keydown.enter="fetchList(1)"
      >
      <button type="button" class="btn-primary btn-sm" :disabled="loading" @click="fetchList(1)">搜索</button>
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="refreshAll">刷新</button>
      <button type="button" class="btn-primary btn-sm" @click="openCreate">＋ 新增</button>
    </div>

    <p v-if="error" class="error-text">{{ error }}</p>
    <div v-else-if="loading" class="empty">加载中…</div>
    <div v-else-if="items.length === 0" class="empty">暂无匹配的隐藏剧情线</div>
    <template v-else>
      <div v-if="summary" class="result-summary">
        当前筛选：共 {{ summary.total_threads }} 条线索，涉及 {{ summary.unique_adventurers }} 名冒险者
      </div>
      <div class="table-wrap">
        <table class="thread-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>标题</th>
              <th>冒险者</th>
              <th>玩家</th>
              <th>状态</th>
              <th>权重</th>
              <th>最近更新</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in items" :key="row.id">
              <td data-label="ID">{{ row.id }}</td>
              <td class="title-cell" data-label="标题">
                <span class="thread-title">{{ row.title }}</span>
                <p class="thread-summary">{{ row.summary }}</p>
                <div v-if="row.tags?.length" class="tag-row">
                  <span v-for="tag in row.tags" :key="tag" class="tag">{{ tag }}</span>
                </div>
              </td>
              <td data-label="冒险者">
                #{{ row.adventurer_id }} {{ row.adventurer_name }}
                <small class="muted">{{ statusLabel(row.adventurer_status) }}</small>
              </td>
              <td data-label="玩家">
                <template v-if="row.owner_id">
                  {{ row.nickname || row.username }}
                  <small class="muted">UID {{ row.owner_id }}</small>
                </template>
                <span v-else class="muted">系统/NPC</span>
              </td>
              <td data-label="状态"><span :class="['st-tag', 'st-' + row.status]">{{ statusLabel(row.status) }}</span></td>
              <td data-label="权重">{{ row.weight }}</td>
              <td class="time-cell" data-label="最近更新">{{ formatTime(row.last_touched_at) }}</td>
              <td class="ops-cell" data-label="操作">
                <button type="button" class="btn-secondary btn-xs" @click="openEdit(row)">编辑</button>
                <button type="button" class="btn-danger btn-xs" :disabled="deletingId === row.id" @click="confirmDelete(row)">
                  {{ deletingId === row.id ? '删除中…' : '删除' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <Pagination v-if="pagination" :pagination="pagination" @go="fetchList" />
    </template>

    <Teleport to="body">
      <div v-if="editModal" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeEditModal">
        <div class="modal-content modal-lg s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h5>{{ editModal.mode === 'create' ? '新增隐藏剧情线' : `编辑 #${editModal.id}` }}</h5>
            <button type="button" class="modal-close" @click="closeEditModal">✕</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <div v-if="editModal.mode === 'create'" class="form-row">
              <label>冒险者 ID <span class="req">*</span></label>
              <input v-model.number="editForm.adventurer_id" type="number" min="1" placeholder="冒险者 ID">
            </div>
            <div v-else class="form-row readonly-row">
              <label>冒险者</label>
              <span>#{{ editForm.adventurer_id }} {{ editForm.adventurer_name }}</span>
            </div>
            <div class="form-row">
              <label>标题 <span class="req">*</span></label>
              <input v-model="editForm.title" type="text" maxlength="80" placeholder="2~80 字">
            </div>
            <div class="form-row">
              <label>摘要 <span class="req">*</span></label>
              <textarea
                v-model="editForm.summary"
                rows="4"
                maxlength="240"
                placeholder="最多 240 字，每次覆盖写入"
              />
              <small class="muted">{{ (editForm.summary || '').length }}/240</small>
            </div>
            <div class="form-row">
              <label>标签</label>
              <input v-model="editForm.tagsText" type="text" placeholder="逗号分隔，最多 8 个">
            </div>
            <div class="form-row inline-row">
              <div class="inline-field">
                <label>状态</label>
                <select v-model="editForm.status">
                  <option value="active">进行中</option>
                  <option value="dormant">休眠</option>
                  <option value="resolved">已收束</option>
                </select>
              </div>
              <div class="inline-field">
                <label>权重</label>
                <input v-model.number="editForm.weight" type="number" min="1" max="100">
              </div>
            </div>
            <p v-if="editModal.mode === 'edit' && editForm.source_log_id" class="muted small">
              来源探险日志 #{{ editForm.source_log_id }}
            </p>
          </div>
          <div class="modal-footer s1-mobile-dialog-footer">
            <button type="button" class="btn-secondary" @click="closeEditModal">取消</button>
            <button type="button" class="btn-primary" :disabled="saving" @click="saveEdit">
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Pagination from '@/components/Pagination.vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const stats = ref<any>(null)
const search = ref('')
const statusFilter = ref('')
const advIdFilter = ref('')
const loading = ref(false)
const error = ref('')
const items = ref<any[]>([])
const summary = ref<any>(null)
const pagination = ref<any>(null)
const deletingId = ref<number | null>(null)

const editModal = ref<{ mode: 'create' | 'edit'; id?: number } | null>(null)
const editForm = ref<any>({})
const saving = ref(false)

function statusLabel(s: string) {
  const map: Record<string, string> = {
    active: '进行中',
    dormant: '休眠',
    resolved: '已收束',
    resting: '休息', in_party: '组队', exploring: '探险', building: '建造',
    skill_school_queue: '等候导师指点',
    building_queue: '等候设施空位',
    dead: '死亡', archived: '封存',
  }
  return map[s] || s
}

function formatTime(iso: string | null | undefined) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function tagsToText(tags: string[] | null | undefined) {
  return (tags || []).join('，')
}

function textToTags(text: string) {
  return text.split(/[,，]/).map(t => t.trim()).filter(Boolean)
}

async function fetchStats() {
  const res = await store.adminStoryThreadStats()
  if (res?.code === 200) stats.value = res.data
}

async function fetchList(page = 1) {
  loading.value = true
  error.value = ''
  try {
    const advRaw = advIdFilter.value.trim()
    const res = await store.adminSearchStoryThreads({
      search: search.value.trim() || undefined,
      status: statusFilter.value || undefined,
      adventurer_id: advRaw && /^\d+$/.test(advRaw) ? Number(advRaw) : undefined,
      page,
      per_page: 20,
    })
    if (res?.code !== 200) {
      error.value = res?.message || '加载失败'
      items.value = []
      summary.value = null
      pagination.value = null
      return
    }
    items.value = res.data?.items || []
    summary.value = res.data?.summary || null
    pagination.value = {
      current_page: res.data?.page || 1,
      pages: res.data?.pages || 1,
      total: res.data?.total || 0,
    }
  } finally {
    loading.value = false
  }
}

async function refreshAll() {
  await Promise.all([fetchStats(), fetchList(pagination.value?.current_page || 1)])
}

function openCreate() {
  editModal.value = { mode: 'create' }
  editForm.value = {
    adventurer_id: advIdFilter.value && /^\d+$/.test(advIdFilter.value.trim())
      ? Number(advIdFilter.value.trim())
      : null,
    title: '',
    summary: '',
    tagsText: '',
    status: 'active',
    weight: 50,
  }
}

function openEdit(row: any) {
  editModal.value = { mode: 'edit', id: row.id }
  editForm.value = {
    adventurer_id: row.adventurer_id,
    adventurer_name: row.adventurer_name,
    title: row.title,
    summary: row.summary,
    tagsText: tagsToText(row.tags),
    status: row.status,
    weight: row.weight,
    source_log_id: row.source_log_id,
  }
}

function closeEditModal() {
  editModal.value = null
  editForm.value = {}
}

async function saveEdit() {
  const form = editForm.value
  if (editModal.value?.mode === 'create' && !form.adventurer_id) {
    message.error('请填写冒险者 ID')
    return
  }
  if (!form.title?.trim()) {
    message.error('请填写标题')
    return
  }
  if (!form.summary?.trim()) {
    message.error('请填写摘要')
    return
  }

  saving.value = true
  try {
    const payload = {
      title: form.title.trim(),
      summary: form.summary.trim(),
      tags: textToTags(form.tagsText || ''),
      status: form.status,
      weight: form.weight,
    }
    let res
    if (editModal.value?.mode === 'create') {
      res = await store.adminCreateStoryThread({
        adventurer_id: form.adventurer_id,
        ...payload,
      })
    } else {
      res = await store.adminUpdateStoryThread(editModal.value!.id!, payload)
    }
    if (res?.code === 200 || res?.code === 201) {
      message.success(editModal.value?.mode === 'create' ? '已新增' : '已保存')
      closeEditModal()
      await refreshAll()
    } else {
      message.error(res?.message || '保存失败')
    }
  } finally {
    saving.value = false
  }
}

async function confirmDelete(row: any) {
  if (!window.confirm(`确定删除剧情线「${row.title}」？此操作不可恢复。`)) return
  deletingId.value = row.id
  try {
    const res = await store.adminDeleteStoryThread(row.id)
    if (res?.code === 200) {
      message.success('已删除')
      await refreshAll()
    } else {
      message.error(res?.message || '删除失败')
    }
  } finally {
    deletingId.value = null
  }
}

onMounted(async () => {
  await refreshAll()
})
</script>

<style scoped lang="scss">
.story-thread-admin {
  margin-top: 8px;
}

.stats-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.stat-chip {
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);

  &.highlight {
    color: var(--color-text);
    border-color: var(--color-primary, #6366f1);
  }

  &.st-active { border-color: #22c55e; color: #16a34a; }
  &.st-dormant { border-color: #94a3b8; }
  &.st-resolved { border-color: #a78bfa; color: #7c3aed; }
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.search-input {
  flex: 1;
  min-width: 200px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.filter-select,
.adv-id-input {
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.adv-id-input {
  width: 100px;
}

.error-text {
  color: #ef4444;
  margin: 8px 0;
}

.empty {
  padding: 24px;
  text-align: center;
  color: var(--color-text-secondary);
}

.result-summary {
  margin-bottom: 8px;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.table-wrap {
  overflow-x: auto;
}

.thread-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;

  th, td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--color-border);
    text-align: left;
    vertical-align: top;
  }

  th {
    color: var(--color-text-secondary);
    font-weight: 600;
    white-space: nowrap;
  }
}

.title-cell {
  min-width: 220px;
  max-width: 360px;
}

.thread-title {
  font-weight: 600;
}

.thread-summary {
  margin: 4px 0 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
}

.st-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;

  &.st-active { background: rgba(34, 197, 94, 0.15); color: #16a34a; }
  &.st-dormant { background: rgba(148, 163, 184, 0.2); color: var(--color-text-secondary); }
  &.st-resolved { background: rgba(167, 139, 250, 0.2); color: #7c3aed; }
}

.time-cell {
  white-space: nowrap;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.ops-cell {
  white-space: nowrap;

  button + button {
    margin-left: 4px;
  }
}

.muted {
  color: var(--color-text-secondary);
}

.small {
  font-size: 12px;
}

.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
  padding: 16px;
}

.modal-content {
  width: 100%;
  max-width: 560px;
  max-height: 90vh;
  overflow-y: auto;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
}

.modal-lg {
  max-width: 640px;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--color-border);

  h5 {
    margin: 0;
    font-size: 16px;
  }
}

.modal-close {
  border: none;
  background: none;
  font-size: 18px;
  cursor: pointer;
  color: var(--color-text-secondary);
}

.modal-body {
  padding: 16px;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--color-border);
}

.form-row {
  margin-bottom: 14px;

  label {
    display: block;
    margin-bottom: 4px;
    font-size: 13px;
    font-weight: 600;
  }

  input[type='text'],
  input[type='number'],
  textarea,
  select {
    width: 100%;
    padding: 8px 10px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: var(--color-bg);
    color: var(--color-text);
    box-sizing: border-box;
  }

  textarea {
    resize: vertical;
    min-height: 80px;
  }
}

.inline-row {
  display: flex;
  gap: 16px;
}

.inline-field {
  flex: 1;
}

.readonly-row span {
  font-size: 14px;
}

.req {
  color: #ef4444;
}

@media (max-width: 640px) {
  .stats-bar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
  }

  .stat-chip {
    padding: 7px 9px;
    text-align: center;
  }

  .toolbar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .toolbar > * {
    min-width: 0;
    width: 100%;
    min-height: 40px;
  }

  .toolbar .search-input {
    grid-column: 1 / -1;
  }

  .toolbar .btn-primary:last-child {
    grid-column: 1 / -1;
  }

  .adv-id-input {
    width: auto;
  }

  .table-wrap {
    overflow: visible;
  }

  .thread-table,
  .thread-table tbody {
    display: block;
  }

  .thread-table thead {
    display: none;
  }

  .thread-table tr {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 10px;
    padding: 12px;
    border: 1px solid var(--color-border);
    border-radius: 10px;
    background: var(--color-surface);
  }

  .thread-table td {
    display: grid;
    gap: 4px;
    min-width: 0;
    padding: 0;
    border: 0;
    white-space: normal;
    overflow-wrap: anywhere;
  }

  .thread-table td::before {
    content: attr(data-label);
    font-size: 11px;
    font-weight: 600;
    color: var(--color-text-secondary);
  }

  .thread-table .title-cell,
  .thread-table .ops-cell {
    grid-column: 1 / -1;
    min-width: 0;
  }

  .ops-cell {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .ops-cell::before {
    grid-column: 1 / -1;
  }

  .ops-cell button {
    min-height: 38px;
    margin: 0;
  }

  .ops-cell button + button {
    margin-left: 0;
  }

  .modal-overlay {
    align-items: flex-end;
    padding: 8px;
  }

  .modal-content {
    max-width: none;
    max-height: calc(100dvh - 16px);
    border-radius: 16px 16px 10px 10px;
  }

  .modal-header,
  .modal-body {
    padding: 12px;
  }

  .modal-footer {
    padding: 10px 12px 12px;
  }

  .modal-footer button {
    flex: 1;
    min-height: 40px;
  }

  .inline-row {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>
