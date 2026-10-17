<template>
  <fieldset class="revelation-editor" :disabled="disabled">
    <legend>调整未揭示的线索</legend>
    <p>只列出当前阶段尚无公开方向、无经历证据的地图，以及从未开启的阶段。修改不改变任务条件、进度或已经发布的战报。</p>
    <label>待调整线索
      <select v-model="target">
        <option value="">请选择地图任务</option>
        <option v-for="item in eligible" :key="item.id" :value="item.id">{{ item.stage.title }} · {{ item.node.dungeon }} · {{ item.node.title }}</option>
      </select>
    </label>
    <template v-if="selected && draft">
      <label>首次偶遇可发现的本地事实
        <textarea v-model="draft.discovery" maxlength="1200" rows="3" />
      </label>
      <label>正式调查可揭示的本地事实
        <textarea v-model="draft.investigation" maxlength="1200" rows="5" />
      </label>
      <fieldset class="clue-maps">
        <legend>有明确证据时允许指向的地图</legend>
        <label v-for="node in selected.stage.nodes" :key="node.key">
          <input v-model="draft.clue_dungeons" type="checkbox" :value="node.dungeon">{{ node.dungeon }}
        </label>
      </fieldset>
      <p>勾选仅限定可建立关联的地图，不会公开路线。模糊痕迹保留为疑点；剧情取得明确依据后才开放具体地图。</p>
      <button type="button" :disabled="!canAdd" @click="add">{{ alreadyQueued ? '更新本次修改' : '加入本次修改' }}</button>
    </template>
    <p v-if="!eligible.length">当前没有可修改的未揭示线索。</p>
    <p>待预览修改：{{ modelValue.length }} 项。填写后点击“加入本次修改”，可连续加入多张地图再统一预览。</p>
    <ul v-if="modelValue.length">
      <li v-for="patch in modelValue" :key="patch.stage_key + '/' + patch.node_key">
        {{ labelFor(patch) }}
        <button type="button" @click="remove(patch)">移出本次修改</button>
      </li>
    </ul>
  </fieldset>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CampaignRevelationPatch, CampaignStageState } from './campaignStageTypes'

const props = defineProps<{
  modelValue: CampaignRevelationPatch[]
  state: CampaignStageState
  enteredStageKeys?: string[]
  disabled?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: CampaignRevelationPatch[]] }>()
const target = ref('')
const draft = ref<CampaignRevelationPatch['revelation'] | null>(null)
const eligible = computed(() => {
  if (props.state.status !== 'active' || !props.state.plan.revelation_enabled) return []
  return props.state.plan.stages.flatMap(stage => stage.nodes.filter(node => {
    if (!node.revelation) return false
    if (stage.key !== props.state.stage_key) return !!props.enteredStageKeys && !props.enteredStageKeys.includes(stage.key)
    const entry = props.state.progress[node.key]
    return entry?.knowledge === 'hidden' && entry.result === 'pending'
      && entry.evidence_log_id === null && entry.evidence_revision === null && !entry.evidence_text
  }).map(node => ({ id: stage.key + '/' + node.key, stage, node })))
})
const selected = computed(() => eligible.value.find(item => item.id === target.value))
const alreadyQueued = computed(() => props.modelValue.some(p => p.stage_key + '/' + p.node_key === target.value))
const canAdd = computed(() => !!draft.value?.investigation.trim() && (
  selected.value?.stage.key !== props.state.plan.stages[0]?.key || !!draft.value?.discovery.trim()))
function copy(value: CampaignRevelationPatch['revelation']) {
  return { discovery: value.discovery, investigation: value.investigation, clue_dungeons: [...value.clue_dungeons] }
}
function labelFor(patch: CampaignRevelationPatch) {
  const stage = props.state.plan.stages.find(s => s.key === patch.stage_key)
  const node = stage?.nodes.find(n => n.key === patch.node_key)
  return stage && node ? stage.title + ' · ' + node.dungeon : '任务已变化，请重新读取进度'
}
function add() {
  if (props.disabled || !selected.value || !draft.value || !canAdd.value) return
  const patch = { stage_key: selected.value.stage.key, node_key: selected.value.node.key, revelation: copy(draft.value) }
  emit('update:modelValue', [...props.modelValue.filter(p => p.stage_key + '/' + p.node_key !== target.value), patch])
}
function remove(patch: CampaignRevelationPatch) {
  if (!props.disabled) emit('update:modelValue', props.modelValue.filter(p => p.stage_key !== patch.stage_key || p.node_key !== patch.node_key))
}
watch([selected, () => props.modelValue], () => {
  const queued = props.modelValue.find(p => p.stage_key + '/' + p.node_key === target.value)
  const value = queued?.revelation || selected.value?.node.revelation
  draft.value = value ? copy(value) : null
}, { immediate: true })
</script>

<style scoped>
.revelation-editor, label { display: grid; gap: 8px; min-width: 0; }
fieldset { border: 1px solid var(--color-border); border-radius: 8px; padding: 12px; }
p { margin: 0; color: var(--color-text-secondary); line-height: 1.7; }
textarea, select, button { box-sizing: border-box; max-width: 100%; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 6px; padding: 9px; }
textarea { width: 100%; resize: vertical; } button { cursor: pointer; justify-self: start; }
button:disabled { cursor: default; opacity: .5; }
.clue-maps { display: flex; flex-wrap: wrap; gap: 10px; } .clue-maps label { display: flex; align-items: center; }
ul { margin: 0; padding-left: 22px; } li { margin-top: 8px; overflow-wrap: anywhere; }
</style>
