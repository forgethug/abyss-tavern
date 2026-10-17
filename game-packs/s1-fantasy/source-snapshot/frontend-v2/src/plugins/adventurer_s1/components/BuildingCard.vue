<template>
  <div
    class="building-card"
    :class="[statusClass, { 'assessment-preview-card': isAssessmentPreview }]"
    @click="$emit('click')"
  >
    <div class="building-heading">
      <div class="building-icon">{{ building.template.icon }}</div>
      <div class="building-name">{{ displayName }}</div>
    </div>
    <div class="building-labels">
      <div class="building-category" v-if="!isAssessmentPreview && building.template.category !== 'general'">
        {{ categoryLabel(building.template.category) }}
      </div>
      <div class="building-status-badge" :class="'badge-' + building.status">
        <template v-if="building.status === 'locked'">{{ lockedStatusLabel }}</template>
        <template v-else-if="building.status === 'building'">🔨 建造中</template>
        <template v-else-if="building.status === 'upgrading'">⬆️ 升级中</template>
        <template v-else-if="building.status === 'built' && isAssessmentPreview">✦ 静观中</template>
        <template v-else-if="building.status === 'built'">✅ 已建成</template>
      </div>
    </div>
    <div class="building-desc">{{ displayDescription }}</div>

    <div class="building-operation">
      <template v-if="building.status === 'building' || building.status === 'upgrading'">
        <div class="building-progress">
          <div class="progress-bar">
            <div class="progress-fill" :style="{ width: liveProgress + '%' }"></div>
          </div>
          <span class="progress-text">{{ liveProgress }}%</span>
        </div>
        <div class="building-remaining">{{ displayRemaining }}</div>
        <div class="building-helpers">
          在位：{{ building.helpers?.length ?? 0 }}/{{ building.template.helper_slots }}
          <template v-if="building.helpers && building.helpers.length > 0">
            —
            <span v-for="(h, i) in building.helpers" :key="h.adventurer_id" class="helper-tag">
              {{ h.adventurer_name }}(💪{{ h.stamina ?? '?' }}){{ i < building.helpers.length - 1 ? '、' : '' }}
            </span>
          </template>
        </div>
      </template>

      <div v-if="building.status === 'built' && isAssessmentPreview" class="assessment-preview-state">
        <span v-if="assessmentMinLevel">终庭观测下限 · 冒险者 Lv.{{ assessmentMinLevel }}</span>
        <span>只观既成之业 · 不设试炼，不受自荐</span>
      </div>

      <div class="building-helpers" v-if="building.status === 'built' && !isAssessmentPreview">
        {{ usageSlotLabel }}：{{ building.helpers?.length ?? building.active_helpers_count ?? 0 }}/{{ effectiveUsageSlots }}
        <template v-if="building.helpers && building.helpers.length > 0">
          —
          <span v-for="(h, i) in building.helpers" :key="h.adventurer_id" class="helper-tag">
            {{ h.adventurer_name }}(💪{{ h.stamina ?? '?' }}){{ i < building.helpers.length - 1 ? '、' : '' }}
          </span>
        </template>
      </div>
    </div>

    <div class="building-meta">
      <template v-if="building.status === 'built' && usageConfig && !isAssessmentPreview">
        <span v-if="usageConfig.usage_stamina_cost">💪 {{ usageConfig.usage_stamina_cost }}精力</span>
        <span v-if="usageConfig.usage_duration_minutes">⏱️ {{ usageConfig.usage_duration_minutes }}分钟</span>
        <span v-if="usageConfig.daily_limit">📅 每日{{ usageConfig.daily_limit }}次</span>
      </template>
      <template v-else-if="building.status === 'building' || building.status === 'upgrading'">
        <span>⏱️ {{ building.template.build_hours }}h</span>
        <span>💪 {{ building.template.stamina_cost_per_help }}精力/h</span>
        <span>⏩ -{{ formatHelperSpeedupHours(building.template.speedup_minutes) }}h/人/h</span>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { BuildingInstance } from '../stores/adventurerS1'
import { useBuildingConstructionProgress } from '../composables/useBuildingConstructionProgress'
import {
  computeEffectiveUsageSlots,
  formatBuildingRemaining,
  formatHelperSpeedupHours,
  getUsageSlotLabel,
  TRANSCENDENT_ASSESSMENT_PREVIEW_DESCRIPTION,
  TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY,
  TRANSCENDENT_ASSESSMENT_PREVIEW_NAME,
} from '../utils'

const props = defineProps<{
  building: BuildingInstance
}>()

defineEmits<{
  click: []
}>()

