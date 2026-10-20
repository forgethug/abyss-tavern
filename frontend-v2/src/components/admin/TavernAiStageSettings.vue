<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import axios from 'axios'
import api from '@/composables/useApi'
import { getActiveTavernSlug } from '@/runtime/activeTavern'

type Stage = 'appeal_second_review' | 'expedition_observer' | 'high_risk_adjudication'
type Settings = Record<Stage, boolean> & { revision: string }
const stages: { key: Stage; title: string; description: string }[] = [
  { key: 'appeal_second_review', title: '申诉二审 AI', description: '关闭后省去自动二审，按初审结论和后端校验处理申诉；不会再由二审补充或纠正初审。玩家主动复核和撤回流程不受此开关影响。' },
  { key: 'expedition_observer', title: '探险剧情审核 AI', description: '关闭后省去剧情审核及其配套裁决，主剧情结果通过后端校验后继续结算；减少 AI 调用，但失去独立检查与修正剧情、奖励和角色变化的机会。' },
  { key: 'high_risk_adjudication', title: '高危事件辅助 AI（两个）', description: '同时控制高危主剧情因果底稿和生命资源一致性复核。关闭后由主剧情 AI 直接处理高危事件，仍保留客观大纲和后端数据校验。' },
]
const tavern = getActiveTavernSlug()
const settings = ref<Settings | null>(null)
const busy = ref(false), error = ref(''), notice = ref('')
let mounted = true
const current = () => mounted && getActiveTavernSlug() === tavern
async function requestSettings(stage?: Stage) {
  if (!current() || busy.value || (stage && !settings.value)) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = stage
      ? await api.put('/adventurer-s1/admin/ai/stage-settings', { ...settings.value!, [stage]: !settings.value![stage] })
      : await api.get('/adventurer-s1/admin/ai/stage-settings')
    if (current()) {
      settings.value = response.data.data
      if (stage) notice.value = '已保存，仅影响当前酒馆。已发出的 AI 请求不会取消。'
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
  <section class="stage-settings" aria-label="AI 流程开关">
    <h3>AI 流程开关</h3>
    <p>三个开关默认开启，保留当前流程；可按需关闭以减少调用与等待。开启表示允许对应阶段按原有条件触发，不代表每次都调用。身份、数据结构、数量和事务校验始终保留。</p>
    <div v-for="stage in stages" :key="stage.key" class="stage">
      <h4>{{ stage.title }}</h4>
      <p>{{ stage.description }}</p>
      <button type="button" role="switch" :aria-label="stage.title"
        :aria-checked="settings?.[stage.key] ?? true" :disabled="busy || !settings"
        @click="requestSettings(stage.key)">
        {{ settings ? (settings[stage.key] ? '已开启 · 点击关闭' : '已关闭 · 点击开启') : '正在读取设置…' }}
      </button>
    </div>
    <button v-if="error" type="button" :disabled="busy" @click="requestSettings()">刷新设置</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.stage-settings { padding: 16px; margin-bottom: 16px; border: 1px solid var(--color-border); border-radius: 10px; }
h3, h4 { margin: 0 0 10px; }
.stage { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--color-border); }
p { line-height: 1.7; color: var(--color-text-secondary); }
button { padding: 8px 12px; background: var(--color-surface); color: var(--color-text); border: 1px solid var(--color-border); border-radius: 6px; cursor: pointer; }
button:disabled { opacity: .6; cursor: wait; }
[role=alert] { color: var(--color-danger, #e17474); }
</style>
