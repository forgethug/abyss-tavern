<template>
  <section class="continuation">
    <strong>承接已发生的战役结果</strong>
    <p>根据已发布战报核对尚未完成的目标。已经清剿的据点保留成果，仍有依据的后续行动进入新阶段。</p>
    <p v-if="job" role="status">{{ statusLabels[job.status] || job.status }}<span v-if="job.automatic"> · 自动衔接</span></p>
    <p v-if="job?.reason">当前准备依据：{{ job.reason }}</p>
    <p v-if="errorText || job?.error" role="alert">{{ errorText || job?.error }}</p>
    <template v-if="!readonly">
      <label>衔接说明<textarea v-model.trim="reason" maxlength="1200" rows="2" :disabled="working" /></label>
      <details><summary>提供待审核的具体草案（可选）</summary>
        <p>留空时根据战报准备。填写时使用衔接草案 JSON；草案仍须通过事实审核。</p>
        <textarea v-model="draft" rows="6" :disabled="working" aria-label="衔接草案 JSON" />
      </details>
      <button type="button" :disabled="working || !reason" @click="prepare">准备阶段衔接</button>
    </template>
    <button type="button" :disabled="busy" @click="refresh">读取衔接状态</button>
    <p v-if="dirtyDraft">说明或草案已经修改，需要重新准备后再预览。</p>
    <button v-if="job?.status === 'prepared' && !readonly" type="button" :disabled="busy || dirtyDraft" @click="makePreview">查看衔接预览</button>
    <section v-if="preview" class="preview">
      <strong>将开放：{{ targetTitle(preview.proposal.next_stage_key) }}</strong>
      <p>玩家将看到：{{ preview.proposal.public_summary }}</p>
      <p>调整依据：{{ preview.proposal.reason }}</p>
      <p>引用战报：{{ preview.proposal.evidence_log_ids.map(id => '#' + id).join('、') }}</p>
      <article v-for="stage in preview.proposal.stages" :key="stage.key">
        <strong>{{ stage.title }}</strong>
        <p v-for="node in stage.nodes" :key="node.key">{{ node.dungeon }} · {{ node.title }}：{{ node.objective }}</p>
      </article>
      <article v-for="front in preview.proposal.successors" :key="front.predecessor_id">
        <strong>后续事件：{{ front.title }}</strong><p>{{ front.summary }}</p><p>{{ front.campaign_agenda }}</p>
      </article>
      <p>审核依据：{{ preview.review.reason }}</p>
      <details><summary>完整前后状态与证据</summary><pre>{{ JSON.stringify(preview, null, 2) }}</pre></details>
      <button v-if="!readonly" type="button" :disabled="busy" @click="execute">确认开放此衔接</button>
      <button type="button" :disabled="busy" @click="preview = null">取消预览</button>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import api from '@/composables/useApi'
import type { CampaignStage, CampaignStagePlan } from './campaignStageTypes'
interface Proposal {
  next_stage_key: string; public_summary: string; reason: string; evidence_log_ids: number[]
  stages: CampaignStage[]
  successors: { predecessor_id: number; title: string; summary: string; campaign_agenda: string }[]
}
interface Job { status: string; error?: string; automatic: boolean; reason?: string }
interface Preview { preview_token: string; proposal: Proposal; review: { reason: string } }
const props = defineProps<{ rootId: number; readonly: boolean; plan: CampaignStagePlan }>()
const emit = defineEmits<{ changed: [] }>()
const job = ref<Job | null>(null)
const preview = ref<Preview | null>(null)
const reason = ref('承接已经发布的调查、清剿与人物去向，开放尚未完成的后续行动。')
const draft = ref('')
const dirtyDraft = ref(false)
const busy = ref(false)
const errorText = ref('')
const working = computed(() => busy.value || ['pending', 'processing'].includes(job.value?.status || ''))
const statusLabels: Record<string, string> = {
  pending: '等待准备', processing: '正在核验战报与后续路线', prepared: '审核通过，等待执行',
  rejected: '审核未通过', failed: '准备失败', stale: '依据已变化，需要重新准备', applied: '衔接已执行',
}
let generation = 0
let timer: ReturnType<typeof setTimeout> | undefined
const endpoint = (action = '') => '/adventurer-s1/admin/world-outline/campaigns/' + props.rootId + '/continuations' + (action ? '/' + action : '')
function targetTitle(key: string) {
  return key ? preview.value?.proposal.stages.find(s => s.key === key)?.title || props.plan.stages.find(s => s.key === key)?.title || key : '结束战役'
}
function fail(error: any) { errorText.value = error?.response?.data?.message || error.message || '操作失败' }
function schedule() {
  if (timer) clearTimeout(timer)
  if (['pending', 'processing'].includes(job.value?.status || '')) timer = setTimeout(refresh, 5000)
}
async function refresh() {
  if (busy.value) return
  const stamp = generation
  busy.value = true; errorText.value = ''
  try {
    const { data } = await api.get(endpoint())
    if (stamp !== generation) return
    if (data.code !== 200) throw new Error(data.message)
    job.value = data.data
    if (job.value?.status === 'applied') emit('changed')
  } catch (error) { if (stamp === generation) fail(error) }
  finally { if (stamp === generation) { busy.value = false; schedule() } }
}
async function action(name: string, body: unknown) {
  const stamp = generation
  busy.value = true; errorText.value = ''
  try {
    const { data } = await api.post(endpoint(name), body)
    if (stamp !== generation) return null
    if (data.code !== 200) throw new Error(data.message)
    return data.data
  } catch (error) { if (stamp === generation) fail(error); return null }
  finally { if (stamp === generation) busy.value = false }
}
async function prepare() {
  if (working.value || props.readonly) return
  preview.value = null
  let proposal: unknown
  try { proposal = draft.value.trim() ? JSON.parse(draft.value) : undefined }
  catch { errorText.value = '草案不是有效的 JSON'; return }
  const result = await action('prepare', { reason: reason.value, proposal, retry: ['failed', 'rejected', 'stale'].includes(job.value?.status || '') })
  if (result) { job.value = result; dirtyDraft.value = false; schedule() }
}
async function makePreview() {
  if (busy.value || props.readonly || dirtyDraft.value) return
  preview.value = await action('preview', {})
}
async function execute() {
  if (!preview.value || busy.value || props.readonly) return
  const result = await action('execute', { preview_token: preview.value.preview_token })
  if (result) { preview.value = null; emit('changed'); await refresh() }
}
watch([reason, draft], () => { preview.value = null; dirtyDraft.value = true })
onMounted(refresh)
onUnmounted(() => { generation++; if (timer) clearTimeout(timer) })
</script>

<style scoped>
.continuation, .preview, article, label { display: grid; gap: 10px; min-width: 0; }
.continuation, .preview { border: 1px solid var(--color-border); border-radius: 8px; padding: 12px; }
p, pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
textarea, button { box-sizing: border-box; max-width: 100%; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 6px; padding: 9px; }
textarea { width: 100%; } button { justify-self: start; min-height: 40px; cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
pre { font-size: 12px; } [role="alert"] { color: var(--color-danger); }
</style>
