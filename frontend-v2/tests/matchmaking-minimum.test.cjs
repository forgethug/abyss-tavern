const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const source = fs.readFileSync('src/plugins/adventurer_s1/views/AdventurerS1View.vue', 'utf8').split('<script setup lang="ts">')[1].split('</script>')[0]
const parsed = ts.createSourceFile('view.ts', source, ts.ScriptTarget.Latest, true)
const printer = ts.createPrinter()
const names = new Set(['formatMatchTicketMembers', 'formatQueueAhead'])
const script = parsed.statements.filter(n => ts.isFunctionDeclaration(n) && names.has(n.name?.text))
  .map(n => printer.printNode(ts.EmitHint.Unspecified, n, parsed)).join('\n')
const context = vm.createContext({})
vm.runInContext(ts.transpileModule(script, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context)

test('one-person group displays the server minimum and actual fill choice', () => {
  const ticket = {queue_type: 'group', member_count: 1, ready_member_threshold: 1, allow_solo_fill: false}
  assert.equal(context.formatMatchTicketMembers(ticket), '1 人（可直接排队进本）')
  assert.match(context.formatQueueAhead(ticket), /本队不匹配路人，将按当前人数进本/)
  ticket.allow_solo_fill = true
  assert.match(context.formatQueueAhead(ticket), /本队已达进本门槛/)
  assert.doesNotMatch(context.formatQueueAhead(ticket), /5-6/)
})

test('the same group waits when this tavern requires four members', () => {
  const ticket = {queue_type: 'group', member_count: 1, ready_member_threshold: 4}
  assert.equal(context.formatMatchTicketMembers(ticket), '1 人（4 人起成团，最多 6 人）')
  assert.match(context.formatQueueAhead(ticket), /等待人数/)
})
