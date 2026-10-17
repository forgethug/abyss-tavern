import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

type ActivityListener = (active: boolean) => void

const VIEWPORT_PRELOAD_MARGIN = '320px 0px'
const activityListeners = new Map<Element, ActivityListener>()

let activityObserver: IntersectionObserver | null = null

function getActivityObserver(): IntersectionObserver | null {
  if (typeof IntersectionObserver === 'undefined') return null

  if (!activityObserver) {
    activityObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        activityListeners.get(entry.target)?.(entry.isIntersecting)
      }
    }, {
      rootMargin: VIEWPORT_PRELOAD_MARGIN,
      threshold: 0,
    })
  }

  return activityObserver
}

/**
 * 在元素接近视口时保持活跃，远离视口时允许调用方暂停纯装饰性工作。
 * 所有实例共用一个 IntersectionObserver，避免卡片列表按项创建观察器。
 */
export function useViewportActivity(target: Ref<HTMLElement | null>) {
  const isViewportActive = ref(true)
  let observedTarget: HTMLElement | null = null

  onMounted(() => {
    const element = target.value
    if (!element) return

    const observer = getActivityObserver()
    if (!observer) return

    observedTarget = element
    activityListeners.set(element, (active) => {
      isViewportActive.value = active
    })
    observer.observe(element)
  })

  onBeforeUnmount(() => {
    if (!observedTarget) return

    activityObserver?.unobserve(observedTarget)
    activityListeners.delete(observedTarget)
    observedTarget = null

    if (activityListeners.size === 0) {
      activityObserver?.disconnect()
      activityObserver = null
    }
  })

  return { isViewportActive }
}
