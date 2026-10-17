// Run actual TS/Vue setup code with real Vue, Pinia, Router and Axios. Each
// harness has its own module cache, matching a fresh browser document. Only
// transport, browser navigation and unrelated presentation helpers are faked.
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const vue = require('vue')
const { createPinia } = require('pinia')
const root = path.resolve(__dirname, '..')

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function storage() {
  const values = new Map([['ai-rpg-token', 'same-account-token']])
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    key: index => [...values.keys()][index] ?? null,
    get length() { return values.size },
  }
}

function documentHarness(sharedStorage = storage(), overrides = {}) {
  const cache = new Map()
  const events = new Map()
  const navigations = []
  const browser = {
    localStorage: sharedStorage,
    location: { assign: href => navigations.push(href), reload: () => navigations.push('reload') },
    addEventListener: (name, handler) => events.set(name, handler),
  }
  const context = vm.createContext({
    console, AbortController, AbortSignal, setTimeout, clearTimeout,
    localStorage: sharedStorage, window: browser,
  })
  const mocks = {
    '@/utils/analytics': { trackEvent() {} },
    // UI confirmation is not involved in the store actions exercised here.
    [path.join(root, 'src/plugins/adventurer_s1/utils.ts')]: { STAMINA_RECOVERY_PER_MINUTE: 1 },
    ...overrides,
  }
  function load(id, parent = path.join(root, 'entry.ts')) {
    if (Object.hasOwn(mocks, id)) return mocks[id]
    if (!id.startsWith('.') && !id.startsWith('@/') && !path.isAbsolute(id)) return require(id)
    let file = id.startsWith('@/') ? path.join(root, 'src', id.slice(2)) : path.resolve(path.dirname(parent), id)
    if (!path.extname(file)) file += '.ts'
    if (Object.hasOwn(mocks, file)) return mocks[file]
    if (cache.has(file)) return cache.get(file).exports
    let source = fs.readFileSync(file, 'utf8')
    if (file.endsWith('.vue')) source = compileScript(parse(source).descriptor, { id: 'test' }).content
    // Expose private key helpers to exercise the exact exported NPC projection.
    if (file.endsWith('useAmbientNpcEncounter.ts')) {
      source += '\nexport { pendingStorageKey, ensurePendingActionSync, pendingActionLocks, AMBIENT_NPC_PENDING_LOCK_PREFIX }'
    }
    const code = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
      fileName: file,
    }).outputText
    const module = { exports: {} }
    cache.set(file, module)
    vm.runInContext(`(function(require, module, exports) {${code}\n})`, context, { filename: file })(
      child => load(child, file), module, module.exports,
    )
    return module.exports
  }
  const lifecycle = load('@/runtime/activeTavern').tavernLifecycle
  function store(slug) {
    lifecycle.begin(slug)
    const definition = load('@/plugins/adventurer_s1/stores/adventurerS1').useAdventurerS1Store
    const result = definition(createPinia())
    result.prepareSquadSession(7)
    return result
  }
  function mountHost() {
    const component = load('@/views/TavernRuntimeView.vue').default
    component.render = () => null
    const renderer = vue.createRenderer({
      createComment: text => ({ text }), createText: text => ({ text }),
      insert() {}, remove() {}, parentNode() {}, nextSibling() {},
      createElement: tag => ({ tag }), setText() {}, setElementText() {}, patchProp() {},
    })
    const app = renderer.createApp(component)
    app.mount({})
    return { state: app._instance.setupState, unmount: () => app.unmount() }
  }
  return { load, lifecycle, store, mountHost, browser, events, navigations, storage: sharedStorage }
}

const flush = () => new Promise(resolve => setImmediate(resolve))
function response(config, data) { return { config, data, status: 200, statusText: 'OK', headers: {} } }
module.exports = { documentHarness, deferred, storage, flush, response }
