<template>
  <details v-if="text" class="item-description" @click.stop>
    <summary>
      <span class="description-preview">{{ text }}</span>
      <span class="description-toggle">
        <span class="description-expand">展开说明</span>
        <span class="description-collapse">收起说明</span>
      </span>
    </summary>
    <p class="description-full">{{ text }}</p>
  </details>
</template>

<script setup lang="ts">
defineProps<{ text?: string | null }>()
</script>

<style scoped>
.item-description {
  min-width: 0;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
summary { cursor: pointer; list-style: none; }
summary::-webkit-details-marker { display: none; }
summary:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 3px; border-radius: 4px; }
.description-preview {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}
.description-toggle {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  color: var(--color-primary);
  font-size: 12px;
}
.description-toggle::after { content: '⌄'; margin-left: 5px; }
.description-collapse, [open] .description-expand, [open] .description-preview { display: none; }
[open] .description-collapse { display: inline; }
[open] .description-toggle::after { content: '⌃'; }
.description-full { margin: 0; white-space: pre-wrap; }
</style>
