import { ref, onUnmounted } from 'vue'

/**
 * 全局单例 tick：每秒更新一次 Date.now()，多个组件共享同一个 interval。
 * 引用计数归零时自动停止，有新组件挂载时重新启动。
 */

const now = ref(Date.now())
let refCount = 0
let timer: ReturnType<typeof setInterval> | null = null

function start() {
  if (timer) return
  timer = setInterval(() => { now.value = Date.now() }, 1000)
}

function stop() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

export function useLiveTick() {
  refCount++
  start()

  onUnmounted(() => {
    refCount--
    if (refCount <= 0) {
      refCount = 0
      stop()
    }
  })

  return { now }
}
