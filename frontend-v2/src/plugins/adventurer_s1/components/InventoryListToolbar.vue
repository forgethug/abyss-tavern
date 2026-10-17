<template>
  <div class="inventory-toolbar">
    <div class="rarity-filter" role="group" :aria-label="ariaLabel">
      <button
        v-for="option in rarityOptions"
        :key="option.value"
        type="button"
        class="filter-chip"
        :class="{ active: rarity === option.value }"
        :style="option.value === 'all' ? undefined : { '--rarity-filter-color': rarityColor(option.value) }"
        :aria-pressed="rarity === option.value"
        @click="emit('update:rarity', option.value)"
      >
        {{ option.label }}
        <span>{{ option.count }}</span>
      </button>
    </div>
    <label class="inventory-sort">
      <span>排序</span>
      <select
        :value="sort"
        :aria-label="`${ariaLabel}排序方式`"
        @change="onSortChange"
      >
        <option value="default">默认顺序</option>
        <option value="newest">新获得优先</option>
        <option value="rarity-desc">稀有度：高到低</option>
        <option value="rarity-asc">稀有度：低到高</option>
        <option value="name-asc">名称：正序</option>
        <option value="quantity-desc">数量：多到少</option>
      </select>
    </label>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { rarityColor, rarityLabel } from '../utils'
import {
  INVENTORY_RARITIES,
  type InventoryListItem,
  type InventoryRarityFilter,
  type InventorySort,
} from '../composables/useInventoryListControls'

const props = withDefaults(defineProps<{
  items: InventoryListItem[]
  rarity: InventoryRarityFilter
  sort: InventorySort
  ariaLabel?: string
}>(), {
  ariaLabel: '按稀有度筛选物品',
})

const emit = defineEmits<{
  'update:rarity': [value: InventoryRarityFilter]
  'update:sort': [value: InventorySort]
}>()

const rarityOptions = computed(() => [
  { value: 'all' as const, label: '全部', count: props.items.length },
  ...INVENTORY_RARITIES.map(value => ({
    value,
    label: rarityLabel(value),
    count: props.items.filter(item => (item.rarity ?? 'common') === value).length,
  })),
])

function onSortChange(event: Event) {
  emit('update:sort', (event.target as HTMLSelectElement).value as InventorySort)
}
</script>

<style scoped>
.inventory-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 10px 0;
  min-width: 0;
}
.rarity-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}
.filter-chip {
  appearance: none;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 999px;
  background: var(--color-bg, #0d1117);
  color: var(--color-text-secondary, #8b949e);
  padding: 4px 9px;
  font-size: 0.78rem;
  cursor: pointer;
}
.filter-chip:hover,
.filter-chip.active {
  border-color: var(--rarity-filter-color, var(--color-primary, #58a6ff));
  color: var(--rarity-filter-color, var(--color-primary, #58a6ff));
}
.filter-chip.active {
  background: color-mix(in srgb, var(--rarity-filter-color, var(--color-primary, #58a6ff)) 14%, var(--color-bg, #0d1117));
}
.filter-chip span {
  margin-left: 3px;
  opacity: 0.75;
}
.inventory-sort {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  color: var(--color-text-secondary, #8b949e);
  font-size: 0.8rem;
}
.inventory-sort select {
  min-width: 138px;
  padding: 5px 8px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 6px;
  background: var(--color-bg, #0d1117);
  color: var(--color-text, #e6edf3);
}
@media (max-width: 640px) {
  .inventory-toolbar { align-items: stretch; flex-direction: column; gap: 8px; padding: 8px; border: 1px solid var(--color-border); border-radius: 9px; background: var(--color-bg); }
  .rarity-filter { display: flex; flex-wrap: wrap; gap: 4px; }
  .filter-chip { flex: 0 1 auto; min-width: 0; min-height: 28px; padding: 4px 7px; border-color: transparent; border-radius: 6px; background: transparent; font-size: 11px; }
  .inventory-sort { justify-content: space-between; }
  .inventory-sort select { flex: 1; min-width: 0; }
}
</style>
