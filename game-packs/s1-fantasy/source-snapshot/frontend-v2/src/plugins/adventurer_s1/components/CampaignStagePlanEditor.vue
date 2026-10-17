<template>
  <fieldset class="stage-editor">
    <legend>阶段路线</legend>
    <label>阶段推进方式
      <select v-model="modelValue.advance_mode" required>
        <option value="auto">自动推进：条件满足后自动开放下一阶段</option>
        <option value="manual">人工测试：每阶段完成后等待管理员确认</option>
      </select>
    </label>
    <label class="clue-option"><input v-model="modelValue.continuity_enabled" type="checkbox">阶段交接时按已发布战报核验后续路线</label>
    <p v-if="modelValue.continuity_enabled">根据本区战报、地点现状与相关人物核验后续路线，承接提前完成、实际逃亡与收尾结果。自动模式审核通过后开放；人工模式保留预览确认。核验发生在阶段交接时。</p>
    <p>默认等待全部任务，也可选择关键目标决定阶段进退。可选行动的成果和损失仍保留，不要求清完全部地图。任务标题与目标会在开放后向玩家展示。</p>
    <label v-if="modelValue.continuity_enabled">后续编排要求<textarea v-model.trim="modelValue.continuation_directive" maxlength="1200" rows="3" placeholder="说明战役后续的行动重点与收束原则；不把计划写成已经发生的事实。"></textarea></label>
    <p v-if="modelValue.revelation_enabled">按进度揭示：总纲与前线设定保留在后台。本场只使用这里配置的本地事实、已公开战报与实际世界变化。公开名称只描述当前已知问题；未来阶段、幕后身份和终点用途应留到对应阶段。</p>
    <article v-for="(stage, index) in modelValue.stages" :key="stage.key" class="stage-card">
      <header><strong>阶段 {{ index + 1 }}</strong><button type="button" @click="removeStage(stage.key)">移除阶段</button></header>
      <label>{{ modelValue.revelation_enabled ? '后台阶段名称' : '公开阶段名称' }}<input v-model.trim="stage.title" required maxlength="120"></label>
      <label v-if="modelValue.revelation_enabled">公开阶段名称<input v-model.trim="stage.public_title" required maxlength="120" placeholder="只写本阶段已公开的行动方向，不预告地图总数或终局。"></label>
      <div class="stage-grid">
        <label>关键目标确认后前往
          <select v-model="stage.success_next">
            <option value="">结束本次战役</option>
            <option v-for="target in modelValue.stages" :key="target.key" :value="target.key">{{ target.title || target.key }}</option>
          </select>
        </label>
        <label>不可挽回的失败后前往
          <select v-model="stage.failure_next">
            <option :value="null">未配置，失利后仍可继续调查</option><option value="">结束本次战役</option>
            <option v-for="target in modelValue.stages" :key="target.key" :value="target.key">{{ target.title || target.key }}</option>
          </select>
        </label>
      </div>
      <label class="clue-option"><input type="checkbox" :checked="!!stage.completion_nodes" @change="toggleCompletion(stage, $event)">只以选定的关键目标决定阶段进退</label>
      <fieldset v-if="stage.completion_nodes" class="clue-maps"><legend>关键目标（至少一项，其余为可选行动）</legend>
        <label v-for="node in stage.nodes" :key="node.key" class="clue-option"><input v-model="stage.completion_nodes" type="checkbox" :value="node.key">{{ node.title || node.dungeon || '未命名任务' }}</label>
      </fieldset>
      <section v-for="node in stage.nodes" :key="node.key" class="node-card">
        <header><strong>地图任务</strong><button type="button" @click="stage.nodes = stage.nodes.filter(n => n.key !== node.key)">移除任务</button></header>
        <div class="stage-grid">
          <label>任务地图<select v-model="node.dungeon" required><option value="">请选择</option><option v-for="map in maps" :key="map" :value="map">{{ map }}</option></select></label>
          <label>公开任务标题<input v-model.trim="node.title" required maxlength="120"></label>
        </div>
        <label>公开行动目标<textarea v-model.trim="node.objective" required maxlength="1200" rows="2"></textarea></label>
        <CampaignChallengeEditor v-model="node.challenge" />
        <fieldset class="node-card">
          <legend>本地行动窗口</legend>
          <label class="clue-option"><input type="checkbox" :checked="!!node.action_window" @change="toggleWindow(node, $event)">配置随时间推进的世界过程</label>
          <template v-if="node.action_window">
            <div class="stage-grid">
              <label>计时起点<select v-model="node.action_window.anchor">
                <option value="campaign_discovered">本区首次发现战役</option>
                <option value="campaign_started">本区创建战役</option>
              </select></label>
              <label>准备所需时间（小时）<input v-model.number="node.action_window.after_hours" type="number" min="1" step="1" required></label>
            </div>
            <label>必须持续有效的前提<textarea v-model.trim="node.action_window.condition" maxlength="1200" rows="3" required placeholder="写明参与者、资源、地点及可能被玩家破坏的必要条件。"></textarea></label>
            <label>条件满足时能够推进的过程<textarea v-model.trim="node.action_window.development" maxlength="1200" rows="3" required placeholder="描述世界内行动及其能力边界，不预定任务成败。"></textarea></label>
            <p>正式行动出发时核对经过时间；首次发现仍按本地揭示内容处理。时间足够并不代表前提成立，实际后果由剧情和世界事实承接。窗口详情保留在后台。</p>
          </template>
        </fieldset>
        <template v-if="modelValue.revelation_enabled && node.revelation">
          <label v-if="index === 0">首次偶遇可发现的本地事实<textarea v-model.trim="node.revelation.discovery" required maxlength="1200" rows="3" placeholder="描述眼前异常、可观察证据和本地能力边界。此时尚未发布调查任务，不写整条路线或远处内幕。"></textarea></label>
          <label>正式调查可揭示的本地事实<textarea v-model.trim="node.revelation.investigation" required maxlength="1200" rows="3" placeholder="描述本地真相、主体能力与限制，以及调查可取得的线索。只开放本阶段内容；不得恢复已经发生的损失。"></textarea></label>
          <fieldset class="clue-maps"><legend>调查线索允许指向的地图</legend>
            <label v-for="map in [...new Set(stage.nodes.map(n => n.dungeon).filter(Boolean))]" :key="map" class="clue-option">
              <input v-model="node.revelation.clue_dungeons" type="checkbox" :value="map">{{ map }}
            </label>
          </fieldset>
          <p>可不选。这里只限定调查可提出的方向，地图仍须有正文证据才会开放；线索指向某地不代表那里已经调查完成。首次偶遇只开放本地任务。</p>
        </template>
        <label>地图确认条件<textarea v-model.trim="node.success_condition" required maxlength="1200" rows="2" placeholder="写明正文需要实际建立的结果，例如找到并带回盖有哨站印章的运单。"></textarea></label>
        <label v-if="stage.failure_next !== null || (stage.completion_nodes && !stage.completion_nodes.includes(node.key))">不可挽回的失败条件<textarea v-model.trim="node.failure_condition" maxlength="1200" rows="2" placeholder="可留空。普通撤退或线索不足不应写成不可挽回的失败。"></textarea></label>
      </section>
      <button type="button" @click="addNode(stage)">添加地图任务</button>
    </article>
    <button type="button" @click="addStage">添加阶段</button>
  </fieldset>
