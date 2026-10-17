<template>
  <Teleport to="body">
  <div class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="emit('close')">
    <div class="modal-card s1-mobile-dialog">
      <header class="modal-head s1-mobile-dialog-header">
        <h3 class="modal-title">文本代号更正</h3>
        <button type="button" class="modal-close" @click="emit('close')">✕</button>
      </header>

      <div class="modal-body s1-mobile-dialog-body">
        <p class="hint">
          扫描技能、特质、装备与冒险史中的探险代号残留。请<strong>勾选要更正的具体词条</strong>，
          并为该条内的代号指定替换成的酒馆冒险者真名；未勾选的条目不会被改动。
          若判定为误报（如「甲」是正常用词），可点<strong>标记无错</strong>，下次不再提示。
        </p>
        <p class="hint warn">
          单字天干（甲/乙/丙…）可能误伤正常用词；皮甲、臂甲、锁子甲、自己、丙烯、几丁质、辛苦等已自动过滤。
          冒险史按段落逐条展示，请对照上下文逐条确认后再勾选。
        </p>

        <div v-if="preview?.has_backup" class="backup-bar">
          <span class="muted small">上次更正已备份，可一键撤回。</span>
          <button
            type="button"
            class="btn-restore"
            :disabled="restoring"
            @click="restore"
          >{{ restoring ? '撤回中…' : '撤回上次更正' }}</button>
        </div>

        <div v-if="loading" class="state">扫描文本中…</div>
        <div v-else-if="loadError" class="state error">{{ loadError }}</div>
        <div v-else-if="!preview?.has_issues" class="state">未发现需要更正的探险代号。</div>

        <template v-else>
          <div class="summary-bar">
            共发现 <strong>{{ preview.occurrences.length }}</strong> 处含代号的词条，
            已勾选 <strong>{{ selectedCount }}</strong> 处
            <span v-if="(preview.dismissed_count ?? 0) > 0" class="dismissed-note">
              · 已标记无错 {{ preview.dismissed_count }} 处（不再显示）
            </span>
            <span v-if="occurrencePagination.pages > 1" class="page-info">
              · 第 {{ occurrencePagination.current_page }}/{{ occurrencePagination.pages }} 页
            </span>
          </div>

          <div
            v-for="occ in pagedOccurrences"
            :key="occ.id"
            class="occurrence-row"
            :class="{ selected: isSelected(occ.id) }"
          >
            <label class="occ-check">
              <input
                type="checkbox"
                :checked="isSelected(occ.id)"
                @change="toggleOccurrence(occ.id, ($event.target as HTMLInputElement).checked)"
              />
              <span class="occ-title">
                {{ occ.field_label }} ·
                <span class="inline-text">
                  <template v-for="(part, idx) in highlightParts(occ.record_name, occ.tokens)" :key="`rn-${idx}`">
                    <mark v-if="part.highlight" class="token-highlight">{{ part.text }}</mark>
                    <span v-else>{{ part.text }}</span>
                  </template>
                </span>
                <span class="attr-tag">{{ occ.attribute_label }}</span>
              </span>
            </label>

            <div class="snippet">
              <template v-for="(part, idx) in highlightParts(occ.snippet, occ.tokens)" :key="`sn-${idx}`">
                <mark v-if="part.highlight" class="token-highlight">{{ part.text }}</mark>
                <span v-else>{{ part.text }}</span>
              </template>
            </div>

            <div class="token-rows">
              <div
                v-for="token in occ.tokens"
                :key="token"
                class="token-row"
              >
                <span class="token-badge">{{ token }}</span>
                <span v-if="occ.hint_names?.length" class="hint-names muted small">
                  曾映射：{{ occ.hint_names.join('、') }}
                </span>
                <select
                  v-model="tokenSelections[occ.id][token]"
                  class="name-select"
                  :disabled="!isSelected(occ.id)"
                >
                  <option value="">— 不替换此代号 —</option>
                  <option
                    v-for="name in nameOptions(occ)"
                    :key="name"
                    :value="name"
                  >{{ name }}</option>
                </select>
              </div>
            </div>

            <div class="occ-actions">
              <button
                type="button"
                class="btn-dismiss"
                :disabled="dismissingId === occ.id"
                @click="dismissOccurrence(occ)"
              >{{ dismissingId === occ.id ? '标记中…' : '标记无错，不再提示' }}</button>
            </div>
          </div>

          <Pagination
            v-if="occurrencePagination.pages > 1"
            :pagination="occurrencePagination"
            @go="goOccurrencePage"
          />

          <div v-if="applyError" class="apply-error">{{ applyError }}</div>

          <div class="actions">
            <button type="button" class="btn-secondary" @click="emit('close')">取消</button>
            <button
              type="button"
              class="btn-primary"
              :disabled="!canApply || applying"
              @click="apply"
            >{{ applying ? '替换中…' : '确认替换已勾选' }}</button>
          </div>
        </template>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref } from 'vue'
