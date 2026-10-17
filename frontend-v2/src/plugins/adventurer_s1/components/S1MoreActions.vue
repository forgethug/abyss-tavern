<template>
  <details ref="details" class="s1-more-actions" @keydown.esc="closeAndFocus">
    <summary>更多<span aria-hidden="true">⌄</span></summary>
    <div class="s1-more-actions-content" @click="closeAfterAction">
      <slot />
    </div>
  </details>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const details = ref<HTMLDetailsElement | null>(null)

function closeAfterAction(event: MouseEvent) {
  const button = event.target instanceof Element ? event.target.closest('button') : null
  if (button && !button.disabled && details.value) details.value.open = false
}

function closeAndFocus(event: KeyboardEvent) {
  if (!details.value?.open) return
  event.stopPropagation()
  event.preventDefault()
  details.value.open = false
  details.value.querySelector('summary')?.focus()
}
</script>

<style scoped>
.s1-more-actions {
  min-width: 0;
  max-width: 100%;
  margin-right: auto;
}

.s1-more-actions[open] { flex-basis: 100%; }

summary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 34px;
  box-sizing: border-box;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.4;
  list-style: none;
  cursor: pointer;
  touch-action: manipulation;
}

summary::-webkit-details-marker { display: none; }
summary:hover, .s1-more-actions[open] > summary { background: var(--color-bg); }
summary:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
.s1-more-actions[open] > summary span { transform: rotate(180deg); }

.s1-more-actions-content {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.s1-more-actions-content :slotted(button) {
  min-width: 0;
  max-width: 100%;
  margin: 0;
  font-size: 13px;
  white-space: normal;
}
</style>
