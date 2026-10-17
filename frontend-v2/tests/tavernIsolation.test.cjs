const { test } = require('node:test')
const assert = require('node:assert/strict')
const { reactive } = require('vue')
const { createRouter, createMemoryHistory, isNavigationFailure } = require('vue-router')
const { documentHarness, deferred, storage, flush, response } = require('./sourceHarness.cjs')

function setupRouter(doc) {
  const component = { render: () => null }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', name: 'home', component },
    { path: '/auth', name: 'auth', component },
    { path: '/play/:slug', name: 'tavern-runtime', component },
  ] })
  doc.load('@/runtime/tavernNavigation').installTavernNavigation(router, doc.browser)
  return router
}

test('switch, leave, and re-enter require a new document; same tavern query stays cached', async () => {
  const doc = documentHarness()
  const router = setupRouter(doc)
  await router.push('/play/a')
  doc.lifecycle.begin('a')
  await router.push('/play/a?tab=logs')
  assert.equal(doc.navigations.length, 0)
  assert.ok(isNavigationFailure(await router.push('/play/b')))
  assert.equal(router.currentRoute.value.params.slug, 'a')
  assert.deepEqual(doc.navigations, ['/play/b'])
  assert.equal(doc.lifecycle.get().signal.aborted, true)
  assert.throws(() => doc.lifecycle.begin('b'))
  assert.throws(() => doc.lifecycle.begin('a'))
  await router.push('/')
  await router.push('/auth')
  assert.deepEqual(doc.navigations, ['/play/b', '/', '/auth'])
})

test('same account and equal character IDs get separate actual stores, init caches and requests', async () => {
  const shared = storage()
  const a = documentHarness(shared), b = documentHarness(shared)
  const aStore = a.store('a'), bStore = b.store('b')
  let aCalls = 0, bCalls = 0
  a.load('@/composables/useApi').default.defaults.adapter = async config => {
    aCalls++
    assert.equal(config.url, '/taverns/a/runtime/init')
    return response(config, { code: 200, data: { adventurers: [{ id: 1, name: 'A' }], buildings: [{ id: 1, name: 'A building' }] } })
  }
  b.load('@/composables/useApi').default.defaults.adapter = async config => {
    bCalls++
    assert.equal(config.url, '/taverns/b/runtime/init')
    return response(config, { code: 200, data: { adventurers: [{ id: 1, name: 'B' }], buildings: [{ id: 1, name: 'B building' }] } })
  }
  await aStore.fetchInit()
  assert.equal((await aStore.fetchInit()).cached, true)
  a.lifecycle.end()
  assert.equal(bStore.adventurers.length, 0)
  assert.equal((await bStore.fetchInit()).cached, undefined)
  assert.equal((await bStore.fetchInit()).cached, true)
  assert.equal(bStore.adventurers[0].name, 'B')
  assert.equal(bStore.buildings[0].name, 'B building')
  assert.equal(aCalls, 1)
  assert.equal(bCalls, 1)
})

test('back navigation leaves through a fresh document and rapid navigation cannot revive the old session', async () => {
  const doc = documentHarness()
  const router = setupRouter(doc)
  await router.push('/')
  await router.push('/play/a')
  doc.lifecycle.begin('a')
  router.back()
  await flush()
  assert.equal(doc.navigations[0], '/')
  assert.equal(doc.lifecycle.get().signal.aborted, true)
  await router.push('/play/b')
  await router.push('/play/c')
  assert.deepEqual(doc.navigations, ['/', '/play/b', '/play/c'])
  assert.equal(router.currentRoute.value.params.slug, 'a')
})

test('old reads cannot populate a new tavern; pending deduplication stays within the document', async () => {
  const a = documentHarness(), b = documentHarness()
  const aStore = a.store('a'), bStore = b.store('b')
  const oldReply = deferred()
  let calls = 0
  a.load('@/composables/useApi').default.defaults.adapter = config => { calls++; return oldReply.promise.then(data => response(config, data)) }
  const first = aStore.fetchInit().catch(error => error)
  const duplicate = aStore.fetchInit().catch(error => error)
  assert.equal(calls, 1)
  a.lifecycle.end()
  b.load('@/composables/useApi').default.defaults.adapter = async config => response(config, { code: 200, data: { adventurers: [{ id: 1, name: 'B' }] } })
  await bStore.fetchInit()
  oldReply.resolve({ code: 200, data: { adventurers: [{ id: 1, name: 'A late' }] } })
  assert.equal((await first).code, 'ERR_CANCELED')
  assert.equal((await duplicate).code, 'ERR_CANCELED')
  assert.equal(aStore.adventurers.length, 0)
  assert.equal(bStore.adventurers[0].name, 'B')
})

