<template>
  <section class="stage-admin">
    <button type="button" :disabled="busy" @click="load">读取阶段进度与证据</button>
    <p v-if="errorText" role="alert" class="error">{{ errorText }}</p>
    <template v-if="profile">
      <p class="muted">数据来源：{{ profile.source.realm_code }} · {{ profile.source.deployment_id }}</p>
      <p v-if="!state">此战役未配置阶段路线，继续使用原有世界线演进。</p>
      <template v-else-if="stage">
        <h6>{{ stage.title }} · {{ statusLabel(state.status) }}</h6>
        <CampaignEndingEditor v-if="state.status === 'ended' && dispatch?.ended_at" :key="profile.source.realm_code + ':' + rootId + ':' + dispatch.revision"
          :root-id="rootId" :realm-code="profile.source.realm_code" :readonly="readonly" :dispatch="dispatch"
          :reports="profile.sections.reports?.rows || []" :reports-truncated="profile.sections.reports?.truncated || false" @changed="endingPublished" />
        <p v-if="state.status !== 'ended'">关键目标 {{ requiredConfirmed }}/{{ requiredKeys.length }}；全部行动已确认 {{ confirmedCount }}/{{ stage.nodes.length }}。{{ advanceMode === 'auto' ? '满足关键条件后自动推进。' : '当前为人工测试，转换前等待确认。' }}</p>
        <p v-if="state.status !== 'ended' && advanceMode === 'auto'" class="muted">修正地图结论后，满足条件的阶段也会自动推进。占用、证据或前线不符合条件时会暂缓，可通过下方核验查看原因。</p>
        <p v-if="dispatch?.active_log_id">小队正在执行或战报待发布：探险 #{{ dispatch.active_log_id }}，当前不能推进。</p>
        <p v-if="recoverableFronts.length" class="error">当前待完成地图中有 {{ recoverableFronts.length }} 条沉寂前线，请核对世界事实。若仅由自动计数造成，可使用下方「恢复误沉寂前线」。</p>
        <CampaignContinuationPanel v-if="state.status === 'ready'" :key="rootId" :root-id="rootId" :readonly="readonly" :plan="state.plan" @changed="load" />
        <article v-for="node in stage.nodes" :key="node.key" class="node">
          <strong>{{ node.dungeon }} · {{ node.title }} · {{ requiredKeys.includes(node.key) ? '关键目标' : '可选行动' }} · {{ resultLabels[state.progress[node.key].result] }}</strong>
          <p>{{ node.objective }}</p>
          <p>确认条件：{{ node.success_condition }}</p>
          <details v-if="node.challenge"><summary>当前挑战配置 · 建议 Lv.{{ node.challenge.recommended_level }}</summary><pre>{{ JSON.stringify(node.challenge, null, 2) }}</pre></details>
          <details v-if="node.action_window"><summary>后台行动窗口 · {{ node.action_window.after_hours }} 小时</summary><pre>{{ JSON.stringify(node.action_window, null, 2) }}</pre></details>
          <p v-if="node.failure_condition">不可挽回的失败条件：{{ node.failure_condition }}</p>
          <p>发现状态：{{ knowledgeLabels[state.progress[node.key].knowledge] }}</p>
          <p v-if="state.progress[node.key].evidence_log_id">证据战报 #{{ state.progress[node.key].evidence_log_id }}：{{ state.progress[node.key].evidence_text }}</p>
        </article>
        <div v-if="!readonly && state.status !== 'ended'" class="operation-form">
          <label>操作<select v-model="kind" :disabled="busy"><option v-if="!state.plan.continuity_enabled" value="transition">核验并开放下一阶段</option><option value="progress_review">依据已发布战报修正地图结论</option><option value="advance_mode">切换自动或人工推进</option><option value="objectives_update">调整关键目标与后续编排</option><option value="challenge_update">调整尚未开启阶段的挑战</option><option v-if="state.status === 'active' && state.plan.revelation_enabled" value="revelation_update">调整未揭示的线索</option><option v-if="state.status === 'active'" value="front_recovery">恢复误沉寂前线</option></select></label>
          <label v-if="kind === 'transition'">推进分支
            <select v-model="branch" :disabled="busy">
              <option value="success">成功 → {{ targetTitle(stage.success_next) }}</option>
              <option v-if="stage.failure_next !== null" value="failure">失败 → {{ targetTitle(stage.failure_next) }}</option>
            </select>
          </label>
          <template v-if="kind === 'objectives_update'">
            <fieldset><legend>当前阶段关键目标（至少一项）</legend>
              <label v-for="node in stage.nodes" :key="node.key" class="attestation"><input v-model="completionDraft" type="checkbox" :value="node.key" :disabled="busy">{{ node.dungeon }} · {{ node.title }}</label>
            </fieldset>
            <label>后续编排要求<textarea v-model.trim="continuationDirective" maxlength="1200" rows="5" :disabled="busy"></textarea></label>
            <p>只调整推进门槛和后续编排，不更改任何任务结论、证据或世界事实。未选任务成为可选行动；满足关键条件后可自动衔接。</p>
          </template>
          <label v-else-if="kind === 'advance_mode'">新的推进方式
            <select v-model="targetMode" :disabled="busy"><option value="auto">自动推进</option><option value="manual">人工测试</option></select>
          </label>
          <template v-else-if="kind === 'challenge_update'">
            <label>待调整任务<select v-model="challengeTarget" :disabled="busy"><option value="">请选择</option><optgroup v-for="future in futureStages" :key="future.key" :label="future.title"><option v-for="node in future.nodes" :key="node.key" :value="future.key + '/' + node.key">{{ node.dungeon }} · {{ node.title }}</option></optgroup></select></label>
            <CampaignChallengeEditor v-if="challengeTarget" v-model="challengeDraft" :disabled="busy" />
            <p class="muted">只调整尚未开启过的阶段。现有任务条件、地图、阶段出口、调查结论与世界事实保持原状；回执保留修改前后的完整挑战配置，可在阶段开启前恢复。</p>
          </template>
          <CampaignRevelationEditor v-else-if="kind === 'revelation_update'" v-model="revelationChanges"
            :state="state" :entered-stage-keys="profile.entered_stage_keys" :disabled="busy" />
          <template v-else-if="kind === 'front_recovery'">
            <label>待恢复前线<select v-model="recoveryThreadId" :disabled="busy"><option value="">请选择</option><option v-for="front in recoverableFronts" :key="front.id" :value="front.id">{{ front.base_dungeon }} · {{ front.title }} · #{{ front.id }}</option></select></label>
            <p v-if="!recoverableFronts.length" class="muted">当前没有符合恢复范围的沉寂前线。</p>
            <p class="muted">先核对世界摘要、最近战报和维护记录，再填写误沉寂原因。此操作只恢复当前未完成任务的触发资格；真实伤亡、毁损、调查结论和战役阶段继续有效。</p>
            <label class="attestation"><input v-model="confirmClockOnly" type="checkbox" :disabled="busy || !recoveryThreadId">我已核验原始世界事实，确认此前线仅被自动计数沉寂，没有真实终结。</label>
          </template>
          <template v-else-if="kind === 'progress_review'">
            <label>待核验任务<select v-model="nodeKey" :disabled="busy"><option value="">请选择</option><option v-for="node in stage.nodes" :key="node.key" :value="node.key">{{ node.dungeon }} · {{ node.title }}</option></select></label>
            <label>已发布的证据探险 ID<input v-model.number="evidenceLogId" type="number" min="1" :disabled="busy"></label>
            <label>核验结论<select v-model="nodeResult" :disabled="busy"><option value="pending">尚未完成，可继续</option><option value="confirmed">地图目标已确认</option><option value="failed">已发生不可挽回的失败</option></select></label>
          </template>
          <label>核验理由<textarea v-model.trim="reason" maxlength="1200" rows="3" :disabled="busy" placeholder="说明正文证据与任务条件如何对应。"></textarea></label>
          <p v-if="kind === 'transition' && !profile.eligible_branches.includes(branch)">所选分支的地图条件尚未满足。</p>
          <button type="button" :disabled="busy || !reason || !!dispatch?.active_log_id || (kind === 'transition' && !profile.eligible_branches.includes(branch)) || (kind === 'objectives_update' && !completionDraft.length) || (kind === 'challenge_update' && !challengeTarget) || (kind === 'revelation_update' && !revelationChanges.length) || (kind === 'front_recovery' && (!recoveryThreadId || !confirmClockOnly))" @click="makePreview">生成操作预览</button>
        </div>
        <section v-if="preview" class="preview">
          <strong>待执行：{{ operationLabels[kind] }}</strong>
          <p>{{ stage.title }} → {{ targetTitle(preview.after.stage_key) }} · {{ statusLabel(preview.after.status, preview.after.advance_mode) }}</p>
          <p v-if="kind === 'advance_mode'">推进方式：{{ preview.after.advance_mode === 'auto' ? '自动推进，满足条件后自动开放后续阶段' : '人工测试，阶段转换前等待确认' }}</p>
          <p>{{ kind === 'transition' ? '将开放地图' : '当前目标地图' }}：{{ preview.after.target_dungeons.join('、') || '无' }}</p>
          <p>核验理由：{{ preview.reason }}</p>
          <details v-if="preview.after.evidence" open><summary>本次核验使用的完整正文与原战报</summary><pre>{{ JSON.stringify(preview.after.evidence, null, 2) }}</pre></details>
          <details v-if="preview.after.challenge_changes" open><summary>挑战配置修改前后</summary><pre>{{ JSON.stringify(preview.after.challenge_changes, null, 2) }}</pre></details>
          <details v-if="preview.after.revelation_changes" open><summary>未揭示线索修改前后</summary><pre>{{ JSON.stringify(preview.after.revelation_changes, null, 2) }}</pre></details>
          <details v-if="preview.after.front_recovery" open><summary>待恢复前线与本次核验的世界状态</summary><pre>{{ JSON.stringify(preview.after.front_recovery, null, 2) }}</pre></details>
          <details v-if="preview.after.objectives_changes" open><summary>关键目标与后续编排修改前后</summary><pre>{{ JSON.stringify(preview.after.objectives_changes, null, 2) }}</pre></details>
          <button v-if="!readonly" type="button" :disabled="busy" @click="execute">确认执行此预览</button>
          <button type="button" :disabled="busy" @click="preview = null">取消预览</button>
        </section>
        <p v-if="receiptId" role="status">操作已落库，回执 #{{ receiptId }}；以下进度已重新读取。</p>
        <details><summary>当前证据与最近操作回执</summary><pre>{{ JSON.stringify({ evidence: profile.sections.current_evidence, operations: profile.sections.operations }, null, 2) }}</pre></details>
        <p v-if="profile.truncated_sections.length" class="muted">历史记录已分页截取：{{ profile.truncated_sections.join('、') }}。阶段条件使用完整进度判定。</p>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import api, { type S1AdminRealmTarget } from '@/composables/useApi'
