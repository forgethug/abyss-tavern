<template>
  <div v-if="result.type === 'pet_evolution' && !result.system_abort" class="evolution-inputs">
    <p v-if="result.old_name && result.success && result.new_name">
      {{ result.old_name }} → <strong>{{ result.new_name }}</strong>
    </p>
    <details v-for="(item, index) in inputs" :key="index">
      <summary>{{ item.role === 'primary' ? '主物品' : '辅助材料' }}：<span>{{ item.name }}</span></summary>
      <p>{{ item.description || '这条记录未保存当时的物品描述。' }}</p>
    </details>
    <p v-if="result.auxiliary_material_name && result.auxiliary_material_consumed">辅助材料已消耗</p>
    <p v-else-if="!result.auxiliary_material_name">本次未投入辅助材料</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { BuildingUsageResult } from '../stores/adventurerS1'
const props = defineProps<{ result: BuildingUsageResult }>()
const inputs = computed<NonNullable<BuildingUsageResult['input_items']>>(() => props.result.input_items ?? [
  ...(props.result.old_name ? [{ role: 'primary' as const, name: props.result.old_name }] : []),
  ...(props.result.auxiliary_material_name ? [{ role: 'auxiliary' as const, name: props.result.auxiliary_material_name }] : []),
])
</script>

<style scoped>
.evolution-inputs { margin: 8px 0; font-size: 13px; line-height: 1.7; }
.evolution-inputs p { margin: 4px 0; white-space: pre-wrap; overflow-wrap: anywhere; }
details { margin: 4px 0; }
summary { cursor: pointer; min-height: 28px; }
summary span { color: var(--color-primary); text-decoration: underline; text-underline-offset: 3px; }
</style>