test('writes bind the original tavern synchronously, then late completion and old callbacks are rejected', async () => {
  const a = documentHarness(), b = documentHarness()
  const aStore = a.store('a'), bStore = b.store('b')
  const reply = deferred(), sent = []
  a.load('@/composables/useApi').default.defaults.adapter = config => { sent.push(config); return reply.promise.then(data => response(config, data)) }
  const oldWrite = aStore.renameInitialAdventurer(1, 'A renamed').catch(error => error)
  assert.equal(sent[0].url, '/taverns/a/runtime/1/initial-name')
  assert.equal(sent[0].headers.Authorization, 'Bearer same-account-token')
  a.lifecycle.end()
  bStore.adventurers = [{ id: 1, name: 'B' }]
  reply.resolve({ code: 200, data: { id: 1, name: 'A renamed' } })
  assert.equal((await oldWrite).code, 'ERR_CANCELED')
  await assert.rejects(aStore.renameInitialAdventurer(1, 'late callback'))
  assert.equal(sent.length, 1)
  assert.equal(bStore.adventurers[0].name, 'B')
})

test('old 401 and changed login cannot remove or reuse a newer credential', async () => {
  const doc = documentHarness()
  doc.lifecycle.begin('a')
  const reply = deferred()
  const api = doc.load('@/composables/useApi').default
  api.defaults.adapter = config => reply.promise.catch(() => Promise.reject({ config, response: { status: 401 } }))
  const pending = api.get('/adventurer-s1/init').catch(error => error)
  doc.storage.setItem('ai-rpg-token', 'new-account-token')
  reply.reject(new Error('old unauthorized'))
  assert.equal((await pending).code, 'ERR_CANCELED')
  assert.equal(doc.storage.getItem('ai-rpg-token'), 'new-account-token')
  await assert.rejects(api.post('/adventurer-s1/reserve', {}))
})

test('missing runtime session never falls through to an unscoped S1 URL', async () => {
  const doc = documentHarness()
  const api = doc.load('@/composables/useApi').default
  let sent = false
  api.defaults.adapter = async config => { sent = true; return response(config, {}) }
  await assert.rejects(api.get('/adventurer-s1/init'))
  assert.equal(sent, false)
})

test('current requests preserve all HTTP methods, caller cancellation and ordinary authorization failures', async () => {
  const doc = documentHarness()
  doc.lifecycle.begin('a b')
  const api = doc.load('@/composables/useApi').default
  const sent = []
  api.defaults.adapter = async config => { sent.push(config); return response(config, { code: 200 }) }
  for (const method of ['get', 'post', 'put', 'delete']) {
    const result = await api.request({ method, url: '/adventurer-s1/1', s1RealmContext: 'membership' })
    assert.equal(result.data.code, 200)
  }
  assert.deepEqual(sent.map(config => config.method), ['get', 'post', 'put', 'delete'])
  assert.ok(sent.every(config => config.url === '/taverns/a%20b/runtime/1' && !config.s1RealmContext))
  const caller = new AbortController()
  caller.abort()
  await assert.rejects(api.get('/adventurer-s1/init', { signal: caller.signal }), { code: 'ERR_CANCELED' })
  assert.equal(sent.length, 4)
  api.defaults.adapter = async config => { throw { config, response: { status: 401 } } }
  await assert.rejects(api.get('/adventurer-s1/init'), error => error.response.status === 401)
  assert.equal(doc.storage.getItem('ai-rpg-token'), null)
  assert.equal(doc.lifecycle.get().signal.aborted, true)
})

test('after B mounts successfully, delayed A success or failure cannot change B context or routing', async () => {
  for (const failure of [false, true]) {
    const reply = deferred()
    const frontend = { render() {} }
    const payload = slug => ({ data: { data: { tavern: { slug }, runtime: { frontend_plugin: 'adventurer_s1' } } } })
    const a = documentHarness(storage(), {
      'vue-router': { useRoute: () => reactive({ params: { slug: 'a' } }) },
      '@/api/client': { __esModule: true, default: { get: () => reply.promise } },
      '@/runtime/gamePackFrontendRegistry': { loadGamePackRuntimeFrontend: async () => frontend },
    })
    const hostA = a.mountHost()
    a.lifecycle.end()
    const b = documentHarness(storage(), {
      'vue-router': { useRoute: () => reactive({ params: { slug: 'b' } }) },
      '@/api/client': { __esModule: true, default: { get: async () => payload('b') } },
      '@/runtime/gamePackFrontendRegistry': { loadGamePackRuntimeFrontend: async () => frontend },
    })
    const hostB = b.mountHost()
    await flush()
    if (failure) reply.reject(new Error('A failure'))
    else reply.resolve(payload('a'))
    await flush()
    assert.equal(hostA.state.context, null)
    assert.equal(hostA.state.error, '')
    assert.equal(hostB.state.context.tavern.slug, 'b')
    assert.equal(hostB.state.runtimeComponent, frontend)
    assert.equal(hostB.state.error, '')
    assert.equal(b.load('@/runtime/activeTavern').getActiveTavernSlug(), 'b')
    hostA.unmount(); hostB.unmount()
  }
})

