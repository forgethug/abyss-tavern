const { test } = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const vm = require('node:vm')
const { createRenderer, defineAsyncComponent, h, nextTick, ref } = require('vue')

test('portable game view uses supported socket API and refreshes active tabs', () => {
  const source = readFileSync(resolve(__dirname, '../src/plugins/adventurer_s1/views/AdventurerS1View.vue'), 'utf8')
  assert.doesNotMatch(source, /syncS1AdminSocketRealmSubscription|unsubscribeS1AdminRealm|isS1AdminRealmSubscribed/)
  assert.match(source, /refreshActiveTabAfterReconnect\(\)\.catch/)
})

test('deferred S1 panel loads on open and cannot mount after its host closes', async () => {
  const source = readFileSync(resolve(__dirname, '../src/plugins/adventurer_s1/views/AdventurerS1View.vue'), 'utf8')
  const declaration = source.match(/^const MarketPanel = defineAsyncComponent\([^\n]+/m)?.[0]
  assert.ok(declaration, 'market must retain an async boundary')
  let loads = 0, mounts = 0, complete
  const pending = new Promise(resolve => { complete = resolve })
  const Panel = vm.runInNewContext(declaration.replace(/import\('[^']+'\)/, 'load()') + '; MarketPanel', {
    defineAsyncComponent, load: () => { loads++; return pending },
  })
  const node = () => ({ children: [] })
  const renderer = createRenderer({
    createElement: node, createText: node, createComment: node,
    setText() {}, setElementText() {}, patchProp() {},
    insert(child, parent) { child.parent = parent; parent.children.push(child) },
    remove(child) { if (child.parent) child.parent.children = child.parent.children.filter(item => item !== child) },
    parentNode: child => child.parent, nextSibling: () => null,
  })
  const visible = ref(false)
  const app = renderer.createApp({ setup: () => () => visible.value ? h(Panel) : null })
  app.mount(node())
  assert.equal(loads, 0)
  visible.value = true
  await nextTick()
  assert.equal(loads, 1)
  visible.value = false
  await nextTick()
  complete({ setup() { mounts++; return () => h('div') } })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(mounts, 0)
  visible.value = true
  await nextTick()
  assert.equal(loads, 1)
  assert.equal(mounts, 1)
  app.unmount()
})
