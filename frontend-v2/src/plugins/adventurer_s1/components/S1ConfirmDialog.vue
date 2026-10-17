<template>
  <Teleport to="body">
    <div class="s1-confirm-overlay s1-mobile-surface s1-mobile-overlay" role="presentation" @click.self="requestCancel">
      <section
        class="s1-confirm-dialog s1-mobile-dialog"
        role="alertdialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        :aria-describedby="messageId"
      >
        <h4 :id="titleId">{{ title }}</h4>
        <p :id="messageId">{{ message }}</p>
        <label v-if="requiredPhrase" class="s1-confirm-verification">
          <span>请输入“{{ requiredPhrase }}”以确认这项不可逆操作</span>
          <input
            v-model="verificationInput"
            type="text"
            autocomplete="off"
            :spellcheck="false"
            :disabled="busy"
            @keyup.enter="requestConfirm"
          >
        </label>
        <div class="s1-confirm-actions s1-mobile-dialog-footer">
          <button type="button" class="s1-confirm-cancel" :disabled="busy" @click="requestCancel">
            {{ cancelLabel }}
          </button>
          <button
            type="button"
            class="s1-confirm-submit"
            :disabled="busy || !verificationMatches"
            @click="requestConfirm"
          >
            {{ busy ? '处理中…' : confirmLabel }}
          </button>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'

const props = withDefaults(defineProps<{
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  busy?: boolean
  requiredPhrase?: string
}>(), {
  confirmLabel: '确认',
  cancelLabel: '取消',
  busy: false,
  requiredPhrase: '',
})

const emit = defineEmits<{
  confirm: []
  cancel: []
}>()

function requestCancel() {
  if (!props.busy) emit('cancel')
}

const verificationInput = ref('')
const verificationMatches = computed(() => (
  !props.requiredPhrase || verificationInput.value === props.requiredPhrase
))

function requestConfirm() {
  if (!props.busy && verificationMatches.value) emit('confirm')
}

watch(() => props.requiredPhrase, () => {
  verificationInput.value = ''
})

const titleId = `s1-confirm-title-${useId()}`
const messageId = `s1-confirm-message-${useId()}`
</script>

<style scoped>
.s1-confirm-overlay {
  position: fixed;
  inset: 0;
  z-index: 10080;
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  padding:
    max(20px, env(safe-area-inset-top, 0px))
    max(20px, env(safe-area-inset-right, 0px))
    max(20px, env(safe-area-inset-bottom, 0px))
    max(20px, env(safe-area-inset-left, 0px));
  background: rgb(0 0 0 / 68%);
  backdrop-filter: blur(3px);
}

.s1-confirm-dialog {
  width: min(100%, 420px);
  max-height: min(90dvh, 720px);
  box-sizing: border-box;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 22px;
  border: 1px solid var(--color-border, #3d4754);
  border-radius: 14px;
  background: var(--color-surface, #161b22);
  color: var(--color-text, #e6edf3);
  box-shadow: 0 18px 55px rgb(0 0 0 / 45%);
}

.s1-confirm-dialog h4 {
  margin: 0 0 12px;
  font-size: 18px;
}

.s1-confirm-dialog p {
  margin: 0;
  color: var(--color-text-secondary, #aab4c0);
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-line;
  overflow-wrap: anywhere;
}

.s1-confirm-verification {
  display: grid;
  gap: 8px;
  margin-top: 16px;
  color: var(--color-warning, #d29922);
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.s1-confirm-verification input {
  width: 100%;
  min-height: 44px;
  box-sizing: border-box;
  padding: 9px 11px;
  border: 1px solid color-mix(in srgb, var(--color-warning, #d29922) 60%, var(--color-border, #3d4754));
  border-radius: 8px;
  outline: none;
  background: var(--color-bg, #0d1117);
  color: var(--color-text, #e6edf3);
}

.s1-confirm-verification input:focus {
  border-color: var(--color-warning, #d29922);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-warning, #d29922) 22%, transparent);
}

.s1-confirm-actions {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.6fr);
  gap: 10px;
  margin-top: 22px;
}

.s1-confirm-actions button {
  min-width: 0;
  min-height: 44px;
  padding: 10px 12px;
  border-radius: 9px;
  font-family: inherit;
  font-size: 14px;
  font-weight: 700;
  line-height: 1.45;
  white-space: normal;
  overflow-wrap: anywhere;
  cursor: pointer;
}

.s1-confirm-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.s1-confirm-cancel {
  border: 1px solid var(--color-border, #3d4754);
  background: transparent;
  color: var(--color-text, #e6edf3);
}

.s1-confirm-submit {
  border: 1px solid color-mix(in srgb, var(--color-warning, #d29922) 70%, white);
  background: var(--color-warning, #d29922);
  color: #111;
}

@media (max-width: 640px) {
  .s1-confirm-dialog { padding: 18px 16px; }
  .s1-confirm-actions { gap: 8px; margin-top: 18px; }
}
</style>
