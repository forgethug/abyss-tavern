<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import axios from 'axios'
import api from '@/composables/useApi'
import { getActiveTavernSlug } from '@/runtime/activeTavern'

interface Settings { compression_enabled: boolean; revision: string }
const tavern = getActiveTavernSlug()
const settings = ref<Settings | null>(null)
const busy = ref(false), error = ref(''), notice = ref('')
let mounted = true
const current = () => mounted && getActiveTavernSlug() === tavern
async function requestSettings(toggle = false) {
  if (!current() || busy.value || (toggle && !settings.value)) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = toggle
      ? await api.put('/adventurer-s1/admin/ai/input-settings', {
          compression_enabled: !settings.value!.compression_enabled, revision: settings.value!.revision,
        })
      : await api.get('/adventurer-s1/admin/ai/input-settings')
    if (current()) {
      settings.value = response.data.data
      if (toggle) notice.value = '已保存，仅对当前酒馆后续生成的输入生效。已冻结的探险输入保持一致。'
    }
  } catch (e) {
    if (current() && !axios.isCancel(e)) error.value = axios.isAxiosError(e)
      ? e.response?.data?.message || '设置读取或保存失败，请重试' : '设置操作失败'
  } finally { if (current()) busy.value = false }
}
onMounted(() => requestSettings())
onBeforeUnmount(() => { mounted = false })
</script>

<template>
  <section class="input-settings" aria-label="AI 输入压缩设置">
    <h3>资产描述压缩与特质分类筛选</h3>
    <p>默认关闭：相关特质、技能和装备描述按原文发送给 AI，避免压缩或分类筛选造成的信息遗漏。
      上下文会更长，模型可能遗忘细节、分散注意力，也会消耗更多 Token、增加费用。</p>
    <p>开启后使用审核通过的摘要与特质场景筛选，减少输入长度和 Token 开销，但存在信息遗漏风险，且生成摘要与分类本身也会调用 AI。</p>
    <p>此设置不改变装备、归档和资产依赖等游戏生效规则；已发出的 AI 请求不会立即取消。</p>
    <button type="button" role="switch" :aria-checked="settings?.compression_enabled ?? false"
      :disabled="busy || !settings" @click="requestSettings(true)">
      {{ busy ? '处理中…' : settings ? (settings.compression_enabled ? '压缩已开启 · 点击关闭' : '压缩已关闭 · 点击开启') : '正在读取设置…' }}
    </button>
    <button v-if="error" type="button" :disabled="busy" @click="requestSettings()">刷新设置</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.input-settings { padding: 16px; margin-bottom: 16px; border: 1px solid var(--color-border); border-radius: 10px; }
h3 { margin: 0 0 10px; }
p { line-height: 1.7; color: var(--color-text-secondary); }
button { padding: 8px 12px; background: var(--color-surface); color: var(--color-text); border: 1px solid var(--color-border); border-radius: 6px; cursor: pointer; }
button:disabled { opacity: .6; cursor: wait; }
[role=alert] { color: var(--color-danger, #e17474); }
</style>
