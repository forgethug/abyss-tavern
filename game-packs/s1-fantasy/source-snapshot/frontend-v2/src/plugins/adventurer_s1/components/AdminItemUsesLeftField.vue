<template>
  <label class="form-label">
    {{ label }}
    <input
      :value="displayValue"
      type="number"
      min="0"
      class="advs1-input"
      :placeholder="placeholder"
      :disabled="disabled"
      @input="onInput"
    />
    <span v-if="hint" class="field-hint">{{ hint }}</span>
  </label>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: number | null
  label?: string
  placeholder?: string
  hint?: string
  disabled?: boolean
}>(), {
  label: '默认使用次数',
  placeholder: '留空按类型默认',
  hint: '发放到冒险者时的剩余次数；永久装备留空即可。',
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: number | null]
}>()

const displayValue = computed(() => (props.modelValue == null ? '' : props.modelValue))

function onInput(event: Event) {
  const raw = (event.target as HTMLInputElement).value.trim()
  if (!raw) {
    emit('update:modelValue', null)
    return
  }
  const n = Number(raw)
  emit('update:modelValue', Number.isFinite(n) ? n : null)
}
</script>

<style scoped>
.form-label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-bottom: 0.75rem;
  font-size: 0.9rem;
  font-weight: 500;
}
.field-hint {
  font-size: 0.78rem;
  color: var(--color-text-secondary);
  font-weight: 400;
  line-height: 1.4;
}
</style>