import Pagination from '@/components/Pagination.vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'

const PAGE_SIZE = 25

interface SnippetPart {
  text: string
  highlight: boolean
}

interface OccurrenceItem {
  id: string
  field_type: string
  field_label: string
  record_id: number
  record_name: string
  attribute: string
  attribute_label: string
  tokens: string[]
  snippet: string
  hint_names: string[]
}

interface PreviewData {
  adventurer_id: number
  adventurer_name: string
  has_issues: boolean
  occurrences: OccurrenceItem[]
  candidate_names: string[]
  has_backup: boolean
  backup_at?: string | null
  dismissed_count?: number
}

const props = defineProps<{
  advId: number
}>()

const emit = defineEmits<{
  close: []
  applied: []
}>()

const store = useAdventurerS1Store()
const message = inject<{ success: (s: string) => void; error: (s: string) => void } | undefined>('message')

const loading = ref(true)
const applying = ref(false)
const restoring = ref(false)
const dismissingId = ref<string | null>(null)
const loadError = ref('')
const applyError = ref('')
const preview = ref<PreviewData | null>(null)
const currentPage = ref(1)

/** 勾选的词条 id */
const selectedIds = reactive<Record<string, boolean>>({})
/** occurrence_id -> token -> chosen name */
const tokenSelections = reactive<Record<string, Record<string, string>>>({})

const selectedCount = computed(() =>
  Object.values(selectedIds).filter(Boolean).length,
)

const occurrencePagination = computed(() => {
  const total = preview.value?.occurrences.length ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  return {
    total,
    pages,
    current_page: Math.min(currentPage.value, pages),
  }
})

const pagedOccurrences = computed(() => {
  const items = preview.value?.occurrences ?? []
  if (items.length <= PAGE_SIZE) return items
  const page = occurrencePagination.value.current_page
  const start = (page - 1) * PAGE_SIZE
  return items.slice(start, start + PAGE_SIZE)
})

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** 按 tokens 切分 snippet/标题，命中片段用于高亮展示。 */
function highlightParts(text: string, tokens: string[]): SnippetPart[] {
  if (!text) return []
  const unique = [...new Set(tokens.filter(Boolean))]
  if (!unique.length) return [{ text, highlight: false }]
  const pattern = unique
    .sort((a, b) => b.length - a.length)
    .map(escapeRegExp)
    .join('|')
  const re = new RegExp(`(${pattern})`, 'g')
  const parts: SnippetPart[] = []
  let last = 0
  for (const match of text.matchAll(re)) {
    const idx = match.index ?? 0
    if (idx > last) {
      parts.push({ text: text.slice(last, idx), highlight: false })
    }
    parts.push({ text: match[0], highlight: true })
    last = idx + match[0].length
  }
  if (last < text.length) {
    parts.push({ text: text.slice(last), highlight: false })
  }
  return parts.length ? parts : [{ text, highlight: false }]
}

function goOccurrencePage(page: number) {
  currentPage.value = page
}

const canApply = computed(() => {
  for (const occ of preview.value?.occurrences ?? []) {
    if (!selectedIds[occ.id]) continue
    const picks = tokenSelections[occ.id] ?? {}
    if (occ.tokens.some(t => (picks[t] || '').trim())) return true
  }
  return false
})

function isSelected(id: string): boolean {
  return !!selectedIds[id]
}

function toggleOccurrence(id: string, checked: boolean) {
  selectedIds[id] = checked
}

