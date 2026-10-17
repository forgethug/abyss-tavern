import type { SquadCatalogOption, SquadEmblemData } from './stores/adventurerS1'

// 本地渲染白名单：远端目录只能缩小可选 key，不能注入文案、颜色或样式。
export const SQUAD_EMBLEM_COLOR_OPTIONS: SquadCatalogOption[] = [
  { key: 'crimson', label: '绯红', value: '#a83a4b' },
  { key: 'azure', label: '苍蓝', value: '#3674a5' },
  { key: 'emerald', label: '翡翠', value: '#3d7f66' },
  { key: 'violet', label: '暮紫', value: '#76558f' },
  { key: 'gold', label: '鎏金', value: '#c89a3d' },
  { key: 'ivory', label: '象牙白', value: '#e7dfcc' },
  { key: 'obsidian', label: '黑曜', value: '#252a31' },
  { key: 'copper', label: '赤铜', value: '#a96845' },
  { key: 'sapphire', label: '深靛', value: '#294a78' },
  { key: 'teal', label: '青碧', value: '#2d7c79' },
  { key: 'jade', label: '玉青', value: '#6f9d64' },
  { key: 'rose', label: '玫瑰', value: '#b65a78' },
  { key: 'amber', label: '琥珀', value: '#cf7d32' },
  { key: 'silver', label: '秘银', value: '#aeb6c2' },
  { key: 'umber', label: '棕褐', value: '#6a4c3b' },
  { key: 'midnight', label: '夜蓝', value: '#2b294f' },
  { key: 'coral', label: '珊瑚', value: '#d3685c' },
  { key: 'lavender', label: '薰紫', value: '#9a79b6' },
]

export const SQUAD_EMBLEM_CATALOG = {
  frame: [
    { key: 'round', label: '月轮' },
    { key: 'shield', label: '古盾' },
    { key: 'diamond', label: '棱晶' },
    { key: 'hex', label: '六方印' },
    { key: 'square', label: '方印' },
    { key: 'arch', label: '拱门' },
    { key: 'crest', label: '尖徽' },
    { key: 'octagon', label: '八方印' },
    { key: 'banner', label: '战旗' },
    { key: 'rune', label: '秘文环' },
    { key: 'tablet', label: '石碑' },
    { key: 'petal', label: '花瓣章' },
  ],
  pattern: [
    { key: 'solid', label: '纯色' },
    { key: 'split', label: '对分' },
    { key: 'chevron', label: '山纹' },
    { key: 'rays', label: '辉芒' },
    { key: 'diagonal', label: '斜裁' },
    { key: 'quarters', label: '四象' },
    { key: 'cross', label: '十字' },
    { key: 'stripes', label: '战纹' },
    { key: 'rings', label: '涟漪' },
    { key: 'checker', label: '棋格' },
    { key: 'horizon', label: '天际' },
    { key: 'halo', label: '光环' },
  ],
  sigil: [
    { key: 'star', label: '星芒' },
    { key: 'sword', label: '古剑' },
    { key: 'moon', label: '弦月' },
    { key: 'flame', label: '火种' },
    { key: 'tower', label: '高塔' },
    { key: 'leaf', label: '月桂' },
    { key: 'shield', label: '守护盾' },
    { key: 'sun', label: '曜日' },
    { key: 'eye', label: '灵视' },
    { key: 'skull', label: '骸骨' },
    { key: 'key', label: '秘钥' },
    { key: 'paw', label: '兽爪' },
    { key: 'rose', label: '蔷薇' },
    { key: 'lightning', label: '雷霆' },
    { key: 'snow', label: '霜花' },
    { key: 'compass', label: '罗盘' },
    { key: 'planet', label: '星环' },
    { key: 'gem', label: '宝石' },
    { key: 'hammer', label: '战锤' },
    { key: 'anchor', label: '远航' },
    { key: 'chalice', label: '圣杯' },
    { key: 'dice', label: '命骰' },
    { key: 'book', label: '秘典' },
    { key: 'music', label: '乐章' },
    { key: 'heart', label: '心火' },
    { key: 'water', label: '浪潮' },
    { key: 'cloud', label: '云冠' },
    { key: 'flower', label: '花冠' },
    { key: 'crown', label: '荣冠' },
    { key: 'prism', label: '秘符' },
  ],
  primary: SQUAD_EMBLEM_COLOR_OPTIONS,
  secondary: SQUAD_EMBLEM_COLOR_OPTIONS,
  accent: SQUAD_EMBLEM_COLOR_OPTIONS,
} satisfies Record<keyof SquadEmblemData, SquadCatalogOption[]>

export const DEFAULT_SQUAD_EMBLEM: SquadEmblemData = {
  frame: 'shield',
  pattern: 'split',
  sigil: 'star',
  primary: 'crimson',
  secondary: 'obsidian',
  accent: 'gold',
}

function safeKey<K extends keyof SquadEmblemData>(
  field: K,
  value: unknown,
): string {
  const allowed = SQUAD_EMBLEM_CATALOG[field]
  return allowed.some(option => option.key === value)
    ? String(value)
    : DEFAULT_SQUAD_EMBLEM[field]
}

export function normalizeSquadEmblem(value: unknown): SquadEmblemData {
  const source = value && typeof value === 'object'
    ? value as Partial<Record<keyof SquadEmblemData, unknown>>
    : {}
  return {
    frame: safeKey('frame', source.frame),
    pattern: safeKey('pattern', source.pattern),
    sigil: safeKey('sigil', source.sigil),
    primary: safeKey('primary', source.primary),
    secondary: safeKey('secondary', source.secondary),
    accent: safeKey('accent', source.accent),
  }
}

export function safeSquadCatalogOptions(
  field: keyof SquadEmblemData,
  remoteOptions: Array<SquadCatalogOption | string> | undefined,
): SquadCatalogOption[] {
  const allowed = new Map(SQUAD_EMBLEM_CATALOG[field].map(option => [option.key, option]))
  if (!Array.isArray(remoteOptions)) return []
  const seen = new Set<string>()
  const result: SquadCatalogOption[] = []
  for (const raw of remoteOptions) {
    const key = typeof raw === 'string' ? raw : raw?.key
    const option = allowed.get(key)
    if (!option || seen.has(key)) continue
    seen.add(key)
    result.push(option)
  }
  return result
}