test('context result, context error and dynamic import cannot revive an unmounted host', async () => {
  for (const stage of ['context', 'error', 'import']) {
    const reply = deferred(), module = deferred()
    let imports = 0
    const doc = documentHarness(storage(), {
      'vue-router': { useRoute: () => reactive({ params: { slug: 'a' } }) },
      '@/api/client': { __esModule: true, default: { get: () => reply.promise } },
      '@/runtime/gamePackFrontendRegistry': { loadGamePackRuntimeFrontend: () => { imports++; return module.promise } },
    })
    const host = doc.mountHost()
    if (stage === 'import') {
      reply.resolve({ data: { data: { tavern: { slug: 'a' }, runtime: { frontend_plugin: 'adventurer_s1' } } } })
      await flush()
      assert.equal(imports, 1)
    }
    host.unmount()
    if (stage === 'error') reply.reject(new Error('late failure'))
    else reply.resolve({ data: { data: { tavern: { slug: 'a' }, runtime: { frontend_plugin: 'adventurer_s1' } } } })
    module.resolve({ render() {} })
    await flush()
    assert.equal(host.state.context, null, stage)
    assert.equal(host.state.runtimeComponent, null, stage)
    assert.equal(host.state.error, '', stage)
    if (stage !== 'import') assert.equal(imports, 0)
  }
})

test('back-forward restoration and another tab logging out retire the old session', async () => {
  const doc = documentHarness()
  setupRouter(doc)
  doc.lifecycle.begin('a')
  doc.events.get('pagehide')({})
  doc.events.get('pageshow')({ persisted: true })
  assert.deepEqual(doc.navigations, ['reload'])
  assert.throws(() => doc.lifecycle.requireCurrent())
  const other = documentHarness()
  setupRouter(other)
  other.lifecycle.begin('b')
  other.storage.removeItem('ai-rpg-token')
  other.events.get('storage')({ key: 'ai-rpg-token' })
  assert.deepEqual(other.navigations, ['reload'])
  assert.equal(other.lifecycle.get().signal.aborted, true)
  const home = documentHarness()
  setupRouter(home)
  home.events.get('pagehide')({})
  home.events.get('pageshow')({ persisted: true })
  assert.equal(home.lifecycle.begin('first-tavern').slug, 'first-tavern')
})

test('NPC pending receipts, storage events, locks and admin repair keys are tavern scoped', () => {
  const shared = storage()
  const a = documentHarness(shared), b = documentHarness(shared)
  a.lifecycle.begin('a'); b.lifecycle.begin('b')
  const aNpc = a.load('@/plugins/adventurer_s1/composables/useAmbientNpcEncounter')
  const bNpc = b.load('@/plugins/adventurer_s1/composables/useAmbientNpcEncounter')
  const keyA = aNpc.pendingStorageKey(7, 'same-encounter', 'gift')
  const keyB = bNpc.pendingStorageKey(7, 'same-encounter', 'gift')
  assert.notEqual(keyA, keyB)
  assert.notEqual(aNpc.AMBIENT_NPC_PENDING_LOCK_PREFIX, bNpc.AMBIENT_NPC_PENDING_LOCK_PREFIX)
  const pending = { userId: 7, encounterId: 'same-encounter', actionKey: 'gift', surface: 'tavern', requestId: 'request_123', createdAt: Date.now(), expiresAt: Date.now() + 60000, item: null }
  shared.setItem(keyA, JSON.stringify(pending))
  bNpc.ensurePendingActionSync()
  assert.equal(shared.getItem(keyA), JSON.stringify(pending), 'B cleanup must not delete A records')
  assert.equal(shared.getItem(keyB), null)
  b.events.get('storage')({ key: keyA, newValue: JSON.stringify(pending) })
  assert.equal(bNpc.pendingActionLocks.size, 0)
  b.events.get('storage')({ key: keyB, newValue: JSON.stringify(pending) })
  assert.equal(bNpc.pendingActionLocks.size, 1)
  const repair = 'advs1:last-companion-continuity-repair-run'
  const aKey = a.load('@/runtime/activeTavern').tavernStorageKey(repair)
  const bKey = b.load('@/runtime/activeTavern').tavernStorageKey(repair)
  shared.setItem(aKey, 'A-run')
  assert.equal(shared.getItem(bKey), null)
})