const constructionSource = computed(() => props.building)
const {
  liveRemaining,
  liveProgress,
  isProgressPaused,
} = useBuildingConstructionProgress(constructionSource)

const statusClass = computed(() => 'status-' + props.building.status)
const isAssessmentPreview = computed(() => (
  props.building.template?.handler_key === TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY
))
const displayName = computed(() => (
  isAssessmentPreview.value
    ? TRANSCENDENT_ASSESSMENT_PREVIEW_NAME
    : (props.building.custom_name || props.building.template.name)
))
const displayDescription = computed(() => (
  isAssessmentPreview.value
    ? TRANSCENDENT_ASSESSMENT_PREVIEW_DESCRIPTION
    : props.building.template.description
))
const assessmentMinLevel = computed(() => (
  props.building.template?.settlement_config?.required_level ?? null
))

const isWaitingForDonations = computed(() => {
  return props.building.status === 'locked' && !!props.building.donation_open
})

const lockedStatusLabel = computed(() => {
  if (props.building.lv2_locked_preview) return '🔒 酒馆 Lv.2 解锁'
  return isWaitingForDonations.value ? '📣 募集中' : '🔒 未解锁'
})

const usageConfig = computed(() => {
  const cfg = props.building.template?.settlement_config
  if (!cfg) return null
  if (
    cfg.usage_stamina_cost
    || cfg.gold_cost
    || cfg.gold_cost_per_hp
    || cfg.gold_cost_per_hour
    || cfg.stamina_cost_per_hour
    || cfg.usage_duration_minutes_per_hp
    || cfg.gold_cost_per_minute
    || cfg.reward_type
  ) {
    return cfg
  }
  return null
})

const effectiveUsageSlots = computed(() => {
  const slots = computeEffectiveUsageSlots(props.building, usageConfig.value)
  return props.building.template?.handler_key === 'skill_school' ? slots * 2 : slots
})

const usageSlotLabel = computed(() => {
  if (props.building.template?.handler_key === 'skill_school') return '学艺中'
  return getUsageSlotLabel(
    props.building.template?.handler_key,
    props.building.template?.category,
  )
})

const displayRemaining = computed(() => {
  const text = formatBuildingRemaining(liveRemaining.value)
  if (isProgressPaused.value) return `${text}（无人帮忙，暂停）`
  return text
})

function categoryLabel(cat: string): string {
  return { dormitory: '宿舍区', medical: '治疗区', equipment: '装备区', leisure: '休闲区', gathering: '采集区', training: '修炼区', work: '打工区', market: '交易市场', auction: '拍卖行', trade: '交易区' }[cat] || ''
}
</script>

<style scoped>
.building-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 12px;
  min-width: 0;
  cursor: pointer;
  transition: transform 0.2s, box-shadow 0.2s;
  display: grid;
  grid-template-rows: auto auto 1fr auto auto;
  align-items: stretch;
  gap: 7px;
  color: var(--color-text);
  overflow-wrap: anywhere;
}
.building-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
}
.building-card.status-locked {
  opacity: 0.7;
}
.building-card.status-built {
  border-color: var(--color-success);
}
.building-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.building-icon {
  flex: 0 0 28px;
  font-size: 26px;
  line-height: 1.2;
}
.building-name {
  min-width: 0;
  color: var(--color-text);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.4;
}
.building-labels {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 6px;
}
.building-category {
  text-align: center;
  font-size: 0.75em;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(255, 193, 7, 0.2);
  color: #ffc107;
  display: inline-block;
  align-self: center;
}
.building-desc {
  font-size: 0.85em;
  color: var(--color-text-secondary);
  line-height: 1.55;
}

.building-status-badge {
  text-align: center;
  font-size: 0.8em;
  padding: 2px 6px;
  border-radius: 6px;
  font-weight: 500;
}
.badge-locked {
  background: rgba(128, 128, 128, 0.2);
  color: #888;
}
.badge-building,
.badge-upgrading {
  background: rgba(255, 193, 7, 0.2);
  color: #ffc107;
}
.badge-built {
  background: rgba(25, 135, 84, 0.2);
  color: var(--color-success);
}

.building-operation {
  display: grid;
  gap: 6px;
  padding-top: 8px;
  border-top: 1px solid var(--color-border);
}
.building-operation:empty { padding: 0; border: 0; }

.building-progress {
  display: flex;
  align-items: center;
  gap: 8px;
}
.progress-bar {
  flex: 1;
  height: 6px;
  background: var(--color-border);
  border-radius: 3px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: 3px;
  transition: width 0.3s;
}
.progress-text {
  font-size: 0.8em;
  color: var(--color-text-secondary);
  min-width: 36px;
  text-align: right;
}

