import { computed, ref, type ComputedRef, type Ref } from 'vue'

export type InventoryRarityFilter = 'all' | 'common' | 'rare' | 'epic' | 'legendary'
export type InventorySort = 'default' | 'newest' | 'rarity-desc' | 'rarity-asc' | 'name-asc' | 'quantity-desc'

export interface InventoryListItem {
  id?: number | string | null
  name?: string | null
  rarity?: string | null
  quantity?: number | null
  created_at?: string | null
}

export const INVENTORY_RARITIES = ['common', 'rare', 'epic', 'legendary'] as const

const RARITY_RANK: Record<string, number> = {
  common: 0,
  rare: 1,
  epic: 2,
  legendary: 3,
}

function acquiredOrder(item: InventoryListItem): number {
  const createdAt = Date.parse(item.created_at ?? '')
  if (Number.isFinite(createdAt)) return createdAt
  const numericId = Number(item.id)
  return Number.isFinite(numericId) ? numericId : 0
}

export function useInventoryListControls<T extends InventoryListItem>(
  items: ComputedRef<T[]>,
  displayName: (item: T) => string = item => item.name ?? '',
): {
  rarityFilter: Ref<InventoryRarityFilter>
  sort: Ref<InventorySort>
  visibleItems: ComputedRef<T[]>
} {
  const rarityFilter = ref<InventoryRarityFilter>('all')
  const sort = ref<InventorySort>('default')

  const visibleItems = computed(() => {
    const filtered = rarityFilter.value === 'all'
      ? [...items.value]
      : items.value.filter(item => (item.rarity ?? 'common') === rarityFilter.value)

    switch (sort.value) {
      case 'newest':
        return filtered.sort((a, b) => acquiredOrder(b) - acquiredOrder(a))
      case 'rarity-desc':
        return filtered.sort((a, b) => (RARITY_RANK[b.rarity ?? 'common'] ?? -1) - (RARITY_RANK[a.rarity ?? 'common'] ?? -1))
      case 'rarity-asc':
        return filtered.sort((a, b) => (RARITY_RANK[a.rarity ?? 'common'] ?? -1) - (RARITY_RANK[b.rarity ?? 'common'] ?? -1))
      case 'name-asc':
        return filtered.sort((a, b) => displayName(a).localeCompare(displayName(b), 'zh-CN'))
      case 'quantity-desc':
        return filtered.sort((a, b) => (b.quantity ?? 1) - (a.quantity ?? 1))
      default:
        return filtered
    }
  })

  return { rarityFilter, sort, visibleItems }
}
