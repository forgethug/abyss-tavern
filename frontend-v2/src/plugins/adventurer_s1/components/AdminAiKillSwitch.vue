<template>
  <section
    class="ai-kill-switch"
    :class="{ stopped: aiCallsEnabled === false, unknown: aiCallsEnabled === null }"
    aria-labelledby="s1-ai-kill-switch-title"
  >
    <div class="switch-copy">
      <span class="switch-icon" aria-hidden="true">{{ aiCallsEnabled === false ? '⛔' : '🤖' }}</span>
      <div class="switch-text">
        <div class="switch-title-row">
          <h6 id="s1-ai-kill-switch-title">S1 AI 总闸</h6>
          <span class="status-pill" :class="{ stopped: aiCallsEnabled === false }" aria-live="polite">
            {{ statusLabel }}
          </span>
        </div>
        <p v-if="aiCallsEnabled === null">正在读取后端权威状态…</p>
        <p v-else-if="aiCallsEnabled">
          总闸允许新请求进入统一 AI 闸门；命中已配置的停用时段时仍会被拦截。点击右侧按钮会立即持久化关闭。
        </p>
        <p v-else>
          后端正在拦截所有新发起的 S1 外部 AI 请求；已在途的供应商请求无法撤销，恢复后新请求才会继续。
        </p>
      </div>
    </div>

    <button
      type="button"
      class="switch-action"
      :class="{ reopen: aiCallsEnabled === false }"
      :disabled="loading || saving"
      :aria-pressed="aiCallsEnabled === false"
      @click="toggleAiCalls"
    >
      {{ actionLabel }}
    </button>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import api from '@/composables/useApi'
import { useGlobalMessage } from '@/composables/useGlobalMessage'

const message = useGlobalMessage()
const aiCallsEnabled = ref<boolean | null>(null)
const loading = ref(false)
const saving = ref(false)

const statusLabel = computed(() => {
  if (aiCallsEnabled.value === null) return '状态未知'
  return aiCallsEnabled.value ? '总闸已开启' : '总闸已关闭'
})

const actionLabel = computed(() => {
  if (loading.value) return '读取中…'
  if (saving.value) return '切换中…'
  if (aiCallsEnabled.value === null) return '重新读取状态'
  return aiCallsEnabled.value ? '立即关闭全部 S1 AI' : '重新开启全部 S1 AI'
})

async function loadAiState(showError = true) {
  loading.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/expedition/config')
    const enabled = data?.data?.ai_calls_enabled
    if (data?.code !== 200 || typeof enabled !== 'boolean') {
      throw new Error(data?.message || '总闸状态响应无效')
    }
    aiCallsEnabled.value = enabled
  } catch (error: any) {
    aiCallsEnabled.value = null
    if (showError) {
      message.error(error?.response?.data?.message || error?.message || 'S1 AI 总闸状态读取失败')
    }
  } finally {
    loading.value = false
  }
}

async function toggleAiCalls() {
  if (aiCallsEnabled.value === null) {
    await loadAiState()
    return
  }

  const nextEnabled = !aiCallsEnabled.value
  saving.value = true
  try {
    const { data } = await api.post('/adventurer-s1/admin/expedition/config', {
      ai_calls_enabled: nextEnabled,
    })
    const persisted = data?.data?.ai_calls_enabled
    if (data?.code !== 200 || typeof persisted !== 'boolean') {
      throw new Error(data?.message || '总闸切换响应无效')
    }
    aiCallsEnabled.value = persisted
    message.success(
      persisted
        ? 'S1 AI 总闸已开启，新请求可以继续调用 AI'
        : 'S1 AI 总闸已关闭，后端将拦截所有新的 AI 调用',
    )
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || 'S1 AI 总闸切换失败')
    await loadAiState(false)
  } finally {
    saving.value = false
  }
}

onMounted(() => { void loadAiState() })
</script>

<style scoped lang="scss">
.ai-kill-switch {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-width: 0;
  padding: 14px 16px;
  border: 1px solid color-mix(in srgb, #16a34a 42%, var(--color-border));
  border-radius: 12px;
  background: color-mix(in srgb, #16a34a 7%, var(--color-surface, var(--color-bg)));
}

.ai-kill-switch.stopped {
  border-color: color-mix(in srgb, #dc2626 58%, var(--color-border));
  background: color-mix(in srgb, #dc2626 9%, var(--color-surface, var(--color-bg)));
}

.ai-kill-switch.unknown {
  border-color: var(--color-border);
  background: var(--color-surface, var(--color-bg));
}

.switch-copy {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.switch-icon {
  display: grid;
  flex: 0 0 40px;
  width: 40px;
  height: 40px;
  place-items: center;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-bg);
  font-size: 20px;
}

.switch-text {
  min-width: 0;
}

.switch-title-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.switch-title-row h6,
.switch-text p {
  margin: 0;
}

.switch-title-row h6 {
  color: var(--color-text);
  font-size: 14px;
}

.switch-text p {
  margin-top: 4px;
  color: var(--color-text-secondary, #718096);
  font-size: 12px;
  line-height: 1.55;
}

.status-pill {
  padding: 2px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, #16a34a 14%, transparent);
  color: #15803d;
  font-size: 11px;
  font-weight: 700;
}

.status-pill.stopped {
  background: color-mix(in srgb, #dc2626 15%, transparent);
  color: #dc2626;
}

.switch-action {
  flex: 0 0 auto;
  min-height: 40px;
  padding: 8px 14px;
  border: 1px solid #dc2626;
  border-radius: 9px;
  background: #dc2626;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.switch-action.reopen {
  border-color: #16a34a;
  background: #16a34a;
}

.switch-action:disabled {
  cursor: not-allowed;
  opacity: 0.58;
}

@media (max-width: 720px) {
  .ai-kill-switch {
    align-items: stretch;
    flex-direction: column;
  }

  .switch-action {
    width: 100%;
    min-height: 44px;
  }
}
</style>
