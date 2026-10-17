<template>
  <aside v-if="enabled && !onLogs && total > 0" class="team-loot-notice" role="status">
    <span>🎁 你参与的小队还有 {{ total }} 份战利品未分配完成。
      <template v-if="summary.voting">{{ summary.voting }} 份正在选择分配方式。</template>
      <template v-if="summary.bidding">{{ summary.bidding }} 份正在竞价。</template>
      <template v-if="summary.claim_pending">{{ summary.claim_pending }} 份等待胜出者领取。</template>
    </span>
    <button type="button" class="btn-secondary btn-sm" @click="emit('open')">查看战利品</button>
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useSocket } from '@/composables/useSocket'
import { fetchTeamLootSummary, type TeamLootSummary } from '../api/teamLoot'

const props = defineProps<{ enabled: boolean; onLogs: boolean; userId: number | null; realm: string | null }>()
const emit = defineEmits<{ open: [] }>()
const socket = useSocket()
const empty = (): TeamLootSummary => ({ voting: 0, bidding: 0, rolling: 0, claim_pending: 0 })
const summary = ref(empty())
const total = computed(() => Object.values(summary.value).reduce((sum, value) => sum + value, 0))
let timer: ReturnType<typeof setInterval> | undefined
let disposed = false, generation = 0, refreshedAt = 0
let pending: Promise<void> | null = null
let dirty = false

async function refresh() {
  if (disposed || !props.enabled || !props.userId || document.visibilityState !== 'visible') return
  if (pending) { dirty = true; return pending }
  if (Date.now() - refreshedAt < 5000) { dirty = true; return }
  const current = generation
  dirty = false
  const request = (async () => {
    try {
      const data = await fetchTeamLootSummary()
      if (!disposed && current === generation) summary.value = data
    } catch { /* A notice failure must not block ordinary play. */ }
    finally { if (current === generation) refreshedAt = Date.now() }
  })()
  pending = request
  await request
  if (pending === request) pending = null
}
const onChanged = () => { dirty = true; void refresh() }
const onVisible = () => { if (document.visibilityState === 'visible') void refresh() }
watch(() => [props.enabled, props.userId, props.realm], () => {
  generation += 1; summary.value = empty(); refreshedAt = 0; pending = null
  void refresh()
})
onMounted(() => {
  void refresh()
  socket.on('expedition_log_changed', onChanged)
  socket.on('adventurer_changed', onChanged)
  document.addEventListener('visibilitychange', onVisible)
  timer = setInterval(() => { if (dirty || props.enabled) void refresh() }, 30_000)
})
onUnmounted(() => {
  disposed = true; generation += 1
  if (timer) clearInterval(timer)
  socket.off('expedition_log_changed', onChanged)
  socket.off('adventurer_changed', onChanged)
  document.removeEventListener('visibilitychange', onVisible)
})
</script>

<style scoped>
.team-loot-notice { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding: 12px 16px; margin: 12px 0; border: 1px solid var(--primary-color, #b69856); border-radius: 8px; line-height: 1.6; }
</style>
