// Presentation availability for this release. Saved records remain intact.
export function isS1DungeonVisible(dungeon: { name: string; entry_enabled?: boolean }): boolean {
  return dungeon.entry_enabled !== false && dungeon.name !== '万象裂隙'
}

export function isS1BuildingVisible(building: { template?: { handler_key?: string | null } | null }): boolean {
  const handler = building.template?.handler_key
  return handler !== 'coming_soon' && handler !== 'transcendent_court_preview'
}
