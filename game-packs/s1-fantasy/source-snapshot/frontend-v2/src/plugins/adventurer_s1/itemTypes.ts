/** S1 物品类型注册表 — 图鉴/装备展示与下拉均由此处引用；新增类型只改本文件。 */

export const ITEM_TYPE_LABELS = {
  companion: '伙伴',
  weapon: '武器',
  armor: '防具',
  shield: '盾牌',
  accessory: '饰品',
  consumable: '消耗品',
  potion: '药水',
  scroll: '卷轴',
  material: '材料',
  special: '特殊',
  quest: '任务',
  clue: '线索',
} as const

const ITEM_TYPE_DISPLAY_ALIASES: Record<string, string> = {
  ticket: '门票',
}

export type S1ItemType = keyof typeof ITEM_TYPE_LABELS

export interface RewardPreviewItem {
  name?: string
  item_name?: string
  quantity?: number
  rarity?: string
  type?: string
  description?: string
  desc?: string
}

/** 标准类型有序列表（= 下拉顺序） */
export const S1_ITEM_TYPES = Object.keys(ITEM_TYPE_LABELS) as S1ItemType[]

export const S1_ITEM_TYPE_SET = new Set<string>(S1_ITEM_TYPES)

export const S1_DEFAULT_ITEM_TYPE: S1ItemType = 'special'

export const S1_ITEM_TYPE_COMPANION: S1ItemType = 'companion'

/** 创建冒险者初始装备 Tab 优先顺序 */
export const STARTER_ITEM_TYPE_ORDER: readonly S1ItemType[] = [
  'weapon',
  'shield',
  'armor',
  'companion',
  'special',
]

export const ITEM_TYPE_OPTIONS = S1_ITEM_TYPES.map((value) => ({
  value,
  label: ITEM_TYPE_LABELS[value],
}))

export function itemTypeLabel(t?: string): string {
  if (!t) return ''
  return ITEM_TYPE_LABELS[t as S1ItemType] ?? ITEM_TYPE_DISPLAY_ALIASES[t] ?? t
}

export function isKnownItemType(t?: string): t is S1ItemType {
  return !!t && S1_ITEM_TYPE_SET.has(t)
}
