export interface TavernSession {
  readonly slug: string
  readonly token: string
  readonly signal: AbortSignal
}

// Pack modules contain singleton stores, pending promises and read caches.
// A document may host only one tavern session, including after leaving it.
export function createTavernLifecycle(readToken: () => string | null) {
  let session: TavernSession | null = null
  const controller = new AbortController()

  function end() { controller.abort() }

  function isCurrent(candidate: TavernSession) {
    return session === candidate && !candidate.signal.aborted && readToken() === candidate.token
  }

  function begin(slug: string) {
    const token = readToken()
    if (!slug || !token || controller.signal.aborted || (session && session.slug !== slug)) {
      throw new Error('请重新进入酒馆')
    }
    if (!session) session = Object.freeze({ slug, token, signal: controller.signal })
    if (!isCurrent(session)) throw new Error('登录状态已改变，请重新进入酒馆')
    return session
  }

  function requireCurrent() {
    if (!session || !isCurrent(session)) throw new Error('酒馆会话已结束，请重新进入酒馆')
    return session
  }

  return { begin, end, isCurrent, requireCurrent, get: () => session }
}

export const tavernLifecycle = createTavernLifecycle(() => localStorage.getItem('ai-rpg-token'))

export function getActiveTavernSlug() {
  const session = tavernLifecycle.get()
  return session && tavernLifecycle.isCurrent(session) ? session.slug : null
}

export function tavernStorageKey(key: string) {
  return `tavern:${encodeURIComponent(tavernLifecycle.requireCurrent().slug)}:${key}`
}
