<template>
  <fieldset class="challenge-editor" :disabled="disabled">
    <legend>本任务挑战</legend>
    <label class="toggle"><input type="checkbox" :checked="!!modelValue" @change="toggle">配置独立挑战强度</label>
    <template v-if="modelValue">
      <div class="grid">
        <label>建议冒险者等级<input v-model.number="modelValue.recommended_level" type="number" min="1" required></label>
        <label>公开风险提示<textarea v-model.trim="modelValue.public_brief" maxlength="1200" rows="2" required placeholder="玩家接取时能知道的危险和准备要求。"></textarea></label>
      </div>
      <label>战场与防守机制<textarea v-model.trim="modelValue.battlefield" maxlength="1200" rows="3" required placeholder="写明真实存在的防守、作用条件、消耗与突破方式。"></textarea></label>
      <label>前期成果怎样影响本场<textarea v-model.trim="modelValue.preparation_effects" maxlength="1200" rows="3" placeholder="可留空。只有已发布战报或当前事实证实的准备才会生效；不要预设前期必然成功。"></textarea></label>
      <article v-for="(opponent, index) in modelValue.opponents" :key="index" class="opponent">
        <header><strong>敌方能力档案 {{ index + 1 }}</strong><button type="button" @click="modelValue.opponents.splice(index, 1)">移除</button></header>
        <div class="grid">
          <label>主体名称<input v-model.trim="opponent.subject" required maxlength="1200"></label>
          <label>主体范围<select v-model="opponent.scope"><option value="individual">单一主体</option><option value="group">同类群体</option></select></label>
          <label>能力基准等级<input v-model.number="opponent.basis_level" type="number" min="1" required></label>
          <label>数量压力<select v-model="opponent.number_pressure"><option v-for="(label, value) in numbers" :key="value" :value="value">{{ label }}</option></select></label>
        </div>
        <label>能力与作用条件（每行一项）<textarea :value="opponent.capabilities.join('\n')" rows="3" required @change="opponent.capabilities = lines($event)"></textarea></label>
        <label>限制与可被利用的弱点（每行一项）<textarea :value="opponent.limitations.join('\n')" rows="3" required @change="opponent.limitations = lines($event)"></textarea></label>
        <details><summary>能力维度与当前状态</summary>
          <label>当前状态<select v-model="opponent.condition"><option value="normal">常态</option><option value="impaired">受损</option><option value="enhanced">增强</option></select></label>
          <div class="grid">
            <fieldset v-for="(label, profileKey) in profiles" :key="profileKey"><legend>{{ label }}</legend>
              <label v-for="(axisLabel, axis) in axes" :key="axis">{{ axisLabel }}<select v-model="opponent[profileKey][axis]"><option v-for="(rankLabel, rank) in ranks" :key="rank" :value="rank">{{ rankLabel }}</option></select></label>
            </fieldset>
          </div>
        </details>
      </article>
      <button type="button" @click="addOpponent">添加敌方档案</button>
      <p>能力以设定和已经发生的世界变化为准。建议等级不限制报名，也不规定胜率或伤亡；玩家只看到建议等级与公开风险提示。</p>
    </template>
  </fieldset>
</template>

<script setup lang="ts">
import type { CampaignChallenge, CampaignOpponent } from './campaignStageTypes'
const props = defineProps<{ modelValue?: CampaignChallenge | null; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: CampaignChallenge | null] }>()
const axes = { force: '力量', mobility: '机动', cognition: '认知', durability: '承受', perception: '感知' }
const ranks = { very_low: '很低', low: '较低', baseline: '基准', high: '较高', very_high: '很高', exceptional: '超常' }
const numbers = { none: '无', low: '低', medium: '中', high: '高', overwhelming: '压倒性数量' }
const profiles = { typical_profile: '常态能力', current_profile: '当前能力' }
function opponent(level: number): CampaignOpponent {
  const baseline: CampaignOpponent['typical_profile'] = { force: 'baseline', mobility: 'baseline', cognition: 'baseline', durability: 'baseline', perception: 'baseline' }
  return { subject: '', scope: 'individual', basis_level: level, typical_profile: { ...baseline }, current_profile: { ...baseline },
    condition: 'normal', number_pressure: 'none', capabilities: [], limitations: [] }
}
function toggle(event: Event) {
  emit('update:modelValue', (event.target as HTMLInputElement).checked
    ? { recommended_level: 1, public_brief: '', battlefield: '', preparation_effects: '', opponents: [opponent(1)] } : null)
}
function addOpponent() { props.modelValue?.opponents.push(opponent(props.modelValue.recommended_level)) }
function lines(event: Event) { return (event.target as HTMLTextAreaElement).value.split('\n').map(s => s.trim()).filter(Boolean) }
</script>

<style scoped>
.challenge-editor, .opponent { display: grid; gap: 12px; min-width: 0; padding: 12px; border: 1px solid var(--color-border); border-radius: 8px; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); }
label { display: grid; gap: 6px; min-width: 0; } fieldset { min-width: 0; } header, .toggle { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; } header { justify-content: space-between; }
input, textarea, select, button { box-sizing: border-box; max-width: 100%; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 6px; padding: 9px; }
input, textarea, select { width: 100%; } .toggle input { width: auto; } button { min-height: 40px; cursor: pointer; justify-self: start; } summary { cursor: pointer; min-height: 40px; } p { margin: 0; line-height: 1.7; color: var(--color-text-secondary); }
</style>
