const { test } = require('node:test')
const assert = require('node:assert/strict')
const { documentHarness } = require('./sourceHarness.cjs')

test('release directory hides unfinished dungeons, not ticket or progression requirements', () => {
  const { isS1DungeonVisible } = documentHarness().load('@/runtime/s1FeatureVisibility')
  const rows = [
    { name: '转职试炼场', holds_ticket: false },
    { name: '顿悟秘境', holds_ticket: true },
    { name: '祛厄药庐', entry_enabled: true },
    { name: '万象裂隙', entry_enabled: true, holds_ticket: true },
    { name: '超越之门', entry_enabled: false },
  ]
  assert.deepEqual(rows.filter(isS1DungeonVisible).map(row => row.name), ['转职试炼场', '顿悟秘境', '祛厄药庐'])
  assert.equal(rows.length, 5) // Existing records remain available for history/classification.
})

test('release building directory preserves locked gameplay and monuments', () => {
  const { isS1BuildingVisible } = documentHarness().load('@/runtime/s1FeatureVisibility')
  const rows = ['coming_soon', 'transcendent_court_preview', 'contribution_monument', 'healing_house', 'market']
    .map(handler_key => ({ template: { handler_key }, lv2_locked_preview: true }))
  assert.deepEqual(rows.filter(isS1BuildingVisible).map(row => row.template.handler_key),
    ['contribution_monument', 'healing_house', 'market'])
  assert.equal(isS1BuildingVisible({ template: { handler_key: 'owner_statue' } }), true)
})