function nameOptions(occ: OccurrenceItem): string[] {
  const set = new Set<string>()
  for (const n of preview.value?.candidate_names ?? []) set.add(n)
  for (const n of occ.hint_names ?? []) set.add(n)
  return [...set].sort((a, b) => a.localeCompare(b, 'zh-CN'))
}

function initSelections(data: PreviewData) {
  for (const key of Object.keys(selectedIds)) delete selectedIds[key]
  for (const key of Object.keys(tokenSelections)) delete tokenSelections[key]
  for (const occ of data.occurrences) {
    selectedIds[occ.id] = false
    const row: Record<string, string> = {}
    for (const token of occ.tokens) {
      const hints = occ.hint_names ?? []
      row[token] = occ.tokens.length === 1 && hints.length === 1 ? hints[0] : ''
    }
    tokenSelections[occ.id] = row
  }
}

async function loadPreview() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await store.fetchTextCorrectionPreview(props.advId)
    if (res?.code !== 200 || !res.data) {
      loadError.value = res?.message || '加载失败'
      preview.value = null
      return
    }
    const data = res.data as PreviewData
    if (!Array.isArray(data.occurrences)) {
      loadError.value = '预览数据异常，请刷新页面后重试'
      preview.value = null
      return
    }
    preview.value = data
    currentPage.value = 1
    initSelections(preview.value)
  } catch (err: unknown) {
    const e = err as { response?: { data?: { message?: string } }; message?: string; code?: string }
    if (e.code === 'ECONNABORTED') {
      loadError.value = '扫描超时，请稍后重试'
    } else {
      loadError.value = e.response?.data?.message || e.message || '加载失败'
    }
    preview.value = null
  } finally {
    loading.value = false
  }
}

function buildSelections() {
  const selections: {
    occurrence_id: string
    replacements: { token: string; name: string }[]
  }[] = []
  for (const occ of preview.value?.occurrences ?? []) {
    if (!selectedIds[occ.id]) continue
    const picks = tokenSelections[occ.id] ?? {}
    const replacements = occ.tokens
      .map(token => ({ token, name: (picks[token] || '').trim() }))
      .filter(r => r.name)
    if (replacements.length) {
      selections.push({ occurrence_id: occ.id, replacements })
    }
  }
  return selections
}

async function dismissOccurrence(occ: OccurrenceItem) {
  if (dismissingId.value) return
  dismissingId.value = occ.id
  try {
    const res = await store.dismissTextCorrectionOccurrence(props.advId, occ.id)
    if (res?.code !== 200) {
      message?.error(res?.message || '标记失败')
      return
    }
    message?.success('已标记为无错')
    delete selectedIds[occ.id]
    delete tokenSelections[occ.id]
    if (preview.value) {
      preview.value.occurrences = preview.value.occurrences.filter(item => item.id !== occ.id)
      preview.value.has_issues = preview.value.occurrences.length > 0
      preview.value.dismissed_count = (preview.value.dismissed_count ?? 0) + 1
      const pages = Math.max(1, Math.ceil(preview.value.occurrences.length / PAGE_SIZE))
      if (currentPage.value > pages) currentPage.value = pages
    }
  } catch (err: unknown) {
    const e = err as { response?: { status?: number; data?: { message?: string } | string } }
    const status = e.response?.status
    const data = e.response?.data
    const msg = typeof data === 'object' && data?.message
      ? data.message
      : status === 405
        ? '服务端接口未就绪，请重启后端后再试'
        : '标记失败'
    message?.error(msg)
  } finally {
    dismissingId.value = null
  }
}

async function apply() {
  if (!preview.value || !canApply.value) return
  applying.value = true
  applyError.value = ''
  try {
    const selections = buildSelections()
    const res = await store.applyTextCorrection(props.advId, selections)
    if (res?.code !== 200) {
      applyError.value = res?.message || '替换失败'
      message?.error(applyError.value)
      return
    }
    message?.success('已更正所选词条')
    emit('applied')
    emit('close')
  } catch (err: unknown) {
    const e = err as { response?: { data?: { message?: string } } }
    applyError.value = e.response?.data?.message || '替换失败'
    message?.error(applyError.value)
  } finally {
    applying.value = false
  }
}