import CampaignChallengeEditor from './CampaignChallengeEditor.vue'
import CampaignContinuationPanel from './CampaignContinuationPanel.vue'
import CampaignEndingEditor from './CampaignEndingEditor.vue'
import CampaignRevelationEditor from './CampaignRevelationEditor.vue'
import type { CampaignAdvanceMode, CampaignChallenge, CampaignNodeResult, CampaignStageState, CampaignRevelationPatch } from './campaignStageTypes'
interface Section<T> { rows: T[]; truncated: boolean }
interface Dispatch { revision: number; active_log_id: number | null; public_title: string; public_summary: string; ended_at: string | null }
interface PublicReport { expedition_log_id: number; revision: number; report: { public_title: string; public_summary: string } }
interface Front { id: number; title: string; base_dungeon: string; campaign_role: string; status: string; is_chain_head: boolean | null }
interface Profile {
  source: { realm_code: S1AdminRealmTarget; deployment_id: string }
  eligible_branches: string[]; truncated_sections: string[]
  entered_stage_keys?: string[]
  sections: { stage?: Section<CampaignStageState>; dispatch: Section<Dispatch>; fronts?: Section<Front>; current_evidence?: Section<unknown>; operations?: Section<unknown>; reports?: Section<PublicReport> }
}
interface Preview {
  preview_token: string; reason: string
  after: { stage_key: string; status: CampaignStageState['status']; advance_mode?: CampaignAdvanceMode; target_dungeons: string[]; evidence?: unknown; challenge_changes?: unknown; revelation_changes?: unknown; front_recovery?: unknown; objectives_changes?: unknown }
}
const props = defineProps<{ rootId: number; readonly: boolean }>()
const profile = ref<Profile | null>(null)
const preview = ref<Preview | null>(null)
const errorText = ref('')
const busy = ref(false)
const kind = ref<'transition' | 'progress_review' | 'advance_mode' | 'challenge_update' | 'revelation_update' | 'front_recovery' | 'objectives_update'>('transition')
const completionDraft = ref<string[]>([])
const continuationDirective = ref('')
const revelationChanges = ref<CampaignRevelationPatch[]>([])
const recoveryThreadId = ref<number | ''>('')
const confirmClockOnly = ref(false)
const challengeTarget = ref('')
const challengeDraft = ref<CampaignChallenge | null>(null)
const targetMode = ref<CampaignAdvanceMode>('auto')
const branch = ref('success')
const reason = ref('')
const nodeKey = ref('')
const nodeResult = ref<CampaignNodeResult>('pending')
const evidenceLogId = ref<number | ''>('')
const receiptId = ref<number | null>(null)
const state = computed(() => profile.value?.sections.stage?.rows[0])
const advanceMode = computed(() => state.value?.plan.advance_mode || 'manual')
const dispatch = computed(() => profile.value?.sections.dispatch.rows[0])
const stage = computed(() => state.value?.plan.stages.find(s => s.key === state.value?.stage_key))
const confirmedCount = computed(() => Object.values(state.value?.progress || {}).filter(n => n.result === 'confirmed').length)
const requiredKeys = computed(() => stage.value?.completion_nodes || stage.value?.nodes.map(n => n.key) || [])
const requiredConfirmed = computed(() => requiredKeys.value.filter(key => state.value?.progress[key]?.result === 'confirmed').length)
const futureStages = computed(() => (state.value?.plan.stages || []).filter(s => profile.value?.entered_stage_keys && !profile.value.entered_stage_keys.includes(s.key)))
const recoverableFronts = computed(() => state.value?.status === 'active' ? (profile.value?.sections.fronts?.rows || []).filter(front =>
  front.campaign_role === 'dungeon_front' && front.is_chain_head !== false && front.status === 'dormant'
  && stage.value?.nodes.some(node => node.dungeon === front.base_dungeon && state.value?.progress[node.key]?.result === 'pending')) : [])
