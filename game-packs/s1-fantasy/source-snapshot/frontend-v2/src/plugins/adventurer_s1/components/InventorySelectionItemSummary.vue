<template>
  <span class="inventory-selection-summary">
    <span class="inventory-selection-summary__name" :style="{ color: rarityColor(rarity) }">
      {{ name }}<span v-if="displayQuantity > 1">×{{ displayQuantity }}</span>
    </span>
    <span class="inventory-selection-summary__meta">
      {{ rarityLabel(rarity) }}<template v-for="(entry, index) in visibleMeta" :key="`${index}-${entry}`"> · {{ entry }}</template>
    </span>
    <span v-if="state" class="inventory-selection-summary__state">{{ state }}</span>
    <span v-if="description" class="inventory-selection-summary__description">{{ description }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { rarityColor, rarityLabel } from '../utils'

const props = defineProps<{
  name: string
  rarity: string
  quantity?: number | null
  meta?: Array<string | null | undefined>
  state?: string | null
  description?: string | null
}>()

const displayQuantity = computed(() => {
  const quantity = Number(props.quantity ?? 1)
  return Number.isFinite(quantity) ? Math.max(1, Math.floor(quantity)) : 1
})
const visibleMeta = computed(() => (
  (props.meta ?? [])
    .map(entry => String(entry ?? '').trim())
    .filter(Boolean)
))
</script>

<style scoped>
.inventory-selection-summary {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  line-height: 1.4;
}

.inventory-selection-summary__name {
  font-weight: 500;
  overflow-wrap: anywhere;
}

.inventory-selection-summary__meta,
.inventory-selection-summary__description {
  color: var(--color-text-secondary);
  font-size: 0.8em;
}

.inventory-selection-summary__state {
  color: var(--color-warning);
  font-size: 0.78em;
  line-height: 1.35;
}

.inventory-selection-summary__description {
  line-height: 1.4;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
</style>