async function restore() {
  if (!preview.value?.has_backup) return
  restoring.value = true
  try {
    const res = await store.restoreTextCorrection(props.advId)
    if (res?.code !== 200) {
      message?.error(res?.message || '撤回失败')
      return
    }
    message?.success('已撤回上次更正')
    emit('applied')
    await loadPreview()
  } catch (err: unknown) {
    const e = err as { response?: { data?: { message?: string } } }
    message?.error(e.response?.data?.message || '撤回失败')
  } finally {
    restoring.value = false
  }
}

onMounted(loadPreview)
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
}

.modal-card {
  width: 100%;
  max-width: 620px;
  max-height: 88vh;
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
  font-size: 17px;
  color: var(--color-text);
}

.modal-close {
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 18px;
  cursor: pointer;
  padding: 4px 8px;
}

.modal-body {
  padding: 16px 18px;
  overflow-y: auto;
}

.hint {
  font-size: 13px;
  color: var(--color-text-secondary);
  margin: 0 0 10px;
  line-height: 1.55;

  &.warn {
    color: #b8860b;
    padding: 8px 10px;
    border-radius: 6px;
    background: rgba(240, 160, 32, 0.08);
    border: 1px solid rgba(240, 160, 32, 0.25);
  }
}

.backup-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(24, 160, 88, 0.08);
  border: 1px solid rgba(24, 160, 88, 0.25);
}

.btn-restore {
  flex-shrink: 0;
  padding: 5px 12px;
  font-size: 12px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  cursor: pointer;

  &:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
}

.summary-bar {
  margin-bottom: 12px;
  font-size: 13px;
  color: var(--color-text);

  strong {
    color: var(--color-primary, #18a058);
  }

  .page-info {
    color: var(--color-text-secondary);
    font-size: 12px;
  }

  .dismissed-note {
    color: var(--color-text-secondary);
    font-size: 12px;
  }
}

.occurrence-row {
  margin-bottom: 12px;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  opacity: 0.82;

  &.selected {
    opacity: 1;
    border-color: rgba(24, 160, 88, 0.45);
    background: var(--color-surface);
  }
}

.occ-check {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  cursor: pointer;
  margin-bottom: 8px;

  input {
    margin-top: 3px;
  }
}

.occ-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  line-height: 1.4;
}

.attr-tag {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  font-size: 11px;
  font-weight: 500;
  border-radius: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
}

.snippet {
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-text);
  word-break: break-word;
  margin-bottom: 8px;
  padding-left: 22px;
  white-space: pre-wrap;
}

.inline-text {
  word-break: break-word;
}

.token-highlight {
  padding: 0 2px;
  border-radius: 3px;
  background: rgba(240, 160, 32, 0.28);
  color: var(--color-text);
  font-weight: 700;
  border-bottom: 2px solid rgba(240, 140, 0, 0.75);
}

.token-rows {
  padding-left: 22px;
}

.occ-actions {
  display: flex;
  justify-content: flex-end;
  padding: 8px 0 0 22px;
}

.btn-dismiss {
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 6px;
  border: 1px dashed var(--color-border);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;

  &:hover:not(:disabled) {
    color: var(--color-text);
    border-color: var(--color-text-secondary);
  }

  &:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
}

.token-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;

  &:last-child {
    margin-bottom: 0;
  }
}

.token-badge {
  display: inline-block;
  padding: 2px 8px;
  font-size: 12px;
  font-weight: 700;
  border-radius: 6px;
  background: rgba(240, 160, 32, 0.15);
  border: 1px solid rgba(240, 140, 0, 0.45);
  color: var(--color-text);
}

:deep(.n-pagination) {
  justify-content: center;
  margin: 8px 0 4px;
}

.name-select {
  flex: 1;
  min-width: 140px;
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  font-size: 13px;

  &:disabled {
    opacity: 0.5;
  }
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.btn-primary, .btn-secondary {
  padding: 8px 16px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  cursor: pointer;
  font-size: 13px;
}

.btn-primary {
  background: var(--color-primary, #18a058);
  color: #fff;
  border-color: transparent;

  &:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
}

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text);
}

.apply-error, .state.error {
  color: #d03050;
  font-size: 13px;
  margin-top: 8px;
}

.state {
  text-align: center;
  padding: 24px;
  color: var(--color-text-secondary);
}

.muted { color: var(--color-text-secondary); }
.small { font-size: 12px; }
</style>