const resultLabels = { pending: '尚待完成', confirmed: '已确认', failed: '不可挽回的失败' }
const operationLabels = { transition: '阶段推进', progress_review: '地图结论核验', advance_mode: '推进方式调整', challenge_update: '未来阶段挑战调整', revelation_update: '未揭示线索调整', front_recovery: '恢复误沉寂前线', objectives_update: '关键目标与后续编排' }
const operationPaths = { transition: 'transitions', progress_review: 'progress-review', advance_mode: 'advance-mode', challenge_update: 'challenges', revelation_update: 'revelations', front_recovery: 'front-recovery', objectives_update: 'objectives' }
function statusLabel(status: CampaignStageState['status'], mode = advanceMode.value) {
  return { active: '进行中', ready: mode === 'auto' ? '等待自动推进' : '待管理员核验', ended: '已结束' }[status]
}
const knowledgeLabels = { hidden: '尚未发现', hinted: '已知调查方向', discovered: '已有本地经历' }
let generation = 0
function targetTitle(key: string | null) { return key ? state.value?.plan.stages.find(s => s.key === key)?.title || key : '结束战役' }
function endpoint(action: string) {
  return '/adventurer-s1/admin/world-outline/campaigns/' + props.rootId + '/' + action
}
function errorMessage(error: any) { return error?.response?.data?.message || error.message || '操作失败，请刷新后重试' }
async function readProgress() {
  const stamp = ++generation
  const { data } = await api.get(endpoint('progress'))
  if (stamp !== generation) return
  if (data.code !== 200) throw new Error(data.message)
  profile.value = data.data
  completionDraft.value = [...requiredKeys.value]
  continuationDirective.value = state.value?.plan.continuation_directive || ''
  confirmClockOnly.value = false
  revelationChanges.value = []
}
async function load() {
  if (busy.value) return
  busy.value = true; errorText.value = ''; preview.value = null
  try { await readProgress() } catch (error) { errorText.value = errorMessage(error) }
  finally { busy.value = false }
}
function endingPublished(id: number) {
  receiptId.value = id
  void load()
}
async function makePreview() {
  if (busy.value || props.readonly) return
  busy.value = true; errorText.value = ''; preview.value = null
  const stamp = generation
  try {
    const body = kind.value === 'transition' ? { branch: branch.value, reason: reason.value }
      : kind.value === 'objectives_update' ? { completion_nodes: completionDraft.value, continuation_directive: continuationDirective.value, reason: reason.value }
      : kind.value === 'advance_mode' ? { advance_mode: targetMode.value, reason: reason.value }
        : kind.value === 'challenge_update' ? { challenges: [{ stage_key: challengeTarget.value.split('/')[0], node_key: challengeTarget.value.split('/')[1], challenge: challengeDraft.value }], reason: reason.value }
        : kind.value === 'revelation_update' ? { revelations: revelationChanges.value, reason: reason.value }
        : kind.value === 'front_recovery' ? { thread_id: recoveryThreadId.value, confirm_clock_only: confirmClockOnly.value, reason: reason.value }
        : { node_key: nodeKey.value, result: nodeResult.value, evidence_log_id: evidenceLogId.value, reason: reason.value }
    const { data } = await api.post(endpoint(operationPaths[kind.value] + '/preview'), body)
    if (stamp !== generation) return
    if (data.code !== 200) throw new Error(data.message)
    preview.value = data.data
  } catch (error) { errorText.value = errorMessage(error) }
  finally { busy.value = false }
}
async function execute() {
  if (!preview.value || busy.value || props.readonly) return
  busy.value = true; errorText.value = ''
  try {
    const { data } = await api.post(endpoint(operationPaths[kind.value] + '/execute'), { preview_token: preview.value.preview_token })
    if (data.code !== 200) throw new Error(data.message)
    receiptId.value = data.data.receipt_id
    preview.value = null
    await readProgress()
  } catch (error) { preview.value = null; errorText.value = errorMessage(error) }
  finally { busy.value = false }
}
watch([kind, branch, reason, nodeKey, nodeResult, evidenceLogId, targetMode, challengeTarget, challengeDraft, revelationChanges, recoveryThreadId, confirmClockOnly, completionDraft, continuationDirective], () => { preview.value = null }, { deep: true })
watch([kind, recoveryThreadId], () => { confirmClockOnly.value = false })
watch(() => state.value?.plan.continuity_enabled, enabled => {
  if (enabled && kind.value === 'transition') kind.value = 'advance_mode'
})
watch([challengeTarget, state], () => {
  const [stageKey, taskKey] = challengeTarget.value.split('/')
  const value = state.value?.plan.stages.find(s => s.key === stageKey)?.nodes.find(n => n.key === taskKey)?.challenge
  challengeDraft.value = value ? JSON.parse(JSON.stringify(value)) : null
})
watch(() => props.rootId, () => { generation++; profile.value = null; preview.value = null; receiptId.value = null; challengeTarget.value = ''; revelationChanges.value = []; recoveryThreadId.value = ''; confirmClockOnly.value = false })
onUnmounted(() => { generation++ })
</script>

<style scoped>
.stage-admin, .node, .operation-form, .preview { display: grid; gap: 10px; min-width: 0; }
.stage-admin { padding: 12px 0; } .node, .preview { border: 1px solid var(--color-border); padding: 12px; border-radius: 8px; }
p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; } h6 { margin: 0; font-size: 15px; }
label { display: grid; gap: 6px; min-width: 0; }
.attestation { grid-template-columns: auto 1fr; align-items: start; }
input, select, textarea, button { box-sizing: border-box; max-width: 100%; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 6px; padding: 9px; }
button { cursor: pointer; min-height: 40px; justify-self: start; } button:disabled { opacity: .5; cursor: default; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; } .error { color: var(--color-danger); }.muted { color: var(--color-text-secondary); font-size: 12px; }
</style>