</template>

<script setup lang="ts">
import { watch } from 'vue'
import CampaignChallengeEditor from './CampaignChallengeEditor.vue'
import type { CampaignStage, CampaignStageNode, CampaignStagePlan } from './campaignStageTypes'
const props = defineProps<{ modelValue: CampaignStagePlan; maps: string[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: CampaignStagePlan] }>()
function key(prefix: string) { return prefix + '_' + crypto.randomUUID().replaceAll('-', '') }
function toggleCompletion(stage: CampaignStage, event: Event) {
  if ((event.target as HTMLInputElement).checked) stage.completion_nodes = stage.nodes.map(n => n.key)
  else delete stage.completion_nodes
}
function toggleWindow(node: CampaignStageNode, event: Event) {
  node.action_window = (event.target as HTMLInputElement).checked
    ? { anchor: 'campaign_discovered', after_hours: 1, condition: '', development: '' } : null
}
function addNode(stage: CampaignStage) {
  stage.nodes.push({ key: key('map'), dungeon: '', title: '', objective: '', success_condition: '', failure_condition: '',
    revelation: { discovery: '', investigation: '', clue_dungeons: [] } })
}
function addStage() {
  const stage: CampaignStage = { key: key('stage'), title: '', public_title: '', nodes: [], success_next: '', failure_next: null }
  addNode(stage)
  emit('update:modelValue', { ...props.modelValue, stages: [...props.modelValue.stages, stage] })
}
function removeStage(stageKey: string) {
  emit('update:modelValue', { ...props.modelValue, stages: props.modelValue.stages.filter(s => s.key !== stageKey).map(s => ({
    ...s, success_next: s.success_next === stageKey ? '' : s.success_next,
    failure_next: s.failure_next === stageKey ? null : s.failure_next,
  })) })
}
watch(() => props.modelValue.stages, stages => {
  for (const stage of stages) {
    if (stage.completion_nodes?.some(key => !stage.nodes.some(n => n.key === key))) {
      stage.completion_nodes = stage.completion_nodes.filter(key => stage.nodes.some(n => n.key === key))
    }
    const maps = new Set(stage.nodes.map(node => node.dungeon))
    for (const node of stage.nodes) {
      if (stage.failure_next === null && (!stage.completion_nodes || stage.completion_nodes.includes(node.key))) node.failure_condition = ''
      if (node.revelation?.clue_dungeons.some(map => !maps.has(map))) {
        node.revelation.clue_dungeons = node.revelation.clue_dungeons.filter(map => maps.has(map))
      }
    }
  }
}, { deep: true })
</script>

<style scoped>
.stage-editor, .stage-card, .node-card { display: grid; gap: 12px; min-width: 0; padding: 14px; border: 1px solid var(--color-border); border-radius: 8px; }
.stage-editor { margin: 12px 0; }.stage-card, .node-card { background: var(--color-surface); }
header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; }
label { display: grid; gap: 6px; min-width: 0; } p { line-height: 1.7; color: var(--color-text-secondary); }
.stage-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); gap: 12px; }
input, textarea, select, button { box-sizing: border-box; max-width: 100%; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 6px; padding: 9px; }
input, textarea, select { width: 100%; } button { min-height: 40px; cursor: pointer; justify-self: start; }
.clue-maps { display: flex; flex-wrap: wrap; gap: 8px 16px; min-width: 0; border: 1px solid var(--color-border); }
.clue-option { display: flex; align-items: center; min-height: 40px; }.clue-option input { width: auto; }
</style>
