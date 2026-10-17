<template>
  <section class="ending-editor" aria-label="战役结局">
    <h6>战役结局</h6>
    <p class="muted">将最初发现、各地行动、最终结果和未解决事项整理成公开回顾。玩家会在战役议事厅看到这份结局。</p>
    <template v-if="readonly">
      <strong>{{ dispatch.public_title }}</strong>
      <p>{{ dispatch.public_summary }}</p>
    </template>
    <template v-else>
      <p v-if="realmMismatch" role="alert" class="error">浏览区服已变化，请重新读取此区战役。</p>
      <label>结局标题<input v-model="title" maxlength="120" :disabled="disabled"></label>
      <label>完整结局<textarea v-model="summary" rows="12" maxlength="4000" :disabled="disabled" placeholder="交代事件如何被发现，各支小队做了什么，最后如何结束；保留真实损失、逃脱与未追回事项。"></textarea></label>
      <details>
        <summary>参考已发布战报（{{ reports.length }} 份）</summary>
        <article v-for="report in orderedReports" :key="report.expedition_log_id">
          <strong>#{{ report.expedition_log_id }} · {{ report.report.public_title }}</strong>
          <p>{{ report.report.public_summary }}</p>
        </article>
        <p v-if="reportsTruncated" class="muted">这里只列出最近的战报。更早的证据请核验原始记录后填写准确 ID。</p>
      </details>
      <label>引用的战报探险 ID<input v-model="evidenceText" :disabled="disabled" placeholder="用逗号分隔，例如 1558, 1785, 1904"></label>
      <label>核验理由<textarea v-model="reason" rows="2" maxlength="1200" :disabled="disabled" placeholder="说明回顾与已发布战报的对应关系，仅管理员可见。"></textarea></label>
      <button type="button" :disabled="disabled || !canPreview" @click="makePreview">预览公开结局</button>
      <section v-if="preview" class="preview" aria-label="待发布结局">
        <strong>{{ preview.after.public_title }}</strong>
        <p>{{ preview.after.public_summary }}</p>
        <p class="muted">发布依据：{{ preview.after.evidence.map(item => '#' + item.expedition_log_id).join('、') }}</p>
        <p class="muted">此操作更新公开回顾，战役结束时间、世界事实、任务进度与结算记录保持原状。</p>
        <button type="button" :disabled="disabled" @click="publish">确认发布此结局</button>
      </section>
      <p v-if="errorText" role="alert" class="error">{{ errorText }}</p>
      <p v-if="receiptId" role="status">结局已发布，回执 #{{ receiptId }}。</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import api, { getS1AdminRealmTarget, type S1AdminRealmTarget } from '@/composables/useApi'

interface PublicReport {
  expedition_log_id: number; revision: number
  report: { public_title: string; public_summary: string }
}
interface Preview {
  preview_token: string
  after: { public_title: string; public_summary: string; evidence: { expedition_log_id: number }[] }
}
const props = defineProps<{
  rootId: number; realmCode: S1AdminRealmTarget; readonly: boolean
  dispatch: { public_title: string; public_summary: string }
  reports: PublicReport[]; reportsTruncated: boolean
}>()
const emit = defineEmits<{ changed: [receiptId: number] }>()
const title = ref(props.dispatch.public_title)
const summary = ref(props.dispatch.public_summary)
const evidenceText = ref(props.reports.map(report => report.expedition_log_id).join(', '))
const reason = ref('')
const busy = ref(false)
const errorText = ref('')
const receiptId = ref<number | null>(null)
const preview = ref<Preview | null>(null)
const realmMismatch = ref(false)
const disabled = computed(() => props.readonly || busy.value || receiptId.value !== null)
const orderedReports = computed(() => [...props.reports].sort((a, b) => a.revision - b.revision))
const evidenceIds = computed(() => evidenceText.value.trim().split(/[,，\s]+/).map(value => /^\d+$/.test(value) ? Number(value) : NaN))
const canPreview = computed(() => title.value.trim() && summary.value.trim() && reason.value.trim()
  && evidenceIds.value.length > 0 && evidenceIds.value.length <= 32
  && evidenceIds.value.every(value => Number.isSafeInteger(value) && value > 0)
  && new Set(evidenceIds.value).size === evidenceIds.value.length)
const endpoint = (action: string) => '/adventurer-s1/admin/world-outline/campaigns/' + props.rootId + '/ending/' + action
let generation = 0
function message(error: any) { return error?.response?.data?.message || error.message || '操作失败，请重试' }
function validateRealm() {
  realmMismatch.value = props.realmCode !== getS1AdminRealmTarget()
  if (realmMismatch.value) preview.value = null
  return !realmMismatch.value
}
async function makePreview() {
  if (!validateRealm() || disabled.value || !canPreview.value) return
  busy.value = true; errorText.value = ''; preview.value = null
  const stamp = generation
  try {
    const { data } = await api.post(endpoint('preview'), {
      public_title: title.value, public_summary: summary.value,
      evidence_log_ids: evidenceIds.value, reason: reason.value,
    }, { s1RealmContext: 'selected', s1AdminRealmTarget: props.realmCode })
    if (stamp !== generation) return
    if (data.code !== 200) throw new Error(data.message)
    preview.value = data.data
  } catch (error) {
    if (stamp === generation) errorText.value = message(error)
  } finally { if (stamp === generation) busy.value = false }
}
async function publish() {
  if (!validateRealm() || disabled.value || !preview.value) return
  busy.value = true; errorText.value = ''
  const stamp = generation
  try {
    const { data } = await api.post(endpoint('execute'), { preview_token: preview.value.preview_token }, { s1RealmContext: 'selected', s1AdminRealmTarget: props.realmCode })
    if (stamp !== generation) return
    if (data.code !== 200) throw new Error(data.message)
    receiptId.value = data.data.receipt_id
    preview.value = null
    emit('changed', data.data.receipt_id)
  } catch (error) {
    // Keep the same token after uncertain responses; the server replays its receipt.
    if (stamp === generation) errorText.value = message(error)
  } finally { if (stamp === generation) busy.value = false }
}
watch([title, summary, evidenceText, reason], () => { preview.value = null })
watch([() => props.rootId, () => props.realmCode, () => props.readonly], () => {
  generation++; preview.value = null; busy.value = false
})
onUnmounted(() => { generation++ })
</script>

<style scoped>
.ending-editor, .preview { display: grid; gap: 12px; min-width: 0; }
.ending-editor { border: 1px solid var(--color-border); padding: 16px; border-radius: 8px; }
h6 { margin: 0; font-size: 16px; } p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.8; }
label { display: grid; gap: 6px; min-width: 0; }
input, textarea, button { box-sizing: border-box; max-width: 100%; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 6px; padding: 9px; }
button { cursor: pointer; min-height: 40px; justify-self: start; } button:disabled { opacity: .5; cursor: default; }
input:focus-visible, textarea:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
article, .preview { margin-top: 12px; border-left: 2px solid var(--color-primary); padding-left: 12px; }
.muted { color: var(--color-text-secondary); font-size: 12px; } .error { color: var(--color-danger); }
</style>
