import { onScopeDispose, ref } from 'vue'

/** 合并申诉通知与手动刷新：同一时刻只读一次，期间的新通知在完成后补读。 */
export function useS1AppealRefresh(load: (isActive: () => boolean) => Promise<void>) {
  const refreshing = ref(false)
  let request: Promise<void> | null = null
  let queued = false
  let disposed = false

  async function run() {
    refreshing.value = true
    try {
      do {
        queued = false
        await load(() => !disposed)
      } while (queued && !disposed)
    } finally {
      refreshing.value = false
      request = null
    }
  }

  function refresh(): Promise<void> {
    if (disposed) return Promise.resolve()
    queued = true
    if (!request) request = run()
    return request
  }

  onScopeDispose(() => {
    disposed = true
    queued = false
  })

  return { refreshing, refresh }
}
