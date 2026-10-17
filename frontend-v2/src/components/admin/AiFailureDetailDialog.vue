<template>
  <dialog ref="dialog" class="failure-dialog" @cancel.prevent="$emit('close')">
    <header>
      <strong>当前酒馆 AI 失败全文 #{{ recordId }}</strong>
      <button type="button" aria-label="关闭失败全文" @click="$emit('close')">关闭</button>
    </header>
    <p v-if="loading">正在读取全文…</p>
    <p v-if="errorMessage" role="alert">{{ errorMessage }}</p>
    <template v-if="data">
      <p>
        {{ data.record.call_type }} · {{ data.record.model }} · {{ data.record.endpoint_host }}
        · {{ data.record.validation_error_code || data.record.failure_stage }}
      </p>
      <p>{{ responseStatus }}；全文保留至管理员清理。</p>
      <details open>
        <summary>原始响应（{{ data.detail.response.format }}）</summary>
        <pre>{{ data.detail.response.body ?? '没有收到可保存的响应正文' }}</pre>
      </details>
      <details>
        <summary>实际请求全文</summary>
        <pre>{{ JSON.stringify(data.detail.request_payload, null, 2) }}</pre>
      </details>
      <details>
        <summary>完整错误详情</summary>
        <pre>{{ JSON.stringify({ contract: data.detail.contract_diagnostics, exceptions: data.detail.exceptions, owner_error: data.detail.owner_error }, null, 2) }}</pre>
      </details>
      <footer>
        <button type="button" @click="downloadDetail">下载记录</button>
        <button type="button" :disabled="clearing" @click="clearDetail">
          {{ clearing ? '正在清理…' : '已排查，清理这条全文' }}
        </button>
      </footer>
    </template>
  </dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import api from '@/composables/useApi'

const props = defineProps<{ recordId: number; realm: 'region_1' | 'region_2' }>()
const emit = defineEmits<{ close: []; cleared: [recordId: number] }>()

interface FailureDetail {
  record: {
    call_type: string
    model: string | null
    endpoint_host: string | null
    failure_stage: string | null
    validation_error_code: string | null
  }
  detail: {
    request_payload: Record<string, unknown>
    response: { format: string; complete: boolean; body: string | null }
    contract_diagnostics: Record<string, unknown>
    exceptions: unknown[]
    owner_error: string | null
  }
}

const dialog = ref<HTMLDialogElement | null>(null)
const data = ref<FailureDetail | null>(null)
const loading = ref(true)
const clearing = ref(false)
const errorMessage = ref('')
const controller = new AbortController()
const responseStatus = computed(() => {
  const response = data.value?.detail.response
  if (response?.body == null) return '响应正文未取得'
  return response.complete ? '已保存完整响应' : '响应未收完整，已保存实际收到的全部片段'
})

onMounted(async () => {
  dialog.value?.showModal()
  try {
    const response = await api.get(`/adventurer-s1/admin/token-usage/records/${props.recordId}/failure-detail`, {
      s1AdminRealmTarget: props.realm, signal: controller.signal,
    })
    if (response.data.code !== 200) throw new Error(response.data.message || '读取失败')
    data.value = response.data.data
  } catch (error) {
    if (!controller.signal.aborted) errorMessage.value = error instanceof Error ? error.message : '读取失败'
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => controller.abort())

async function clearDetail() {
  if (clearing.value || !window.confirm(`清理当前酒馆调用 #${props.recordId} 的全文？调用统计和错误摘要将保留。`)) return
  clearing.value = true
  errorMessage.value = ''
  try {
    const response = await api.post('/adventurer-s1/admin/token-usage/failure-details/clear', {
      record_ids: [props.recordId],
    }, { s1AdminRealmTarget: props.realm, signal: controller.signal })
    if (response.data.code !== 200) throw new Error(response.data.message || '清理失败')
    emit('cleared', props.recordId)
    emit('close')
  } catch (error) {
    if (!controller.signal.aborted) errorMessage.value = error instanceof Error ? error.message : '清理失败'
  } finally {
    clearing.value = false
  }
}

function downloadDetail() {
  if (!data.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(data.value, null, 2)], { type: 'application/json' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `ai-failure-${props.realm}-${props.recordId}.json`
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
</script>

<style scoped>
.failure-dialog {
  width: min(1100px, 92vw);
  max-height: 88vh;
  overflow: auto;
  background: var(--color-bg, white);
  color: var(--color-text, #222);
  border: 1px solid var(--color-border, #aaa);
  border-radius: 12px;
  padding: 20px;
}
.failure-dialog::backdrop { background: rgb(0 0 0 / 55%); }
header, footer { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
details { margin: 16px 0; }
summary { cursor: pointer; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; line-height: 1.6; }
button { cursor: pointer; padding: 6px 12px; }
</style>
