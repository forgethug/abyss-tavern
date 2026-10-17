<template>
  <section class="tavern-upgrade-panel">
    <div class="upgrade-head">
      <div class="upgrade-title-wrap">
        <span class="upgrade-icon">🏰</span>
        <div>
          <h3 class="upgrade-title">酒馆等级 Lv.{{ progress?.tavern_level ?? 1 }}</h3>
          <p class="upgrade-subtitle">
            <template v-if="progress?.building_upgrade_unlocked">
              建筑 2 级改造已开放
            </template>
            <template v-else-if="progress?.next_level">
              全服存活冒险者达 {{ progress.next_level_requirement }} 名可解锁升级条件，管理员确认后酒馆升至 Lv.{{ progress.next_level }}
            </template>
            <template v-else>
              酒馆已达当前最高等级
            </template>
          </p>
        </div>
      </div>
      <button
        v-if="showAdminUnlockButton"
        type="button"
        class="btn-primary btn-sm upgrade-btn"
        :disabled="!progress?.awaiting_admin_unlock || !store.realmViewCode || unlocking || forcing"
        @click="onAdminUnlock"
      >
        {{ unlocking ? '解锁中…' : `解锁升至 Lv.${progress?.next_level}` }}
      </button>
      <button
        v-if="isAdmin && progress?.next_level != null"
        type="button"
        class="btn-sm upgrade-btn gm-force-btn"
        :disabled="!store.realmViewCode || forcing || unlocking"
        @click="onGmForceUpgrade"
        title="GM 测试：无视门槛直接升级并补建 Lv2 建筑"
      >
        {{ forcing ? '升级中…' : `🔧 GM 升 Lv.${progress?.next_level}` }}
      </button>
    </div>

    <div v-if="showProgressBar" class="upgrade-progress">
      <div class="progress-meta">
        <span>升级条件 · 全服存活冒险者</span>
        <span class="progress-count">
          {{ progress?.progress_current ?? 0 }} / {{ progress?.progress_required ?? '—' }}
        </span>
      </div>
      <div class="progress-bar">
        <div
          class="progress-fill"
          :class="{ complete: progress?.requirement_met }"
          :style="{ width: (progress?.progress_percent ?? 0) + '%' }"
        ></div>
      </div>
      <p v-if="progress?.awaiting_admin_unlock" class="progress-hint ready">
        升级条件已满足{{ isAdmin ? '，请点击右侧按钮解锁' : '，等待管理员解锁酒馆升级' }}
      </p>
      <p v-else-if="!progress?.requirement_met" class="progress-hint">
        还差 {{ remainingCount }} 名冒险者
      </p>
    </div>

    <div v-else-if="progress?.building_upgrade_unlocked" class="upgrade-unlocked">
      <span class="unlock-badge">✨ 建筑升级已解锁</span>
      <span class="unlock-desc">进入各分区已建成的建筑，点击即可发起 2 级改造</span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { useAdminSecondaryAccess } from '@/composables/useAdminSecondaryAccess'
import { useGlobalMessage } from '@/composables/useGlobalMessage'

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const unlocking = ref(false)
const forcing = ref(false)

const { canUseFullAdminUi: isAdmin } = useAdminSecondaryAccess()
const progress = computed(() => store.tavernUpgrade)

const showProgressBar = computed(() => {
  const p = progress.value
  return !!p && p.next_level != null && !p.building_upgrade_unlocked
})

const showAdminUnlockButton = computed(() => {
  const p = progress.value
  return isAdmin.value && !!p && p.next_level != null && !p.building_upgrade_unlocked
})

const remainingCount = computed(() => {
  const p = progress.value
  if (!p) return 0
  return Math.max(0, (p.progress_required ?? 0) - (p.alive_adventurer_count ?? 0))
})

