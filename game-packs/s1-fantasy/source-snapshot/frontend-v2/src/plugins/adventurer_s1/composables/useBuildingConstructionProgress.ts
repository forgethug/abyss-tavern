import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'
import { useLiveTick } from './useLiveTick'
import {
  getBuildingActiveHelperCount,
  type BuildingProgressSnapshot,
  buildingProgressAnchorKey,
  resolveBuildingProgressEtaMs,
} from '../utils'

export type { BuildingProgressSnapshot }

/**
 * 建造/升级进度：后端快照锚定 ETA，前端每秒本地倒计时，直到快照关键字段变化再重锚。
 */
export function useBuildingConstructionProgress(
  source: Ref<BuildingProgressSnapshot> | ComputedRef<BuildingProgressSnapshot>,
) {
  const { now } = useLiveTick()
  const anchorKey = ref('')
  const anchorMs = ref(0)
  const etaMs = ref<number | null>(null)

  watch(
    source,
    (b) => {
      const key = buildingProgressAnchorKey(b)
      if (key === anchorKey.value) return
      anchorKey.value = key
      anchorMs.value = Date.now()
      etaMs.value = resolveBuildingProgressEtaMs(b, anchorMs.value)
    },
    { immediate: true, deep: true },
  )

  const activeHelperCount = computed(() => getBuildingActiveHelperCount(source.value))

  const liveRemaining = computed(() => {
    const b = source.value
    if (b.status !== 'building' && b.status !== 'upgrading') {
      return Math.max(0, b.remaining_seconds ?? 0)
    }
    const remainingWork = Math.max(0, b.remaining_seconds ?? 0)
    if (remainingWork <= 0) return 0
    if (activeHelperCount.value <= 0) return remainingWork
    if (etaMs.value == null) return remainingWork
    return Math.max(0, Math.floor((etaMs.value - now.value) / 1000))
  })

  const liveProgress = computed(() => {
    const b = source.value
    if (b.status !== 'building' && b.status !== 'upgrading') {
      return b.progress_percent ?? 0
    }
    const total = b.total_build_seconds || (b.template?.build_hours ?? 1) * 3600
    if (total <= 0) return b.progress_percent ?? 0
    return Math.min(100, Math.round((1 - liveRemaining.value / total) * 100))
  })

  const isProgressPaused = computed(() => (
    (source.value.status === 'building' || source.value.status === 'upgrading')
    && activeHelperCount.value === 0
    && liveRemaining.value > 0
  ))

  return { liveRemaining, liveProgress, activeHelperCount, isProgressPaused }
}
