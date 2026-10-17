<script setup lang="ts">
import TavernModelConfigEditor from './TavernModelConfigEditor.vue'
import TavernAiInputSettings from './TavernAiInputSettings.vue'
import { defineAsyncComponent, ref } from 'vue'
const TavernTokenUsagePanel = defineAsyncComponent(() => import('./TavernTokenUsagePanel.vue'))
const showUsage = ref(false)
defineProps<{
  scope: 's0' | 's1'
  title?: string
}>()
</script>

<template>
  <TavernAiInputSettings v-if="scope === 's1'" />
  <TavernModelConfigEditor v-if="scope === 's1'" />
  <section v-if="scope === 's1'" class="usage-section">
    <button type="button" @click="showUsage = !showUsage">{{ showUsage ? '收起 AI 用量统计' : '查看 AI 用量统计与失败详情' }}</button>
    <TavernTokenUsagePanel v-if="showUsage" />
  </section>
</template>

<style scoped>
.usage-section { margin-top: 14px; padding: 14px; border: 1px solid var(--color-border); border-radius: 10px; }
.usage-section > button { padding: 8px 12px; background: var(--color-surface); color: var(--color-text); border: 1px solid var(--color-border); border-radius: 6px; cursor: pointer; }
</style>
