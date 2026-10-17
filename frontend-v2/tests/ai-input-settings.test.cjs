const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const source = fs.readFileSync('src/components/admin/TavernAiInputSettings.vue', 'utf8').split('<script setup lang="ts">')[1].split('</script>')[0]
const parsed = ts.createSourceFile('settings.ts', source, ts.ScriptTarget.Latest, true)
const printer = ts.createPrinter()
const script = parsed.statements.filter(n => !ts.isImportDeclaration(n)).map(n => printer.printNode(ts.EmitHint.Unspecified, n, parsed)).join('\n')
const compiled = ts.transpileModule(script, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}}).outputText
function editor(api) {
  let mount, unmount, slug = 'a'
  const context = {ref: vue.ref, onMounted: f => {mount = f}, onBeforeUnmount: f => {unmount = f},
    api, getActiveTavernSlug: () => slug, axios: {isCancel: e => e?.canceled, isAxiosError: e => !!e?.response}}
  vm.createContext(context)
  vm.runInContext(compiled + '\nglobalThis.editor = {settings, busy, error, notice, requestSettings}', context)
  return {...context.editor, mount, unmount, switchTavern: () => {slug = 'b'}}
}
test('one click persists toggle and revision, a rejected save leaves the previous state', async () => {
  let calls = 0
  const e = editor({get: async () => ({data: {data: {compression_enabled: false, revision: 'one'}}}),
    put: async (url, payload) => {
      assert.equal(url, '/adventurer-s1/admin/ai/input-settings')
      calls++
      if (calls === 2) throw {response: {status: 409, data: {message: '刷新后重试'}}}
      assert.equal(payload.compression_enabled, true)
      assert.equal(payload.revision, 'one')
      return {data: {data: {...payload, revision: 'two'}}}
    }})
  await e.mount()
  assert.equal(e.settings.value.compression_enabled, false)
  await e.requestSettings(true)
  assert.equal(e.settings.value.compression_enabled, true)
  await e.requestSettings(true)
  assert.equal(e.settings.value.compression_enabled, true)
  assert.equal(calls, 2)
  assert.match(e.error.value, /刷新/)
  assert.equal(e.notice.value, '')
})
test('pending responses and subsequent clicks cannot affect another tavern', async () => {
  let resolve, puts = 0
  const e = editor({get: () => new Promise(r => {resolve = r}), put: async () => {puts++}})
  const pending = e.mount()
  e.switchTavern()
  resolve({data: {data: {compression_enabled: true, revision: 'old'}}})
  await pending
  assert.equal(e.settings.value, null)
  await e.requestSettings(true)
  assert.equal(puts, 0)
})