async function onAdminUnlock() {
  if (!isAdmin.value || unlocking.value || forcing.value) return
  const realm = store.realmViewCode
  const nextLevel = progress.value?.next_level
  if (!realm || !nextLevel || !progress.value?.awaiting_admin_unlock) return
  const realmLabel = '当前酒馆'
  if (!confirm(`确认解锁${realmLabel}的酒馆升级至 Lv.${nextLevel}？解锁后将开放建筑 2 级改造入口。`)) return

  unlocking.value = true
  try {
    const res = await store.adminUnlockTavernUpgrade(realm)
    if (res?.code === 200) {
      message.success(`${realmLabel}的酒馆已解锁升级至 Lv.${res.data?.tavern_level ?? nextLevel}`)
      if (res.refresh_failed) message.warning('升级已成功，页面状态刷新失败，请刷新页面确认')
    } else {
      message.error(res?.message || '解锁失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '解锁请求未完成，请刷新确认酒馆等级后再操作')
  } finally {
    unlocking.value = false
  }
}

async function onGmForceUpgrade() {
  if (!isAdmin.value || forcing.value || unlocking.value) return
  const realm = store.realmViewCode
  const nextLevel = progress.value?.next_level
  if (!realm || !nextLevel) return
  const realmLabel = '当前酒馆'
  if (!confirm(`GM 测试：无视升级门槛，将${realmLabel}的酒馆升级至 Lv.${nextLevel}，并补建 Lv2 建筑（铁矿山 / 熔铁坊 / 深渊手术室）？`)) return
  forcing.value = true
  try {
    const res = await store.adminForceSetTavernLevel(realm, nextLevel)
    if (res?.code === 200) {
      message.success(`${realmLabel}的酒馆已升至 Lv.${res.data?.tavern_level ?? nextLevel}`)
      if (res.refresh_failed) message.warning('升级已成功，页面状态刷新失败，请刷新页面确认')
    } else {
      message.error(res?.message || '升级失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '升级请求未完成，请刷新确认酒馆等级后再操作')
  } finally {
    forcing.value = false
  }
}
</script>

<style scoped>
.tavern-upgrade-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px 18px;
  margin-bottom: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.upgrade-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.upgrade-title-wrap {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
}

.upgrade-icon {
  font-size: 1.8em;
  line-height: 1;
  flex-shrink: 0;
}

.upgrade-title {
  margin: 0;
  font-size: 1.05em;
  font-weight: 600;
  color: var(--color-text);
}

.upgrade-subtitle {
  margin: 4px 0 0;
  font-size: 0.85em;
  color: var(--color-text-secondary);
  line-height: 1.45;
}

.upgrade-btn {
  flex-shrink: 0;
  white-space: nowrap;
}

.gm-force-btn {
  background: transparent;
  border: 1px dashed var(--color-border);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.upgrade-progress {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.progress-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.85em;
  color: var(--color-text-secondary);
}

.progress-count {
  font-weight: 600;
  color: var(--color-text);
}

.progress-bar {
  height: 8px;
  background: var(--color-border);
  border-radius: 4px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: 4px;
  transition: width 0.35s ease;
}

.progress-fill.complete {
  background: var(--color-success);
}

.progress-hint {
  margin: 0;
  font-size: 0.8em;
  color: var(--color-text-secondary);
}

.progress-hint.ready {
  color: var(--color-success);
}

.upgrade-unlocked {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(25, 135, 84, 0.1);
  border: 1px solid rgba(25, 135, 84, 0.25);
}

.unlock-badge {
  font-size: 0.9em;
  font-weight: 600;
  color: var(--color-success);
}

.unlock-desc {
  font-size: 0.8em;
  color: var(--color-text-secondary);
  line-height: 1.4;
}

.tavern-upgrade-panel {
  --upgrade-brass: color-mix(in srgb, #5d87b3 74%, var(--color-primary));
  position: relative;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--upgrade-brass) 24%, var(--color-border));
  border-radius: 15px;
  background:
    radial-gradient(circle at 94% 10%, color-mix(in srgb, var(--upgrade-brass) 12%, transparent), transparent 30%),
    linear-gradient(120deg, color-mix(in srgb, var(--upgrade-brass) 6%, var(--color-surface)), var(--color-surface));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent), 0 9px 24px rgba(0, 0, 0, 0.08);
}
.upgrade-head,
.upgrade-progress,
.upgrade-unlocked { position: relative; z-index: 1; }
.upgrade-icon {
  filter: drop-shadow(0 4px 8px color-mix(in srgb, var(--upgrade-brass) 30%, transparent));
  animation: tavern-sign 3s ease-in-out infinite;
}
.progress-fill {
  background: linear-gradient(90deg, var(--upgrade-brass), color-mix(in srgb, var(--upgrade-brass) 55%, var(--color-primary)));
  box-shadow: 0 0 10px color-mix(in srgb, var(--upgrade-brass) 24%, transparent);
}
@keyframes tavern-sign { 50% { transform: translateY(-2px) rotate(-2deg); } }

@media (prefers-reduced-motion: reduce) {
  .upgrade-icon { animation: none; }
}

@media (max-width: 640px) {
  .upgrade-head {
    flex-direction: column;
    align-items: stretch;
  }

  .upgrade-btn {
    width: 100%;
  }
}
</style>
