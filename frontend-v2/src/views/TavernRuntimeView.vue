<script setup lang="ts">
import axios from 'axios'
import { markRaw, onBeforeUnmount, ref, shallowRef, watch, type Component } from 'vue'
import { useRoute } from 'vue-router'

import apiClient, { type ApiEnvelope } from '@/api/client'
import { tavernLifecycle } from '@/runtime/activeTavern'
import { loadGamePackRuntimeFrontend } from '@/runtime/gamePackFrontendRegistry'
import type { TavernContext } from '@/types/platform'


const route = useRoute()
const context = ref<TavernContext | null>(null)
const runtimeComponent = shallowRef<Component | null>(null)
const error = ref('')
let entryRevision = 0

async function enterRuntime(slug: string) {
  const revision = ++entryRevision
  context.value = null
  runtimeComponent.value = null
  error.value = ''
  try {
    const session = tavernLifecycle.begin(slug)
    const response = await apiClient.get<ApiEnvelope<TavernContext>>(
      `/taverns/${encodeURIComponent(slug)}/context`,
      { signal: session.signal },
    )
    if (revision !== entryRevision || !tavernLifecycle.isCurrent(session)) return
    const runtimeContext = response.data.data
    const component = await loadGamePackRuntimeFrontend(
      runtimeContext.runtime.frontend_plugin,
    )
    if (revision !== entryRevision || !tavernLifecycle.isCurrent(session)) return
    context.value = runtimeContext
    runtimeComponent.value = markRaw(component)
  } catch (caught) {
    if (revision !== entryRevision || tavernLifecycle.get()?.signal.aborted) return
    error.value = axios.isAxiosError(caught)
      ? caught.response?.data?.message || '无法进入酒馆'
      : '无法进入酒馆'
  }
}

watch(
  () => String(route.params.slug || ''),
  (slug) => {
    if (slug) void enterRuntime(slug)
  },
  { immediate: true, flush: 'sync' },
)

onBeforeUnmount(() => {
  entryRevision += 1
  tavernLifecycle.end()
})
</script>

<template>
  <section v-if="context && runtimeComponent" class="runtime-host">
    <div class="runtime-identity-bar">
      <RouterLink to="/">← 返回酒馆列表</RouterLink>
      <strong>{{ context.tavern.display_name }}</strong>
    </div>
    <component :is="runtimeComponent" :key="context.tavern.slug" />
  </section>
  <p v-else-if="error" class="error-message">{{ error }}</p>
  <div v-else class="empty-state">正在启动并进入酒馆…</div>
</template>

<style scoped>
.runtime-host {
  min-width: 0;
}

.runtime-identity-bar {
  align-items: center;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  color: var(--color-text-secondary);
  display: flex;
  gap: 16px;
  justify-content: space-between;
  margin: 0 16px 8px;
  padding: 10px 14px;
}

.runtime-identity-bar a {
  color: var(--color-primary);
}
</style>
