const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')

const source = fs.readFileSync('src/components/admin/TavernModelConfigEditor.vue', 'utf8').split('<script setup lang="ts">')[1].split('</script>')[0]
const parsed = ts.createSourceFile('editor.ts', source, ts.ScriptTarget.Latest, true)
const printer = ts.createPrinter()
const script = parsed.statements.filter(n => !ts.isImportDeclaration(n)).map(n => printer.printNode(ts.EmitHint.Unspecified, n, parsed)).join('\n')
const compiled = ts.transpileModule(script, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}}).outputText
function data() {
  return {revision: 'a-version', balance_revision: 'a-balance', managed_by: 'tavern', blocked_credential_count: 0,
    profiles: [{id: 'primary', name: '故事', model: 'story', api_base: 'https://a.test/v1', provider: 'deepseek', api_key_configured: true, backup_api_keys: []},
               {id: 'secondary', name: '工具', model: 'tool', api_base: 'https://a.test/v1', provider: 'deepseek', backup_api_keys: []}],
    features: [{key: 'expedition_main', label: '探险主剧情'}, {key: 'building_ai', label: '建筑'}],
    default_profile_id: 'primary', assignments: {expedition_main: 'primary', building_ai: 'primary'}, reasoning_efforts: {expedition_main: 'high'}}
}
function editor(api) {
  let mount, unmount
  const context = {ref: vue.ref, computed: vue.computed, onMounted: f => {mount = f}, onBeforeUnmount: f => {unmount = f},
    getActiveTavernSlug: () => 'tavern-a', api, axios: {isCancel: e => e?.canceled, isAxiosError: e => !!e?.response},
    crypto: require('node:crypto'), window: {confirm: () => true}}
  vm.createContext(context)
  vm.runInContext(compiled + '\nglobalThis.editor = {catalog, dirty, busy, error, notice, search, bulkTarget, load, save, addProfile, removeProfile, assignAll};', context)
  return {...context.editor, mount, unmount}
}
test('public catalog roundtrip keeps keys blank and sends edited assignments', async () => {
  let sent
  const e = editor({get: async () => ({data: {data: data()}}), put: async (_, payload) => {
    sent = JSON.parse(JSON.stringify(payload)); return {data: {data: {...data(), revision: 'new-version', assignments: payload.assignments}}}
  }})
  await e.mount()
  assert.equal(e.catalog.value.profiles[0].api_key, '')
  assert.equal(e.dirty.value, false)
  e.catalog.value.profiles[0].api_key = 'new-test-key'
  e.catalog.value.assignments.building_ai = 'secondary'
  await e.save()
  assert.equal(sent.revision, 'a-version')
  assert.equal(sent.profiles[0].api_key, 'new-test-key')
  assert.equal(sent.assignments.building_ai, 'secondary')
  assert.equal(e.catalog.value.profiles[0].api_key, '')
  assert.equal(e.catalog.value.revision, 'new-version')
  assert.equal(e.dirty.value, false)
})
test('conflicting save retains the draft without retrying or reporting success', async () => {
  let calls = 0
  const e = editor({get: async () => ({data: {data: data()}}), put: async () => {
    calls++; throw {response: {status: 409, data: {message: '配置已更新，请刷新'}}}
  }})
  await e.mount(); e.catalog.value.profiles[0].api_key = 'draft-secret'
  await e.save()
  assert.equal(calls, 1)
  assert.equal(e.catalog.value.profiles[0].api_key, 'draft-secret')
  assert.equal(e.dirty.value, true)
  assert.match(e.error.value, /刷新/)
  assert.equal(e.notice.value, '')
})
test('a pending catalog load cannot repopulate an unmounted tavern editor', async () => {
  let resolve
  const e = editor({get: () => new Promise(r => {resolve = r})})
  const pending = e.mount(); e.unmount()
  resolve({data: {data: data()}}); await pending
  assert.equal(e.catalog.value, null)
})
test('bulk assignment affects filtered uses and referenced profiles cannot be removed', async () => {
  const e = editor({get: async () => ({data: {data: data()}})})
  await e.mount(); e.search.value = '建筑'; e.bulkTarget.value = 'secondary'; e.assignAll()
  assert.equal(e.catalog.value.assignments.building_ai, 'secondary')
  assert.equal(e.catalog.value.assignments.expedition_main, 'primary')
  e.removeProfile(e.catalog.value.profiles[1])
  assert.equal(e.catalog.value.profiles.length, 2)
  assert.match(e.error.value, /引用/)
})
