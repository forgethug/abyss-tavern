import type { Router } from 'vue-router'

import { tavernLifecycle } from './activeTavern'

export function installTavernNavigation(router: Router, browser: Window = window) {
  // Register before the asynchronous auth guard: revoke old callbacks immediately.
  router.beforeEach((to) => {
    const session = tavernLifecycle.get()
    if (!session) return
    if (to.name === 'tavern-runtime' && String(to.params.slug) === session.slug
        && tavernLifecycle.isCurrent(session)) return
    tavernLifecycle.end()
    browser.location.assign(router.resolve(to).href)
    return false
  })
  browser.addEventListener('pagehide', () => {
    if (tavernLifecycle.get()) tavernLifecycle.end()
  })
  browser.addEventListener('pageshow', (event) => {
    // Back/forward cache must not resurrect a disposed pack and its pending work.
    if (event.persisted && tavernLifecycle.get()) browser.location.reload()
  })
  browser.addEventListener('storage', (event) => {
    const session = tavernLifecycle.get()
    if ((event.key === 'ai-rpg-token' || event.key === null) && session
        && !tavernLifecycle.isCurrent(session)) {
      tavernLifecycle.end()
      browser.location.reload()
    }
  })
}