.building-remaining {
  font-size: 0.85em;
  color: var(--color-warning);
}
.building-helpers {
  font-size: 0.8em;
  color: var(--color-text-secondary);
}
.helper-tag {
  color: var(--color-primary);
}

.building-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-size: 0.75em;
  color: var(--color-text-secondary);
  min-height: 1.5em;
  line-height: 1.5;
}
.assessment-preview-state {
  display: grid;
  gap: 4px;
  padding: 7px 9px;
  border: 1px solid #9d86d8;
  border: 1px solid color-mix(in srgb, #9d86d8 42%, var(--color-border));
  border-radius: 8px;
  color: #bba8ed;
  color: color-mix(in srgb, #c7b8ef 82%, var(--color-text));
  background: rgba(92, 68, 140, 0.1);
  font-size: 0.78em;
  letter-spacing: 0.08em;
  text-align: left;
}

/* 驻地设施牌：强调建造、运转与共同建设状态。 */
.building-card {
  --build-brass: #557fae;
  position: relative;
  overflow: hidden;
  border-color: var(--color-border);
  border-color: color-mix(in srgb, var(--build-brass) 18%, var(--color-border));
  border-radius: 10px;
  background:
    linear-gradient(145deg, rgba(85, 127, 174, 0.06), var(--color-surface) 58%),
    var(--color-surface);
  background:
    linear-gradient(145deg, color-mix(in srgb, var(--build-brass) 6%, var(--color-surface)), var(--color-surface) 58%),
    var(--color-surface);
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  box-shadow: inset 0 1px rgba(85, 127, 174, 0.12);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent);
}

/* 自定义属性的现代混色只在确认受支持时覆盖实色基线。 */
@supports (color: color-mix(in srgb, #000 50%, #fff)) {
  .building-card {
    --build-brass: color-mix(in srgb, #557fae 72%, var(--color-primary));
  }
}

.building-card:hover {
  border-color: var(--build-brass);
  border-color: color-mix(in srgb, var(--build-brass) 55%, var(--color-border));
  transform: translateY(-3px);
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.14);
}

.building-card.status-built {
  border-color: var(--color-success);
  border-color: color-mix(in srgb, var(--color-success) 48%, var(--color-border));
}
.building-card.status-built.assessment-preview-card {
  --build-brass: #9d86d8;
  border-color: #9d86d8;
  border-color: color-mix(in srgb, #9d86d8 58%, var(--color-border));
  background:
    radial-gradient(circle at 50% -20%, rgba(157, 134, 216, 0.16), transparent 48%),
    var(--color-surface);
  background:
    radial-gradient(circle at 50% -20%, rgba(157, 134, 216, 0.16), transparent 48%),
    linear-gradient(145deg, color-mix(in srgb, #9d86d8 7%, var(--color-surface)), var(--color-surface) 64%);
}
.building-icon {
  filter: drop-shadow(0 5px 8px rgba(85, 127, 174, 0.2));
  filter: drop-shadow(0 5px 8px color-mix(in srgb, var(--build-brass) 20%, transparent));
  transition: transform 0.2s ease;
}
.building-card:hover .building-icon { transform: translateY(-2px) scale(1.06); }
.building-category {
  background: var(--color-bg);
  background: color-mix(in srgb, var(--build-brass) 12%, var(--color-bg));
  color: var(--build-brass);
}
.progress-fill {
  background: linear-gradient(90deg, var(--build-brass), var(--color-primary));
  background: linear-gradient(90deg, var(--build-brass), color-mix(in srgb, var(--build-brass) 58%, var(--color-primary)));
  background-size: 180% 100%;
  animation: build-progress-flow 2.8s linear infinite;
}
.helper-tag { color: var(--build-brass); }

@keyframes build-progress-flow { to { background-position: -180% 0; } }

@media (max-width: 640px) {
  .building-card {
    padding: 10px 12px;
    gap: 6px;
  }
  .building-icon { flex-basis: 26px; font-size: 24px; }
  .building-card:hover {
    transform: none;
    box-shadow: inset 0 1px rgba(85, 127, 174, 0.12);
    box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent);
  }
  .building-card:active { transform: scale(0.985); border-color: var(--build-brass); }
  .building-card:hover .building-icon { transform: none; }
  .building-meta { flex-wrap: wrap; gap: 6px 10px; }
  .building-meta:empty { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .building-card,
  .building-icon,
  .progress-fill { animation: none; transition: none; }
}
</style>
